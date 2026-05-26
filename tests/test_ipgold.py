"""Contract tests for IpgoldAdapter — Perfect Panel form.

ipgold.ru exposes a Perfect-Panel-style API at `https://ipgold.ru/api/v2`.
The adapter is a `PanelAdapter` (orders are prepaid; no per-submission cycle).
"""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest

from adapters.base import Capability, PanelAdapter, TaskExchangeAdapter
from adapters.ipgold import IpgoldAdapter
from models import OrderSpec, Scenario


def _make_client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _body(request: httpx.Request) -> dict[str, str]:
    return dict(httpx.QueryParams(request.content.decode()))


_SERVICES_LIST = [
    {"service": "1", "name": "Подписчики Telegram", "rate": "50.0", "min": "100", "max": "10000"},
    {"service": "2", "name": "Лайки YouTube", "rate": "120.0", "min": "10", "max": "10000"},
    {
        "service": "3",
        "name": "Переходы на сайт из соцсетей",
        "rate": "30.0",
        "min": "100",
        "max": "100000",
    },
]


# --- capabilities + shape ----------------------------------------------------


async def test_ipgold_capabilities() -> None:
    """ipgold is panel-style: create/status/balance, no submission cycle."""
    async with _make_client(lambda r: httpx.Response(500)) as http:
        adapter = IpgoldAdapter("test_key", http)
        caps = adapter.capabilities()
    assert Capability.CREATE_ORDER in caps
    assert Capability.GET_ORDER_STATUS in caps
    assert Capability.GET_BALANCE in caps
    assert Capability.LIST_SUBMISSIONS not in caps
    assert Capability.ACCEPT_SUBMISSION not in caps
    assert Capability.REJECT_SUBMISSION not in caps
    assert Capability.SUPPORTS_CLIENT_ORDER_ID not in caps


async def test_ipgold_is_panel_adapter_not_task_exchange() -> None:
    """ipgold's lifecycle matches smmcode/prskill — prepaid, no rework."""
    async with _make_client(lambda r: httpx.Response(500)) as http:
        adapter = IpgoldAdapter("test_key", http)
    assert isinstance(adapter, PanelAdapter)
    assert not isinstance(adapter, TaskExchangeAdapter)


async def test_ipgold_requires_non_empty_key() -> None:
    async with _make_client(lambda r: httpx.Response(500)) as http:
        with pytest.raises(ValueError, match="api_key"):
            IpgoldAdapter("", http)


# --- happy paths -------------------------------------------------------------


async def test_ipgold_get_balance_happy_path() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = _body(request)
        assert request.url.path == "/api/v2"
        assert body["api_key"] == "test_key"
        assert body["action"] == "balance"
        return httpx.Response(200, json={"status": "OK", "balance": "412.34", "currency": "RUB"})

    async with _make_client(handler) as http:
        adapter = IpgoldAdapter("test_key", http)
        assert await adapter.get_balance() == pytest.approx(412.34)


async def test_ipgold_get_balance_handles_numeric_balance() -> None:
    """Tolerate `{balance: 100.5}` (float) as well as the string form."""
    async with _make_client(lambda r: httpx.Response(200, json={"balance": 100.5})) as http:
        adapter = IpgoldAdapter("test_key", http)
        assert await adapter.get_balance() == 100.5


async def test_ipgold_create_order_happy_path() -> None:
    add_called = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal add_called
        body = _body(request)
        if body["action"] == "services":
            return httpx.Response(200, json=_SERVICES_LIST)
        if body["action"] == "add":
            add_called = True
            assert body["service"] == "1"
            assert body["link"] == "https://t.me/test_canary"
            assert body["quantity"] == "200"
            return httpx.Response(200, json={"order": 9001})
        return httpx.Response(404, json={"error": "unknown"})

    async with _make_client(handler) as http:
        adapter = IpgoldAdapter("test_key", http)
        spec = OrderSpec(
            scenario=Scenario.ACTIVITY_SUBSCRIBE,
            exchange="ipgold",
            target="https://t.me/test_canary",
            quantity=200,
            service_id="1",
            max_cost=50.0,
        )
        external_id, cost = await adapter.create_order(spec, "uuid-1")
        assert external_id == "9001"
        # rate 50 per 1000 → 0.05 per unit; 200 units → 10.00
        assert cost == pytest.approx(10.0)
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
async def test_ipgold_status_map(raw_status: str, expected: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"charge": "5.00", "status": raw_status, "remains": 0, "currency": "RUB"},
        )

    async with _make_client(handler) as http:
        adapter = IpgoldAdapter("test_key", http)
        assert await adapter.get_order_status("9001") == expected


# --- defensive paths ---------------------------------------------------------


async def test_ipgold_refuses_create_when_cost_exceeds_max() -> None:
    """HIGH-c: estimate-first stops over-budget orders before /add is called."""
    add_called = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal add_called
        body = _body(request)
        if body["action"] == "services":
            return httpx.Response(200, json=_SERVICES_LIST)
        if body["action"] == "add":
            add_called = True
            return httpx.Response(200, json={"order": 1})
        return httpx.Response(404)

    async with _make_client(handler) as http:
        adapter = IpgoldAdapter("test_key", http)
        spec = OrderSpec(
            scenario=Scenario.ACTIVITY_SUBSCRIBE,
            exchange="ipgold",
            target="https://t.me/test_canary",
            quantity=10_000,  # 10000 * 0.05 = 500, way over the cap
            service_id="1",
            max_cost=10.0,
        )
        with pytest.raises(ValueError, match=r"exceeds spec\.max_cost"):
            await adapter.create_order(spec, "uuid-1")
        assert not add_called


async def test_ipgold_create_unknown_service_id() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if _body(request)["action"] == "services":
            return httpx.Response(200, json=_SERVICES_LIST)
        return httpx.Response(500)

    async with _make_client(handler) as http:
        adapter = IpgoldAdapter("test_key", http)
        spec = OrderSpec(
            scenario=Scenario.ACTIVITY_SUBSCRIBE,
            exchange="ipgold",
            target="x",
            quantity=10,
            service_id="999",
            max_cost=10.0,
        )
        with pytest.raises(ValueError, match="not in catalogue"):
            await adapter.create_order(spec, "uuid-1")


async def test_ipgold_access_denied_envelope_surfaces_clean_error() -> None:
    """Real IPGold rejects with {status: BAD, errors: [...]}. The adapter
    surfaces the API's own message rather than masking it — and never leaks
    the api_key into the error string.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "status": "BAD",
                "errors": [
                    {"message": "Something is broken", "code": 0},
                    {"message": "Access denied", "code": 403},
                ],
            },
        )

    async with _make_client(handler) as http:
        adapter = IpgoldAdapter("supersecret_ipgold_token", http)
        with pytest.raises(RuntimeError) as exc:
            await adapter.get_balance()
    msg = str(exc.value)
    assert "Access denied" in msg
    assert "code 403" in msg
    assert "supersecret_ipgold_token" not in msg  # security: token not leaked


# --- catalogue filter --------------------------------------------------------


async def test_ipgold_list_services_for_scenario_filters_by_name() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if _body(request)["action"] == "services":
            return httpx.Response(200, json=_SERVICES_LIST)
        return httpx.Response(404)

    async with _make_client(handler) as http:
        adapter = IpgoldAdapter("test_key", http)
        # ACTIVITY_SUBSCRIBE → "подпис..." → "Подписчики Telegram"
        subs = await adapter.list_services_for_scenario(Scenario.ACTIVITY_SUBSCRIBE)
        assert len(subs) == 1
        assert subs[0].service_id == "1"
        assert "Подписчики" in subs[0].name
        # ACTIVITY_LIKE → "лайк..." → "Лайки YouTube"
        likes = await adapter.list_services_for_scenario(Scenario.ACTIVITY_LIKE)
        assert len(likes) == 1
        assert likes[0].service_id == "2"
        # SOCIAL_TRAFFIC → "переход..." → "Переходы..."
        traffic = await adapter.list_services_for_scenario(Scenario.SOCIAL_TRAFFIC)
        assert len(traffic) == 1
        assert traffic[0].service_id == "3"
