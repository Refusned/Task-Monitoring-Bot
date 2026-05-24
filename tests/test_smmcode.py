"""Contract tests for SmmcodeAdapter (Day 2).

Tests use `httpx.MockTransport` with JSON fixtures matching the documented API
response shapes (see `docs/api/smmcode.md` and `tests/fixtures/smmcode_*.json`).
No real network calls are made.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from adapters.base import Capability
from adapters.smmcode import SmmcodeAdapter
from models import OrderSpec, Scenario

FIXTURES = Path(__file__).parent / "fixtures"


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _make_client(
    handler: Callable[[httpx.Request], httpx.Response],
) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


# --- happy paths --------------------------------------------------------------


async def test_smmcode_get_balance() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/api/reseller/balance"
        body = dict(httpx.QueryParams(request.content.decode()))
        assert body["api_token"] == "test_token"
        return httpx.Response(200, json=_load("smmcode_balance.json"))

    async with _make_client(handler) as http:
        adapter = SmmcodeAdapter("test_token", http)
        assert await adapter.get_balance() == 1537.84


async def test_smmcode_create_order_happy_path() -> None:
    """Estimate-first: /services is queried before /create_order; cost computed from catalogue."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        body = dict(httpx.QueryParams(request.content.decode()))
        assert body["api_token"] == "test_token"
        if path == "/api/reseller/services":
            return httpx.Response(200, json=_load("smmcode_services.json"))
        if path == "/api/reseller/create_order":
            assert body["service_id"] == "136"
            assert body["count"] == "100"
            assert body["link"] == "https://example.com"
            return httpx.Response(200, json=_load("smmcode_create_order.json"))
        return httpx.Response(404, json={"status": 404})

    async with _make_client(handler) as http:
        adapter = SmmcodeAdapter("test_token", http)
        spec = OrderSpec(
            scenario=Scenario.ACTIVITY_SUBSCRIBE,
            exchange="smmcode",
            target="https://example.com",
            quantity=100,
            service_id="136",
            max_cost=10.0,
        )
        external_id, cost = await adapter.create_order(spec, "uuid-1")
        assert external_id == "105987"
        # Service 136 price=0.05, quantity=100 -> cost=5.00
        assert cost == pytest.approx(5.0)


@pytest.mark.parametrize(
    "fixture, expected",
    [
        ("smmcode_order_status_completed.json", "completed"),
        ("smmcode_order_status_in_progress.json", "in_progress"),
    ],
)
async def test_smmcode_get_order_status_fixture(fixture: str, expected: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/reseller/order_status"
        body = dict(httpx.QueryParams(request.content.decode()))
        assert body["order_id"] == "123"
        return httpx.Response(200, json=_load(fixture))

    async with _make_client(handler) as http:
        adapter = SmmcodeAdapter("test_token", http)
        assert await adapter.get_order_status("123") == expected


@pytest.mark.parametrize(
    "status_id, expected",
    [
        (1, "in_progress"),
        (2, "failed"),
        (3, "completed"),
        (4, "completed"),
        (5, "failed"),
        (6, "failed"),
        (7, "in_progress"),
        (8, "failed"),
        (9, "in_progress"),
        (10, "in_progress"),
    ],
)
async def test_smmcode_status_map_coverage(status_id: int, expected: str) -> None:
    """Each raw status_id 1-10 must map deterministically per docs/api/smmcode.md."""
    payload = {
        "order": {
            "id": 1,
            "service_id": 1,
            "link": "x",
            "count": 1,
            "price": 1.0,
            "status_id": status_id,
            "status_name": f"raw-{status_id}",
        },
        "status": 200,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    async with _make_client(handler) as http:
        adapter = SmmcodeAdapter("test_token", http)
        assert await adapter.get_order_status("1") == expected


# --- defensive paths ---------------------------------------------------------


async def test_smmcode_refuses_create_when_cost_exceeds_max() -> None:
    """HIGH-c invariant: /create_order is NOT called when cost > spec.max_cost."""
    create_called = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal create_called
        if request.url.path == "/api/reseller/services":
            return httpx.Response(200, json=_load("smmcode_services.json"))
        if request.url.path == "/api/reseller/create_order":
            create_called = True
            return httpx.Response(200, json={"order_id": 1, "status": 200})
        return httpx.Response(404)

    async with _make_client(handler) as http:
        adapter = SmmcodeAdapter("test_token", http)
        spec = OrderSpec(
            scenario=Scenario.ACTIVITY_SUBSCRIBE,
            exchange="smmcode",
            target="https://example.com",
            quantity=100,
            service_id="136",
            max_cost=1.0,  # cost would be 5.00 - over the cap
        )
        with pytest.raises(ValueError, match=r"exceeds spec\.max_cost"):
            await adapter.create_order(spec, "uuid-1")
        assert not create_called, "/create_order must NOT be called when cost exceeds cap"


async def test_smmcode_create_order_unknown_service_id() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/reseller/services":
            return httpx.Response(200, json=_load("smmcode_services.json"))
        return httpx.Response(500)

    async with _make_client(handler) as http:
        adapter = SmmcodeAdapter("test_token", http)
        spec = OrderSpec(
            scenario=Scenario.ACTIVITY_SUBSCRIBE,
            exchange="smmcode",
            target="https://example.com",
            quantity=10,
            service_id="999999",  # not in fixture
            max_cost=10.0,
        )
        with pytest.raises(ValueError, match="not in catalogue"):
            await adapter.create_order(spec, "uuid-1")


async def test_smmcode_create_order_requires_service_id() -> None:
    async with _make_client(lambda r: httpx.Response(500)) as http:
        adapter = SmmcodeAdapter("test_token", http)
        spec = OrderSpec(
            scenario=Scenario.ACTIVITY_SUBSCRIBE,
            exchange="smmcode",
            target="https://example.com",
            quantity=10,
            max_cost=10.0,
            # service_id missing
        )
        with pytest.raises(ValueError, match="service_id"):
            await adapter.create_order(spec, "uuid-1")


async def test_smmcode_raises_on_non_200_status_field() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": 401, "error": "Invalid token"})

    async with _make_client(handler) as http:
        adapter = SmmcodeAdapter("test_token", http)
        with pytest.raises(RuntimeError, match="non-200 status"):
            await adapter.get_balance()


async def test_smmcode_requires_non_empty_token() -> None:
    async with _make_client(lambda r: httpx.Response(500)) as http:
        with pytest.raises(ValueError, match="api_token"):
            SmmcodeAdapter("", http)


async def test_smmcode_services_cache_reused_across_calls() -> None:
    """Repeated create_order calls should hit /services exactly once (cached)."""
    services_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal services_calls
        if request.url.path == "/api/reseller/services":
            services_calls += 1
            return httpx.Response(200, json=_load("smmcode_services.json"))
        if request.url.path == "/api/reseller/create_order":
            return httpx.Response(200, json=_load("smmcode_create_order.json"))
        return httpx.Response(404)

    async with _make_client(handler) as http:
        adapter = SmmcodeAdapter("test_token", http)
        spec = OrderSpec(
            scenario=Scenario.ACTIVITY_SUBSCRIBE,
            exchange="smmcode",
            target="https://example.com",
            quantity=10,
            service_id="136",
            max_cost=10.0,
        )
        await adapter.create_order(spec, "uuid-1")
        await adapter.create_order(spec, "uuid-2")
        assert services_calls == 1, "services catalogue should be cached across calls"


async def test_smmcode_capabilities_panel_only() -> None:
    """smmcode is a PANEL adapter - no list_submissions / accept / reject."""
    async with _make_client(lambda r: httpx.Response(500)) as http:
        adapter = SmmcodeAdapter("test_token", http)
        caps = adapter.capabilities()
        assert Capability.CREATE_ORDER in caps
        assert Capability.GET_BALANCE in caps
        assert Capability.LIST_SUBMISSIONS not in caps
        assert Capability.ACCEPT_SUBMISSION not in caps
        assert Capability.REJECT_SUBMISSION not in caps
        assert Capability.SUPPORTS_CLIENT_ORDER_ID not in caps
