"""Tests for the Ollama-driven autopilot path."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from adapters.base import Capability, PanelAdapter, ServiceOption
from autopilot.models import AutopilotIntent
from autopilot.ollama import OllamaPlanner
from autopilot.runner import AutopilotRunner
from config import Settings
from db.database import connect, get_order, init_db, list_active_orders
from models import OrderSpec, Scenario
from verification.activity_metrics import ActivityMetricSnapshot, ActivityMetricsProvider


class _FakePlanner:
    def __init__(self, intent: AutopilotIntent) -> None:
        self.intent = intent

    async def plan_goal(self, goal_text: str) -> AutopilotIntent:
        assert goal_text
        return self.intent


class _FakePanelAdapter(PanelAdapter):
    def __init__(self, name: str, options: list[ServiceOption]) -> None:
        self.name = name
        self.options = options
        self.created_specs: list[OrderSpec] = []

    def capabilities(self) -> set[Capability]:
        return {Capability.CREATE_ORDER, Capability.GET_ORDER_STATUS, Capability.GET_BALANCE}

    async def get_balance(self) -> float:
        return 1000.0

    async def list_services_for_scenario(
        self, scenario: Scenario, limit: int = 8
    ) -> list[ServiceOption]:
        return self.options[:limit]

    async def create_order(self, spec: OrderSpec, client_order_uuid: str) -> tuple[str, float]:
        self.created_specs.append(spec)
        option = next(o for o in self.options if o.service_id == spec.service_id)
        return f"ext-{self.name}-{client_order_uuid[:8]}", option.price_per_unit * spec.quantity

    async def get_order_status(self, external_order_id: str) -> str:
        return "completed"


class _FakeMetricsProvider(ActivityMetricsProvider):
    def __init__(self, snapshot: ActivityMetricSnapshot | None) -> None:
        self.snapshot = snapshot

    async def measure(self, target: str, scenario: Scenario) -> ActivityMetricSnapshot | None:
        assert target
        assert scenario
        return self.snapshot


def _settings(tmp_path: Path, *, dry_run: bool) -> Settings:
    return Settings(
        dry_run=dry_run,
        db_path=tmp_path / "autopilot.db",
        per_order_spend_limit=500.0,
        daily_spend_limit=1000.0,
    )


@pytest.mark.asyncio
async def test_ollama_planner_uses_structured_chat_api() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        body = json.loads(request.content.decode("utf-8"))
        captured["body"] = body
        return httpx.Response(
            200,
            json={
                "message": {
                    "content": json.dumps(
                        {
                            "scenario": "activity_like",
                            "target": "https://youtube.com/watch?v=abc",
                            "quantity": 500,
                            "confidence": 0.92,
                            "notes": "YouTube video likes",
                        }
                    )
                },
                "done": True,
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        planner = OllamaPlanner(
            base_url="http://ollama.local:11434",
            model="llama3.1",
            http_client=http,
        )
        intent = await planner.plan_goal("500 лайков на https://youtube.com/watch?v=abc")

    assert captured["url"] == "http://ollama.local:11434/api/chat"
    assert captured["body"]["stream"] is False
    assert captured["body"]["format"]["title"] == "AutopilotIntent"
    assert captured["body"]["options"]["temperature"] == 0
    assert intent.scenario == Scenario.ACTIVITY_LIKE
    assert intent.quantity == 500


@pytest.mark.asyncio
async def test_autopilot_selects_cheapest_service_and_creates_order(tmp_path: Path) -> None:
    settings = _settings(tmp_path, dry_run=False)
    await init_db(settings)
    intent = AutopilotIntent(
        scenario=Scenario.ACTIVITY_LIKE,
        target="https://youtube.com/watch?v=dQw4w9WgXcQ",
        quantity=100,
        confidence=0.9,
    )
    cheap = _FakePanelAdapter(
        "cheap",
        [ServiceOption(service_id="cheap-like", name="Лайки дешёвые", price_per_unit=0.03)],
    )
    expensive = _FakePanelAdapter(
        "expensive",
        [ServiceOption(service_id="exp-like", name="Лайки дорогие", price_per_unit=0.25)],
    )

    runner = AutopilotRunner(
        settings,
        {"expensive": expensive, "cheap": cheap},
        _FakePlanner(intent),
        activity_metrics_provider=_FakeMetricsProvider(
            ActivityMetricSnapshot(
                metric="likeCount",
                count=1000,
                source="test",
            )
        ),
    )
    result = await runner.run_goal("100 likes", actor="test:autopilot")

    assert result.status == "created"
    assert result.selected is not None
    assert result.selected.exchange == "cheap"
    assert result.cost == pytest.approx(3.0)
    assert result.baseline_count == 1000
    assert result.baseline_metric == "likeCount"
    assert result.baseline_source == "test"
    assert len(cheap.created_specs) == 1
    assert expensive.created_specs == []
    assert cheap.created_specs[0].baseline_count == 1000

    async with connect(settings) as conn:
        stored = await get_order(conn, result.order_uuid)
    assert stored is not None
    assert stored.spec.exchange == "cheap"
    assert stored.spec.service_id == "cheap-like"
    assert stored.spec.baseline_count == 1000
    assert stored.spec.baseline_metric == "likeCount"
    assert stored.spec.baseline_source == "test"


@pytest.mark.asyncio
async def test_autopilot_refuses_live_activity_order_without_baseline(tmp_path: Path) -> None:
    settings = _settings(tmp_path, dry_run=False)
    await init_db(settings)
    intent = AutopilotIntent(
        scenario=Scenario.ACTIVITY_VIEW,
        target="https://youtube.com/watch?v=dQw4w9WgXcQ",
        quantity=1000,
        confidence=0.9,
    )
    adapter = _FakePanelAdapter(
        "views",
        [ServiceOption(service_id="views-1", name="Просмотры YouTube", price_per_unit=0.01)],
    )
    runner = AutopilotRunner(
        settings,
        {"views": adapter},
        _FakePlanner(intent),
        activity_metrics_provider=_FakeMetricsProvider(None),
    )

    result = await runner.run_goal("1000 просмотров", actor="test:autopilot")

    assert result.status == "create_failed"
    assert "baseline is unavailable" in result.reason
    assert adapter.created_specs == []
    async with connect(settings) as conn:
        assert await list_active_orders(conn) == []


@pytest.mark.asyncio
async def test_autopilot_dry_run_plans_without_creating_order(tmp_path: Path) -> None:
    settings = _settings(tmp_path, dry_run=True)
    await init_db(settings)
    intent = AutopilotIntent(
        scenario=Scenario.ACTIVITY_VIEW,
        target="https://youtube.com/watch?v=abc",
        quantity=1000,
        confidence=0.9,
    )
    adapter = _FakePanelAdapter(
        "views",
        [ServiceOption(service_id="views-1", name="Просмотры YouTube", price_per_unit=0.01)],
    )
    runner = AutopilotRunner(settings, {"views": adapter}, _FakePlanner(intent))

    result = await runner.run_goal("1000 просмотров", actor="test:autopilot")

    assert result.status == "dry_run"
    assert result.selected is not None
    assert result.selected.service_id == "views-1"
    assert adapter.created_specs == []
    async with connect(settings) as conn:
        assert await list_active_orders(conn) == []
