"""Contract tests for PrskillAdapter (Day 4).

Tests use `httpx.MockTransport` with JSON fixtures matching the documented API
response shapes (see `docs/api/prskill.md` and `tests/fixtures/prskill_*.json`).
No real network calls are made.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from adapters.base import Capability
from adapters.prskill import PrskillAdapter
from models import OrderSpec, Scenario

FIXTURES = Path(__file__).parent / "fixtures"


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _make_client(
    handler: Callable[[httpx.Request], httpx.Response],
) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


# --- happy paths --------------------------------------------------------------


async def test_prskill_get_balance_warns_and_returns_zero() -> None:
    with pytest.warns(UserWarning, match="does not expose a balance endpoint"):
        async with _make_client(lambda r: httpx.Response(200, json={"balance": 0})) as http:
            adapter = PrskillAdapter("test_key", http)
            assert await adapter.get_balance() == 0.0


async def test_prskill_create_order_happy_path() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        body = dict(httpx.QueryParams(request.content.decode()))
        assert body["key"] == "test_key"
        if path == "/api" and body.get("action") == "services":
            return httpx.Response(200, json=_load("prskill_services.json"))
        if path == "/api" and body.get("action") == "add":
            assert body["service"] == "136"
            assert body["quantity"] == "100"
            assert body["service_url"] == "https://t.me/x"
            return httpx.Response(200, json=_load("prskill_create_order.json"))
        return httpx.Response(404, json={"error": "not found"})

    async with _make_client(handler) as http:
        adapter = PrskillAdapter("test_key", http)
        spec = OrderSpec(
            scenario=Scenario.ACTIVITY_SUBSCRIBE,
            exchange="prskill",
            target="https://t.me/x",
            quantity=100,
            service_id="136",
            max_cost=10.0,
        )
        external_id, cost = await adapter.create_order(spec, "uuid-1")
        assert external_id == "23501"
        # Service 136 rate=0.05, quantity=100 -> cost=5.00
        assert cost == pytest.approx(5.0)


@pytest.mark.parametrize(
    "fixture, expected",
    [
        ("prskill_order_status_process.json", "in_progress"),
        ("prskill_order_status_success.json", "completed"),
    ],
)
async def test_prskill_get_order_status_fixture(fixture: str, expected: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = dict(httpx.QueryParams(request.content.decode()))
        assert body["action"] == "status"
        assert body["order"] == "2168845"
        return httpx.Response(200, json=_load(fixture))

    async with _make_client(handler) as http:
        adapter = PrskillAdapter("test_key", http)
        assert await adapter.get_order_status("2168845") == expected


@pytest.mark.parametrize(
    "raw_status, expected",
    [
        ("process", "in_progress"),
        ("success", "completed"),
        ("cancel", "failed"),
        ("piece", "completed"),
        ("fail", "failed"),
        ("check", "in_progress"),
    ],
)
async def test_prskill_status_map_coverage(raw_status: str, expected: str) -> None:
    payload = {"orders": {"1": raw_status}}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    async with _make_client(handler) as http:
        adapter = PrskillAdapter("test_key", http)
        assert await adapter.get_order_status("1") == expected


# --- defensive paths ---------------------------------------------------------


async def test_prskill_refuses_create_when_cost_exceeds_max() -> None:
    create_called = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal create_called
        body = dict(httpx.QueryParams(request.content.decode()))
        if body.get("action") == "services":
            return httpx.Response(200, json=_load("prskill_services.json"))
        if body.get("action") == "add":
            create_called = True
            return httpx.Response(200, json={"order": 1})
        return httpx.Response(404)

    async with _make_client(handler) as http:
        adapter = PrskillAdapter("test_key", http)
        spec = OrderSpec(
            scenario=Scenario.ACTIVITY_SUBSCRIBE,
            exchange="prskill",
            target="https://t.me/x",
            quantity=100,
            service_id="136",
            max_cost=1.0,  # cost would be 5.00
        )
        with pytest.raises(ValueError, match=r"exceeds spec\.max_cost"):
            await adapter.create_order(spec, "uuid-1")
        assert not create_called, "/add must NOT be called when cost exceeds cap"


async def test_prskill_create_order_unknown_service_id() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = dict(httpx.QueryParams(request.content.decode()))
        if body.get("action") == "services":
            return httpx.Response(200, json=_load("prskill_services.json"))
        return httpx.Response(500)

    async with _make_client(handler) as http:
        adapter = PrskillAdapter("test_key", http)
        spec = OrderSpec(
            scenario=Scenario.ACTIVITY_SUBSCRIBE,
            exchange="prskill",
            target="https://t.me/x",
            quantity=10,
            service_id="999999",
            max_cost=10.0,
        )
        with pytest.raises(ValueError, match="not in catalogue"):
            await adapter.create_order(spec, "uuid-1")


async def test_prskill_create_order_requires_service_id() -> None:
    async with _make_client(lambda r: httpx.Response(500)) as http:
        adapter = PrskillAdapter("test_key", http)
        spec = OrderSpec(
            scenario=Scenario.ACTIVITY_SUBSCRIBE,
            exchange="prskill",
            target="https://t.me/x",
            quantity=10,
            max_cost=10.0,
        )
        with pytest.raises(ValueError, match="service_id"):
            await adapter.create_order(spec, "uuid-1")


async def test_prskill_raises_on_error_string_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="Invalid API key")

    async with _make_client(handler) as http:
        adapter = PrskillAdapter("test_key", http)
        with pytest.raises(RuntimeError, match="error string"):
            await adapter.get_order_status("1")


async def test_prskill_raises_on_error_key_in_json() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"error": "Some problem"})

    async with _make_client(handler) as http:
        adapter = PrskillAdapter("test_key", http)
        with pytest.raises(RuntimeError, match="Some problem"):
            await adapter.get_order_status("1")


async def test_prskill_requires_non_empty_key() -> None:
    async with _make_client(lambda r: httpx.Response(500)) as http:
        with pytest.raises(ValueError, match="api_key"):
            PrskillAdapter("", http)


async def test_prskill_services_cache_reused_across_calls() -> None:
    services_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal services_calls
        body = dict(httpx.QueryParams(request.content.decode()))
        if body.get("action") == "services":
            services_calls += 1
            return httpx.Response(200, json=_load("prskill_services.json"))
        if body.get("action") == "add":
            return httpx.Response(200, json=_load("prskill_create_order.json"))
        return httpx.Response(404)

    async with _make_client(handler) as http:
        adapter = PrskillAdapter("test_key", http)
        spec = OrderSpec(
            scenario=Scenario.ACTIVITY_SUBSCRIBE,
            exchange="prskill",
            target="https://t.me/x",
            quantity=10,
            service_id="136",
            max_cost=10.0,
        )
        await adapter.create_order(spec, "uuid-1")
        await adapter.create_order(spec, "uuid-2")
        assert services_calls == 1, "services catalogue should be cached across calls"


async def test_prskill_capabilities_panel_only() -> None:
    async with _make_client(lambda r: httpx.Response(500)) as http:
        adapter = PrskillAdapter("test_key", http)
        caps = adapter.capabilities()
        assert Capability.CREATE_ORDER in caps
        assert Capability.GET_BALANCE in caps
        assert Capability.LIST_SUBMISSIONS not in caps
        assert Capability.ACCEPT_SUBMISSION not in caps
        assert Capability.REJECT_SUBMISSION not in caps
        assert Capability.SUPPORTS_CLIENT_ORDER_ID not in caps
