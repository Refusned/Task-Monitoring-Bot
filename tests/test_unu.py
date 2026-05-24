"""Contract tests for UnuAdapter (Day 4).

Tests use `httpx.MockTransport` with JSON fixtures matching the documented API
response shapes (see `docs/api/unu.md` and `tests/fixtures/unu_*.json`).
No real network calls are made.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from adapters.base import Capability
from adapters.unu import UnuAdapter
from models import OrderSpec, Scenario

FIXTURES = Path(__file__).parent / "fixtures"


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _make_client(
    handler: Callable[[httpx.Request], httpx.Response],
) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


# --- happy paths --------------------------------------------------------------


async def test_unu_get_balance() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = dict(httpx.QueryParams(request.content.decode()))
        assert body["api_key"] == "test_key"
        assert body["action"] == "get_balance"
        return httpx.Response(200, json=_load("unu_balance.json"))

    async with _make_client(handler) as http:
        adapter = UnuAdapter("test_key", http)
        assert await adapter.get_balance() == 1000.0


async def test_unu_create_order_happy_path() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = dict(httpx.QueryParams(request.content.decode()))
        assert body["api_key"] == "test_key"
        if body.get("action") == "get_tariffs":
            return httpx.Response(200, json=_load("unu_tariffs.json"))
        if body.get("action") == "add_task_start":
            assert body["name"] == "activity_subscribe"
            assert body["link"] == "https://t.me/x"
            assert body["add_to_limit"] == "50"
            return httpx.Response(200, json=_load("unu_create_task.json"))
        return httpx.Response(404, json={"success": 0, "errors": "not found"})

    async with _make_client(handler) as http:
        adapter = UnuAdapter("test_key", http)
        spec = OrderSpec(
            scenario=Scenario.ACTIVITY_SUBSCRIBE,
            exchange="unu",
            target="https://t.me/x",
            quantity=50,
            service_id="1",
            max_cost=200.0,
        )
        external_id, cost = await adapter.create_order(spec, "uuid-1")
        assert external_id == "98765"
        # Tariff 1 price=2.00, quantity=50 -> cost=100.00
        assert cost == pytest.approx(100.0)


@pytest.mark.parametrize(
    "fixture, expected",
    [
        ("unu_get_tasks.json", "in_progress"),
    ],
)
async def test_unu_get_order_status_fixture(fixture: str, expected: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = dict(httpx.QueryParams(request.content.decode()))
        assert body["action"] == "get_tasks"
        assert body["task_id"] == "98765"
        return httpx.Response(200, json=_load(fixture))

    async with _make_client(handler) as http:
        adapter = UnuAdapter("test_key", http)
        assert await adapter.get_order_status("98765") == expected


@pytest.mark.parametrize(
    "raw_status, expected",
    [
        (1, "in_progress"),
        (2, "completed"),
        (3, "failed"),
        (4, "in_progress"),
        (5, "failed"),
        (6, "in_progress"),
    ],
)
async def test_unu_task_status_map_coverage(raw_status: int, expected: str) -> None:
    payload = {
        "tasks": [{"id": 1, "status": raw_status, "name": "x"}],
        "success": 1,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    async with _make_client(handler) as http:
        adapter = UnuAdapter("test_key", http)
        assert await adapter.get_order_status("1") == expected


async def test_unu_list_submissions() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = dict(httpx.QueryParams(request.content.decode()))
        assert body["action"] == "get_reports"
        assert body["task_id"] == "98765"
        return httpx.Response(200, json=_load("unu_get_reports.json"))

    async with _make_client(handler) as http:
        adapter = UnuAdapter("test_key", http)
        subs = await adapter.list_submissions("98765")
        # Status 1 (In work) and 2 (On review) are both pending a decision
        assert len(subs) == 2
        ids = {s.external_submission_id for s in subs}
        assert "111" in ids
        assert "112" in ids


async def test_unu_accept_submission() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = dict(httpx.QueryParams(request.content.decode()))
        assert body["action"] == "approve_report"
        assert body["report_id"] == "111"
        return httpx.Response(200, json={"success": 1})

    async with _make_client(handler) as http:
        adapter = UnuAdapter("test_key", http)
        await adapter.accept_submission("98765", "111")


async def test_unu_reject_submission() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = dict(httpx.QueryParams(request.content.decode()))
        assert body["action"] == "reject_report"
        assert body["report_id"] == "111"
        assert body["comment"] == "low quality"
        assert body["reject_type"] == "1"
        return httpx.Response(200, json={"success": 1})

    async with _make_client(handler) as http:
        adapter = UnuAdapter("test_key", http)
        await adapter.reject_submission("98765", "111", "low quality")


# --- defensive paths ---------------------------------------------------------


async def test_unu_refuses_create_when_cost_exceeds_max() -> None:
    create_called = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal create_called
        body = dict(httpx.QueryParams(request.content.decode()))
        if body.get("action") == "get_tariffs":
            return httpx.Response(200, json=_load("unu_tariffs.json"))
        if body.get("action") == "add_task_start":
            create_called = True
            return httpx.Response(200, json={"task_id": 1, "success": 1})
        return httpx.Response(404)

    async with _make_client(handler) as http:
        adapter = UnuAdapter("test_key", http)
        spec = OrderSpec(
            scenario=Scenario.ACTIVITY_SUBSCRIBE,
            exchange="unu",
            target="https://t.me/x",
            quantity=50,
            service_id="1",
            max_cost=1.0,  # cost would be 100.00
        )
        with pytest.raises(ValueError, match=r"exceeds spec\.max_cost"):
            await adapter.create_order(spec, "uuid-1")
        assert not create_called, "add_task_start must NOT be called when cost exceeds cap"


async def test_unu_create_order_unknown_tariff() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if dict(httpx.QueryParams(request.content.decode())).get("action") == "get_tariffs":
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
        with pytest.raises(ValueError, match="not found"):
            await adapter.create_order(spec, "uuid-1")


async def test_unu_raises_on_non_success_envelope() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"success": 0, "errors": "Invalid token"})

    async with _make_client(handler) as http:
        adapter = UnuAdapter("test_key", http)
        with pytest.raises(RuntimeError, match="Invalid token"):
            await adapter.get_balance()


async def test_unu_requires_non_empty_key() -> None:
    async with _make_client(lambda r: httpx.Response(500)) as http:
        with pytest.raises(ValueError, match="api_key"):
            UnuAdapter("", http)


async def test_unu_tariffs_cache_reused_across_calls() -> None:
    tariffs_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal tariffs_calls
        body = dict(httpx.QueryParams(request.content.decode()))
        if body.get("action") == "get_tariffs":
            tariffs_calls += 1
            return httpx.Response(200, json=_load("unu_tariffs.json"))
        if body.get("action") == "add_task_start":
            return httpx.Response(200, json=_load("unu_create_task.json"))
        return httpx.Response(404)

    async with _make_client(handler) as http:
        adapter = UnuAdapter("test_key", http)
        spec = OrderSpec(
            scenario=Scenario.ACTIVITY_SUBSCRIBE,
            exchange="unu",
            target="https://t.me/x",
            quantity=10,
            service_id="1",
            max_cost=200.0,
        )
        await adapter.create_order(spec, "uuid-1")
        await adapter.create_order(spec, "uuid-2")
        assert tariffs_calls == 1, "tariffs should be cached across calls"


async def test_unu_capabilities_task_exchange() -> None:
    async with _make_client(lambda r: httpx.Response(500)) as http:
        adapter = UnuAdapter("test_key", http)
        caps = adapter.capabilities()
        assert Capability.CREATE_ORDER in caps
        assert Capability.GET_BALANCE in caps
        assert Capability.LIST_SUBMISSIONS in caps
        assert Capability.ACCEPT_SUBMISSION in caps
        assert Capability.REJECT_SUBMISSION in caps
        assert Capability.SUPPORTS_CLIENT_ORDER_ID not in caps
