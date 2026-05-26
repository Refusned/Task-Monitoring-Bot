"""Contract tests for IpgoldAdapter — Advertiser API (v1, JSON, campaign-based).

Source of truth: IPGold OpenAPI 1.1.7 spec.
- Endpoint:  POST https://ipgold.ru/api/v1/<action>
- Body:      JSON `{"key": ..., "action": ..., ...params}`
- Success:   `{"status": "OK", "results": ...}`
- Errors:    `{"status": "BAD", "errors": [{message, code}]}` on HTTP 200/400/403/429
"""

from __future__ import annotations

import json as jsonlib
from collections.abc import Callable

import httpx
import pytest

from adapters.base import Capability, PanelAdapter, TaskExchangeAdapter
from adapters.ipgold import IpgoldAdapter
from models import OrderSpec, Scenario, SourcePlatform


def _make_client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _body(request: httpx.Request) -> dict:
    return jsonlib.loads(request.content.decode())


_CAMPAIGN_TYPES = [
    {
        "id": 24,
        "name": "simple site visit",
        "price": "0.00015",
        "currency": "usd",
        "category": "Site visits",
        "regularity_variants": [],
    },
    {
        "id": 7,
        "name": "subscribe to Telegram channel",
        "price": "0.05",
        "currency": "rub",
        "category": "Social subscriptions",
        "regularity_variants": [],
    },
    {
        "id": 11,
        "name": "YouTube like",
        "price": "0.02",
        "currency": "rub",
        "category": "Social likes",
        "regularity_variants": [],
    },
]


# --- shape + capabilities ----------------------------------------------------


async def test_ipgold_is_panel_adapter_not_task_exchange() -> None:
    """ipgold campaign lifecycle is prepaid, no per-submission accept/reject."""
    async with _make_client(lambda r: httpx.Response(500)) as http:
        adapter = IpgoldAdapter("test_key", http)
    assert isinstance(adapter, PanelAdapter)
    assert not isinstance(adapter, TaskExchangeAdapter)


async def test_ipgold_capabilities_no_balance() -> None:
    """IPGold has no account-level balance endpoint → GET_BALANCE intentionally
    absent so `/balance` shows the honest 'не отдаётся API' line.
    """
    async with _make_client(lambda r: httpx.Response(500)) as http:
        adapter = IpgoldAdapter("test_key", http)
        caps = adapter.capabilities()
    assert Capability.CREATE_ORDER in caps
    assert Capability.GET_ORDER_STATUS in caps
    assert Capability.GET_BALANCE not in caps
    assert Capability.LIST_SUBMISSIONS not in caps
    assert Capability.ACCEPT_SUBMISSION not in caps


async def test_ipgold_requires_non_empty_key() -> None:
    async with _make_client(lambda r: httpx.Response(500)) as http:
        with pytest.raises(ValueError, match="api_key"):
            IpgoldAdapter("", http)


async def test_ipgold_get_balance_raises_not_implemented() -> None:
    """No account balance endpoint exists; honest NotImplementedError."""
    async with _make_client(lambda r: httpx.Response(500)) as http:
        adapter = IpgoldAdapter("test_key", http)
        with pytest.raises(NotImplementedError, match="account-level balance"):
            await adapter.get_balance()


# --- create_order = create_campaign + refill_campaign -----------------------


async def test_ipgold_create_order_two_phase_flow() -> None:
    """`create_order` ⇒ get_campaign_types ⇒ create_campaign ⇒ refill_campaign.
    Verify the request bodies + URLs + that external_order_id encodes both ids.
    """
    seen: list[tuple[str, dict]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = _body(request)
        assert body["key"] == "test_key"
        action = body["action"]
        path = request.url.path
        seen.append((action, body))
        if action == "get_campaign_types":
            assert path == "/api/v1/get_campaign_types"
            return httpx.Response(200, json={"status": "OK", "results": _CAMPAIGN_TYPES})
        if action == "create_campaign":
            assert path == "/api/v1/create_campaign"
            assert body["type_id"] == 24
            assert body["url"] == "https://example.com/landing"
            assert body["actions_per_day"] == 200
            return httpx.Response(
                200, json={"status": "OK", "results": {"id": 555111, "type_id": 24}}
            )
        if action == "refill_campaign":
            assert path == "/api/v1/refill_campaign"
            assert body["type_id"] == 24
            assert body["id"] == 555111
            assert body["actions_number"] == 200
            return httpx.Response(
                200, json={"status": "OK", "results": {"added": 200, "campaign_balance": 200}}
            )
        return httpx.Response(
            404, json={"status": "BAD", "errors": [{"message": "Unknown action"}]}
        )

    async with _make_client(handler) as http:
        adapter = IpgoldAdapter("test_key", http)
        spec = OrderSpec(
            scenario=Scenario.SOCIAL_TRAFFIC,
            exchange="ipgold",
            target="https://example.com/landing",
            quantity=200,
            service_id="24",
            max_cost=1.0,
            source_platform=SourcePlatform.VK,
        )
        external_id, cost = await adapter.create_order(spec, "uuid-1")
    # external_id encodes both type_id and campaign id for later status checks.
    assert external_id == "24:555111"
    # type 24 price 0.00015 × 200 = 0.03
    assert cost == pytest.approx(0.03)
    # Order of calls: types → create → refill.
    actions_called = [a for a, _ in seen]
    assert actions_called == ["get_campaign_types", "create_campaign", "refill_campaign"]


async def test_ipgold_estimate_first_rejects_over_budget_before_create() -> None:
    """HIGH-c: when cost exceeds spec.max_cost, neither create_campaign nor
    refill_campaign is touched. The whole point: NO money risk.
    """
    create_called = False
    refill_called = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal create_called, refill_called
        action = _body(request)["action"]
        if action == "get_campaign_types":
            return httpx.Response(200, json={"status": "OK", "results": _CAMPAIGN_TYPES})
        if action == "create_campaign":
            create_called = True
        if action == "refill_campaign":
            refill_called = True
        return httpx.Response(200, json={"status": "OK", "results": {"id": 999, "type_id": 7}})

    async with _make_client(handler) as http:
        adapter = IpgoldAdapter("test_key", http)
        spec = OrderSpec(
            scenario=Scenario.ACTIVITY_SUBSCRIBE,
            exchange="ipgold",
            target="https://t.me/x",
            quantity=100,  # 100 × 0.05 = 5.0
            service_id="7",
            max_cost=1.0,
        )
        with pytest.raises(ValueError, match=r"exceeds spec\.max_cost"):
            await adapter.create_order(spec, "uuid-1")
        assert not create_called
        assert not refill_called


async def test_ipgold_create_order_unknown_type_id() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if _body(request)["action"] == "get_campaign_types":
            return httpx.Response(200, json={"status": "OK", "results": _CAMPAIGN_TYPES})
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
        with pytest.raises(ValueError, match="not in campaign-types catalogue"):
            await adapter.create_order(spec, "uuid-1")


async def test_ipgold_create_order_requires_numeric_service_id() -> None:
    async with _make_client(lambda r: httpx.Response(500)) as http:
        adapter = IpgoldAdapter("test_key", http)
        spec = OrderSpec(
            scenario=Scenario.ACTIVITY_SUBSCRIBE,
            exchange="ipgold",
            target="x",
            quantity=10,
            service_id="not-a-number",
            max_cost=10.0,
        )
        with pytest.raises(ValueError, match="numeric"):
            await adapter.create_order(spec, "uuid-1")


async def test_ipgold_create_succeeds_but_refill_fails_surfaces_orphan() -> None:
    """If step 2 (refill) fails after step 1 (create) succeeded, the campaign
    is orphan on IPGold's side. The adapter MUST raise with the campaign id
    in the message so an admin can recover from the web cabinet.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        action = _body(request)["action"]
        if action == "get_campaign_types":
            return httpx.Response(200, json={"status": "OK", "results": _CAMPAIGN_TYPES})
        if action == "create_campaign":
            return httpx.Response(
                200, json={"status": "OK", "results": {"id": 7777, "type_id": 24}}
            )
        if action == "refill_campaign":
            return httpx.Response(
                400,
                json={
                    "status": "BAD",
                    "errors": [{"message": "Insufficient funds", "code": 42}],
                },
            )
        return httpx.Response(404)

    async with _make_client(handler) as http:
        adapter = IpgoldAdapter("test_key", http)
        spec = OrderSpec(
            scenario=Scenario.SOCIAL_TRAFFIC,
            exchange="ipgold",
            target="https://example.com",
            quantity=10,
            service_id="24",
            max_cost=1.0,
            source_platform=SourcePlatform.VK,
        )
        with pytest.raises(RuntimeError) as exc:
            await adapter.create_order(spec, "uuid-1")
    msg = str(exc.value)
    assert "7777" in msg  # campaign id surfaces for manual recovery
    assert "Insufficient funds" in msg


# --- get_order_status --------------------------------------------------------


@pytest.mark.parametrize(
    "info, expected",
    [
        ({"moderation_status": "reject", "status": "start", "balance": 100}, "failed"),
        ({"moderation_status": "wait", "status": "stop", "balance": 0}, "in_progress"),
        ({"moderation_status": "success", "status": "stop", "balance": 50}, "failed"),
        ({"moderation_status": "success", "status": "start", "balance": 100}, "in_progress"),
        ({"moderation_status": "success", "status": "start", "balance": 0}, "completed"),
    ],
)
async def test_ipgold_status_mapping(info: dict, expected: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = _body(request)
        assert body["action"] == "get_campaign_info"
        assert body["type_id"] == 24
        assert body["id"] == 555111
        return httpx.Response(200, json={"status": "OK", "results": info})

    async with _make_client(handler) as http:
        adapter = IpgoldAdapter("test_key", http)
        assert await adapter.get_order_status("24:555111") == expected


async def test_ipgold_status_malformed_external_id_is_failed() -> None:
    """If external_order_id isn't in the `type_id:id` shape, treat as failed
    (we can't recover; surface to caller cleanly)."""
    async with _make_client(lambda r: httpx.Response(500)) as http:
        adapter = IpgoldAdapter("test_key", http)
        assert await adapter.get_order_status("just-a-number") == "failed"
        assert await adapter.get_order_status("a:b") == "failed"


# --- catalogue filter --------------------------------------------------------


async def test_ipgold_list_services_for_scenario_filters_correctly() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if _body(request)["action"] == "get_campaign_types":
            return httpx.Response(200, json={"status": "OK", "results": _CAMPAIGN_TYPES})
        return httpx.Response(404)

    async with _make_client(handler) as http:
        adapter = IpgoldAdapter("test_key", http)
        # ACTIVITY_SUBSCRIBE → "subscribe" / "подпис" → type 7
        subs = await adapter.list_services_for_scenario(Scenario.ACTIVITY_SUBSCRIBE)
        assert [s.service_id for s in subs] == ["7"]
        # ACTIVITY_LIKE → "like" / "лайк" → type 11
        likes = await adapter.list_services_for_scenario(Scenario.ACTIVITY_LIKE)
        assert [s.service_id for s in likes] == ["11"]
        # SOCIAL_TRAFFIC → "visit" / "трафик" → type 24 (simple site visit)
        traffic = await adapter.list_services_for_scenario(Scenario.SOCIAL_TRAFFIC)
        assert [s.service_id for s in traffic] == ["24"]


# --- error envelope handling -------------------------------------------------


async def test_ipgold_access_denied_envelope_surfaces_clean_error() -> None:
    """Real IPGold returns {status: BAD, errors: [...]} on HTTP 403 when the
    token is invalid or API isn't activated. Surface the message verbatim;
    DON'T leak the api key.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403,
            json={
                "status": "BAD",
                "errors": [{"message": "Access denied", "code": 403}],
            },
        )

    async with _make_client(handler) as http:
        adapter = IpgoldAdapter("supersecret_ipgold_token", http)
        with pytest.raises(RuntimeError) as exc:
            await adapter._get_campaign_types()
    msg = str(exc.value)
    assert "Access denied" in msg
    assert "code 403" in msg
    assert "supersecret_ipgold_token" not in msg


async def test_ipgold_action_not_specified_envelope() -> None:
    """API returns BAD/code 4 on missing action — we surface it cleanly."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={
                "status": "BAD",
                "errors": [{"message": "Action is not specified", "code": 4}],
            },
        )

    async with _make_client(handler) as http:
        adapter = IpgoldAdapter("test_key", http)
        with pytest.raises(RuntimeError) as exc:
            await adapter._get_campaign_types()
    assert "Action is not specified" in str(exc.value)


async def test_ipgold_rate_limit_429_envelope() -> None:
    """API returns BAD/code 429 on rate limits — surfaces cleanly."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            json={
                "status": "BAD",
                "errors": [{"message": "You have exceeded the request limit", "code": 429}],
            },
        )

    async with _make_client(handler) as http:
        adapter = IpgoldAdapter("test_key", http)
        with pytest.raises(RuntimeError) as exc:
            await adapter._get_campaign_types()
    assert "exceeded the request limit" in str(exc.value)


async def test_ipgold_http_5xx_non_json_falls_back_to_http_error() -> None:
    """Non-JSON 5xx body → fallback to httpx HTTP error."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="<html>Internal Server Error</html>")

    async with _make_client(handler) as http:
        adapter = IpgoldAdapter("test_key", http)
        with pytest.raises(Exception) as exc:
            await adapter._get_campaign_types()
    assert "500" in str(exc.value) or "Server error" in str(exc.value)
