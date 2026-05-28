"""Contract tests for UnuAdapter (Day 4).

Tests use `httpx.MockTransport` against JSON fixtures matching the documented
response shapes (see `docs/api/unu.md` and `tests/fixtures/unu_*.json`). No real
network calls.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from adapters.base import Capability
from adapters.unu import UnuAdapter
from models import OrderSpec, Scenario, SourcePlatform

FIXTURES = Path(__file__).parent / "fixtures"


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _make_client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _body(request: httpx.Request) -> dict[str, str]:
    return dict(httpx.QueryParams(request.content.decode()))


# --- happy paths --------------------------------------------------------------


async def test_unu_get_balance() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/api"
        body = _body(request)
        assert body["api_key"] == "test_key"
        assert body["action"] == "get_balance"
        return httpx.Response(200, json=_load("unu_balance.json"))

    async with _make_client(handler) as http:
        adapter = UnuAdapter("test_key", http)
        assert await adapter.get_balance() == 312.45


async def test_unu_create_order_happy_path() -> None:
    """Estimate-first: get_tariffs is queried before add_task_start; cost computed
    from catalogue price * quantity.
    """
    add_called = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal add_called
        body = _body(request)
        assert body["api_key"] == "test_key"
        if body["action"] == "get_tariffs":
            return httpx.Response(200, json=_load("unu_tariffs.json"))
        if body["action"] == "add_task_start":
            add_called = True
            assert body["tarif_id"] == "101"
            assert body["add_to_limit"] == "10"
            assert body["link"] == "https://t.me/x"
            assert body["price"] == "0.5"
            return httpx.Response(200, json=_load("unu_add_task_start.json"))
        return httpx.Response(404, json={"success": 0})

    async with _make_client(handler) as http:
        adapter = UnuAdapter("test_key", http)
        spec = OrderSpec(
            scenario=Scenario.ACTIVITY_SUBSCRIBE,
            exchange="unu",
            target="https://t.me/x",
            quantity=10,
            service_id="101",
            max_cost=10.0,
        )
        external_id, cost = await adapter.create_order(spec, "uuid-1")
        assert external_id == "778899"
        # tariff 101 price=0.5 * 10 = 5.0
        assert cost == pytest.approx(5.0)
        assert add_called


@pytest.mark.parametrize(
    "fixture, expected",
    [
        ("unu_get_tasks_active.json", "in_progress"),
        ("unu_get_tasks_completed.json", "completed"),
    ],
)
async def test_unu_get_order_status_fixture(fixture: str, expected: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = _body(request)
        assert body["action"] == "get_tasks"
        assert body["task_id"] == "778899"
        return httpx.Response(200, json=_load(fixture))

    async with _make_client(handler) as http:
        adapter = UnuAdapter("test_key", http)
        assert await adapter.get_order_status("778899") == expected


@pytest.mark.parametrize(
    "raw_status, expected",
    [
        (1, "in_progress"),
        (2, "completed"),
        (3, "failed"),
        (4, "in_progress"),
        (5, "failed"),
        (6, "in_progress"),
        (99, "failed"),  # unknown -> conservative failed
    ],
)
async def test_unu_task_status_map(raw_status: int, expected: str) -> None:
    payload = {"success": 1, "tasks": [{"id": 1, "status": raw_status}]}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    async with _make_client(handler) as http:
        adapter = UnuAdapter("test_key", http)
        assert await adapter.get_order_status("1") == expected


async def test_unu_list_submissions_filters_awaiting_only() -> None:
    """get_reports returns mixed statuses; only status=2 (on review) is surfaced."""

    def handler(request: httpx.Request) -> httpx.Response:
        body = _body(request)
        assert body["action"] == "get_reports"
        assert body["task_id"] == "778899"
        return httpx.Response(200, json=_load("unu_get_reports.json"))

    async with _make_client(handler) as http:
        adapter = UnuAdapter("test_key", http)
        subs = await adapter.list_submissions("778899")
    assert [s.external_submission_id for s in subs] == ["5001", "5003"]
    # First sub carries evidence text from messages[0].text
    assert subs[0].evidence is not None and "imgur" in subs[0].evidence
    assert subs[0].executor_hint == "42"


async def test_unu_accept_submission_calls_approve_report() -> None:
    seen: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = _body(request)
        seen.append((body["action"], body.get("report_id", "")))
        return httpx.Response(200, json=_load("unu_approve_report.json"))

    async with _make_client(handler) as http:
        adapter = UnuAdapter("test_key", http)
        await adapter.accept_submission("778899", "5001")
    assert seen == [("approve_report", "5001")]


async def test_unu_reject_submission_uses_reject_type_1() -> None:
    """A6: reject defaults to 'to revision' (rework), not outright refusal."""
    seen: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = _body(request)
        seen.append(body)
        return httpx.Response(200, json={"success": 1})

    async with _make_client(handler) as http:
        adapter = UnuAdapter("test_key", http)
        await adapter.reject_submission("778899", "5002", "evidence missing")
    assert len(seen) == 1
    call = seen[0]
    assert call["action"] == "reject_report"
    assert call["report_id"] == "5002"
    assert call["reject_type"] == "1"
    assert call["comment"] == "evidence missing"


# --- defensive paths ---------------------------------------------------------


async def test_unu_refuses_create_when_cost_exceeds_max() -> None:
    """HIGH-c: add_task_start is NOT called when cost > spec.max_cost."""
    add_called = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal add_called
        body = _body(request)
        if body["action"] == "get_tariffs":
            return httpx.Response(200, json=_load("unu_tariffs.json"))
        if body["action"] == "add_task_start":
            add_called = True
            return httpx.Response(200, json=_load("unu_add_task_start.json"))
        return httpx.Response(404, json={"success": 0})

    async with _make_client(handler) as http:
        adapter = UnuAdapter("test_key", http)
        spec = OrderSpec(
            scenario=Scenario.SOCIAL_TRAFFIC,
            exchange="unu",
            target="https://example.com",
            quantity=10,
            service_id="303",  # price 2.0 * 10 = 20.0
            source_platform=SourcePlatform.VK,
            max_cost=5.0,
        )
        with pytest.raises(ValueError, match=r"exceeds spec\.max_cost"):
            await adapter.create_order(spec, "uuid-1")
        assert not add_called


async def test_unu_create_order_unknown_tarif_id() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if _body(request)["action"] == "get_tariffs":
            return httpx.Response(200, json=_load("unu_tariffs.json"))
        return httpx.Response(500)

    async with _make_client(handler) as http:
        adapter = UnuAdapter("test_key", http)
        spec = OrderSpec(
            scenario=Scenario.ACTIVITY_SUBSCRIBE,
            exchange="unu",
            target="https://t.me/x",
            quantity=10,
            service_id="999999",
            max_cost=10.0,
        )
        with pytest.raises(ValueError, match="not in catalogue"):
            await adapter.create_order(spec, "uuid-1")


async def test_unu_create_order_requires_service_id() -> None:
    async with _make_client(lambda r: httpx.Response(500)) as http:
        adapter = UnuAdapter("test_key", http)
        spec = OrderSpec(
            scenario=Scenario.ACTIVITY_SUBSCRIBE,
            exchange="unu",
            target="https://t.me/x",
            quantity=10,
            max_cost=10.0,
        )
        with pytest.raises(ValueError, match="service_id"):
            await adapter.create_order(spec, "uuid-1")


async def test_unu_raises_on_non_success_payload() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"success": 0, "errors": "Invalid api_key"})

    async with _make_client(handler) as http:
        adapter = UnuAdapter("bad_key", http)
        with pytest.raises(RuntimeError, match="non-success"):
            await adapter.get_balance()


async def test_unu_requires_non_empty_key() -> None:
    async with _make_client(lambda r: httpx.Response(500)) as http:
        with pytest.raises(ValueError, match="api_key"):
            UnuAdapter("", http)


async def test_unu_tariffs_cache_reused_across_calls() -> None:
    tariff_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal tariff_calls
        body = _body(request)
        if body["action"] == "get_tariffs":
            tariff_calls += 1
            return httpx.Response(200, json=_load("unu_tariffs.json"))
        if body["action"] == "add_task_start":
            return httpx.Response(200, json=_load("unu_add_task_start.json"))
        return httpx.Response(404, json={"success": 0})

    async with _make_client(handler) as http:
        adapter = UnuAdapter("test_key", http)
        spec = OrderSpec(
            scenario=Scenario.ACTIVITY_SUBSCRIBE,
            exchange="unu",
            target="https://t.me/x",
            quantity=10,
            service_id="101",
            max_cost=10.0,
        )
        await adapter.create_order(spec, "uuid-1")
        await adapter.create_order(spec, "uuid-2")
    assert tariff_calls == 1


async def test_unu_capabilities_task_exchange() -> None:
    """unu is a TASK-EXCHANGE adapter with full lifecycle."""
    async with _make_client(lambda r: httpx.Response(500)) as http:
        adapter = UnuAdapter("test_key", http)
        caps = adapter.capabilities()
    assert Capability.CREATE_ORDER in caps
    assert Capability.LIST_SUBMISSIONS in caps
    assert Capability.ACCEPT_SUBMISSION in caps
    assert Capability.REJECT_SUBMISSION in caps
    assert Capability.SUPPORTS_CLIENT_ORDER_ID not in caps
