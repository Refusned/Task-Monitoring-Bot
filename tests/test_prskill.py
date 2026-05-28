"""Contract tests for PrSkillAdapter (Day 4).

The Perfect Panel API form: POST `https://prskill.ru/api/v2` with body params
`key=<api_key>` + `action=<balance|services|add|status>`. Responses JSON.

Note: PRSKILL_API_KEY hasn't been provisioned yet — these are shape tests against
the documented Perfect-Panel form, not live-API exercises. They guard regressions
when the real key lands.
"""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest

from adapters.base import Capability
from adapters.prskill import PrSkillAdapter
from models import OrderSpec, Scenario


def _make_client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _body(request: httpx.Request) -> dict[str, str]:
    return dict(httpx.QueryParams(request.content.decode()))


_SERVICES = [
    {"service": "10", "name": "VK подписки", "rate": "50.0", "min": "100", "max": "10000"},
    {"service": "20", "name": "YouTube лайки", "rate": "120.0", "min": "10", "max": "10000"},
]


# --- happy paths --------------------------------------------------------------


async def test_prskill_get_balance_returns_none() -> None:
    """prskill public API has no balance action (confirmed against live docs
    at https://prskill.ru/api on 2026-05-28 — only services/add/status).
    Adapter returns None and MUST NOT hit the network."""
    network_calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        network_calls.append(str(request.url))
        return httpx.Response(500)

    async with _make_client(handler) as http:
        adapter = PrSkillAdapter("test_key", http)
        assert await adapter.get_balance() is None
        assert network_calls == []


async def test_prskill_create_order_happy_path() -> None:
    add_called = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal add_called
        body = _body(request)
        assert body["key"] == "test_key"
        if body["action"] == "services":
            return httpx.Response(200, json=_SERVICES)
        if body["action"] == "add":
            add_called = True
            assert body["service"] == "10"
            assert body["link"] == "https://vk.com/club1"
            assert body["quantity"] == "100"
            return httpx.Response(200, json={"order": 24680})
        return httpx.Response(404, json={"error": "Unknown action"})

    async with _make_client(handler) as http:
        adapter = PrSkillAdapter("test_key", http)
        spec = OrderSpec(
            scenario=Scenario.ACTIVITY_SUBSCRIBE,
            exchange="prskill",
            target="https://vk.com/club1",
            quantity=100,
            service_id="10",
            max_cost=10.0,
        )
        external_id, cost = await adapter.create_order(spec, "uuid-1")
        assert external_id == "24680"
        # rate 50 per 1000 -> 0.05 per unit; 100 units -> 5.0
        assert cost == pytest.approx(5.0)
    assert add_called


@pytest.mark.parametrize(
    "raw_status, expected",
    [
        ("Pending", "in_progress"),
        ("In progress", "in_progress"),
        ("Processing", "in_progress"),
        ("Completed", "completed"),
        ("Partial", "completed"),
        ("Canceled", "failed"),
        ("Cancelled", "failed"),
        ("Refunded", "failed"),
        ("Error", "failed"),
        ("WhoKnows", "failed"),
    ],
)
async def test_prskill_status_map(raw_status: str, expected: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"charge": "5.00", "status": raw_status, "remains": 0, "currency": "USD"},
        )

    async with _make_client(handler) as http:
        adapter = PrSkillAdapter("test_key", http)
        assert await adapter.get_order_status("24680") == expected


async def test_prskill_services_cache_reused() -> None:
    services_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal services_calls
        body = _body(request)
        if body["action"] == "services":
            services_calls += 1
            return httpx.Response(200, json=_SERVICES)
        if body["action"] == "add":
            return httpx.Response(200, json={"order": 1})
        return httpx.Response(404)

    async with _make_client(handler) as http:
        adapter = PrSkillAdapter("test_key", http)
        spec = OrderSpec(
            scenario=Scenario.ACTIVITY_SUBSCRIBE,
            exchange="prskill",
            target="https://vk.com/club1",
            quantity=100,
            service_id="10",
            max_cost=10.0,
        )
        await adapter.create_order(spec, "uuid-1")
        await adapter.create_order(spec, "uuid-2")
    assert services_calls == 1


# --- defensive paths ---------------------------------------------------------


async def test_prskill_refuses_create_when_cost_exceeds_max() -> None:
    add_called = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal add_called
        body = _body(request)
        if body["action"] == "services":
            return httpx.Response(200, json=_SERVICES)
        if body["action"] == "add":
            add_called = True
            return httpx.Response(200, json={"order": 1})
        return httpx.Response(404)

    async with _make_client(handler) as http:
        adapter = PrSkillAdapter("test_key", http)
        spec = OrderSpec(
            scenario=Scenario.ACTIVITY_SUBSCRIBE,
            exchange="prskill",
            target="https://vk.com/club1",
            quantity=1000,  # 1000 * 0.05 = 50.0 > cap
            service_id="10",
            max_cost=10.0,
        )
        with pytest.raises(ValueError, match=r"exceeds spec\.max_cost"):
            await adapter.create_order(spec, "uuid-1")
        assert not add_called


async def test_prskill_create_unknown_service_id() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if _body(request)["action"] == "services":
            return httpx.Response(200, json=_SERVICES)
        return httpx.Response(500)

    async with _make_client(handler) as http:
        adapter = PrSkillAdapter("test_key", http)
        spec = OrderSpec(
            scenario=Scenario.ACTIVITY_SUBSCRIBE,
            exchange="prskill",
            target="x",
            quantity=10,
            service_id="999",
            max_cost=10.0,
        )
        with pytest.raises(ValueError, match="not in catalogue"):
            await adapter.create_order(spec, "uuid-1")


async def test_prskill_error_response_raises() -> None:
    """Error envelope handling moved from get_balance (no-op now) to
    get_order_status — same _post wrapper."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"error": "Incorrect API key"})

    async with _make_client(handler) as http:
        adapter = PrSkillAdapter("bad_key", http)
        with pytest.raises(RuntimeError, match="Incorrect API key"):
            await adapter.get_order_status("123")


async def test_prskill_requires_non_empty_key() -> None:
    async with _make_client(lambda r: httpx.Response(500)) as http:
        with pytest.raises(ValueError, match="api_key"):
            PrSkillAdapter("", http)


async def test_prskill_capabilities_panel_only() -> None:
    async with _make_client(lambda r: httpx.Response(500)) as http:
        adapter = PrSkillAdapter("test_key", http)
        caps = adapter.capabilities()
    assert Capability.CREATE_ORDER in caps
    # GET_BALANCE intentionally absent: prskill API doesn't expose it.
    assert Capability.GET_BALANCE not in caps
    assert Capability.LIST_SUBMISSIONS not in caps
    assert Capability.ACCEPT_SUBMISSION not in caps
    assert Capability.SUPPORTS_CLIENT_ORDER_ID not in caps
