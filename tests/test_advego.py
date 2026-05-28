"""Contract tests for AdvegoAdapter (Day 4).

advego uses XML-RPC over HTTPS POST. Tests use `httpx.MockTransport` and assert
on the parsed XML-RPC envelope (methodName + params), then return a synthesized
XML-RPC `<methodResponse>` body via `xmlrpc.client.dumps`. No real network.
"""

from __future__ import annotations

import xmlrpc.client
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from adapters.advego import AdvegoAdapter
from adapters.base import Capability
from models import OrderSpec, Scenario, SourcePlatform


def _make_client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _parse_call(request: httpx.Request) -> tuple[str, list[Any]]:
    """Return (methodName, params) from an XML-RPC request."""
    params, method = xmlrpc.client.loads(request.content)
    return method or "", list(params)


def _rpc_response(payload: Any) -> bytes:
    """Encode a return value as an XML-RPC `<methodResponse>` body."""
    return xmlrpc.client.dumps((payload,), methodresponse=True, allow_none=True).encode("utf-8")


# --- happy paths --------------------------------------------------------------


async def test_advego_get_balance_returns_none() -> None:
    """advego XML-RPC has no public balance method (confirmed 2026-05-28).
    Adapter returns None to signal 'no API' instead of faking a number."""
    network_calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        network_calls.append(str(request.url))
        return httpx.Response(500)  # should not be called

    async with _make_client(handler) as http:
        adapter = AdvegoAdapter("test_token", http)
        assert await adapter.get_balance() is None
        # Important: no_api adapters MUST NOT hit the network for balance.
        assert network_calls == []


async def test_advego_create_order_happy_path() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        method, params = _parse_call(request)
        seen["method"] = method
        seen["params"] = params
        assert method == "advego.addOrder"
        body = params[0]
        assert body["token"] == "test_token"
        assert body["campaign_id"] == 555
        assert body["order_type"] == 18
        assert body["jobs_total"] == 20
        # per-job cost = max_cost / quantity = 20.0 / 20 = 1.0
        assert body["cost"] == pytest.approx(1.0)
        return httpx.Response(200, content=_rpc_response({"order_id": 9001}))

    async with _make_client(handler) as http:
        adapter = AdvegoAdapter("test_token", http, default_campaign_id=555)
        spec = OrderSpec(
            scenario=Scenario.ACTIVITY_LIKE,
            exchange="advego",
            target="https://vk.com/wall1_2",
            quantity=20,
            max_cost=20.0,
        )
        external_id, cost = await adapter.create_order(spec, "uuid-1")
        assert external_id == "9001"
        assert cost == pytest.approx(20.0)
    assert seen["method"] == "advego.addOrder"


async def test_advego_create_order_requires_campaign_id() -> None:
    async with _make_client(lambda r: httpx.Response(500)) as http:
        adapter = AdvegoAdapter("test_token", http)  # no default_campaign_id
        spec = OrderSpec(
            scenario=Scenario.ACTIVITY_LIKE,
            exchange="advego",
            target="x",
            quantity=10,
            max_cost=10.0,
        )
        with pytest.raises(ValueError, match="default_campaign_id"):
            await adapter.create_order(spec, "uuid-1")


async def test_advego_create_order_service_id_overrides_order_type() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        _, params = _parse_call(request)
        assert params[0]["order_type"] == 6
        return httpx.Response(200, content=_rpc_response({"order_id": 9002}))

    async with _make_client(handler) as http:
        adapter = AdvegoAdapter("test_token", http, default_campaign_id=555)
        spec = OrderSpec(
            scenario=Scenario.SOCIAL_TRAFFIC,
            exchange="advego",
            target="https://example.com",
            quantity=10,
            service_id="6",
            source_platform=SourcePlatform.VK,
            max_cost=10.0,
        )
        external_id, _ = await adapter.create_order(spec, "uuid-1")
    assert external_id == "9002"


@pytest.mark.parametrize(
    "raw_state, expected",
    [
        ("active", "in_progress"),
        ("pending", "in_progress"),
        ("moderation", "in_progress"),
        ("completed", "completed"),
        ("finished", "completed"),
        ("stopped", "failed"),
        ("cancelled", "failed"),
        ("rejected", "failed"),
        ("error", "failed"),
        ("ZZZ-unknown", "failed"),  # conservative default
    ],
)
async def test_advego_order_state_map(raw_state: str, expected: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        method, params = _parse_call(request)
        assert method == "advego.ordersGetState"
        oid = params[0]["orders"][0]
        # XML-RPC struct keys are strings on the wire — encode that way.
        return httpx.Response(200, content=_rpc_response({str(oid): {"state": raw_state}}))

    async with _make_client(handler) as http:
        adapter = AdvegoAdapter("test_token", http)
        assert await adapter.get_order_status("9001") == expected


async def test_advego_list_submissions() -> None:
    jobs = [
        {"id": 7001, "worker_id": 11, "report": "Done: https://i.imgur.com/x.png"},
        {"id": 7002, "worker_id": 12, "comment": "Posted yesterday"},
        # malformed entry: ignored, must not crash.
        {"worker_id": 99},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        method, params = _parse_call(request)
        assert method == "advego.getJobsCompleted"
        assert params[0]["id_order"] == 9001
        assert params[0]["date_type"] == "complete"
        return httpx.Response(200, content=_rpc_response(jobs))

    async with _make_client(handler) as http:
        adapter = AdvegoAdapter("test_token", http)
        subs = await adapter.list_submissions("9001")
    assert [s.external_submission_id for s in subs] == ["7001", "7002"]
    assert "imgur" in (subs[0].evidence or "")
    assert subs[1].evidence == "Posted yesterday"


async def test_advego_accept_job() -> None:
    seen: list[tuple[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        method, params = _parse_call(request)
        seen.append((method, params[0]))
        return httpx.Response(200, content=_rpc_response({"success": 1}))

    async with _make_client(handler) as http:
        adapter = AdvegoAdapter("test_token", http)
        await adapter.accept_submission("9001", "7001")
    assert seen[0][0] == "advego.acceptJob"
    assert seen[0][1] == {"token": "test_token", "ID": 7001}


async def test_advego_reject_pads_short_comment() -> None:
    """advego requires comment ≥10 chars; the adapter pads short reasons."""
    seen: list[Any] = []

    def handler(request: httpx.Request) -> httpx.Response:
        _, params = _parse_call(request)
        seen.append(params[0])
        return httpx.Response(200, content=_rpc_response({"success": 1}))

    async with _make_client(handler) as http:
        adapter = AdvegoAdapter("test_token", http)
        await adapter.reject_submission("9001", "7002", "bad")
    body = seen[0]
    assert body["ID"] == 7002
    assert len(body["comment"]) >= 10
    assert "bad" in body["comment"]


async def test_advego_reject_keeps_long_comment() -> None:
    seen: list[Any] = []

    def handler(request: httpx.Request) -> httpx.Response:
        _, params = _parse_call(request)
        seen.append(params[0])
        return httpx.Response(200, content=_rpc_response({"success": 1}))

    async with _make_client(handler) as http:
        adapter = AdvegoAdapter("test_token", http)
        long_reason = "Evidence URL returns 404 in browser; please re-upload screenshot."
        await adapter.reject_submission("9001", "7002", long_reason)
    assert seen[0]["comment"] == long_reason


# --- defensive paths ---------------------------------------------------------


async def test_advego_requires_non_empty_token() -> None:
    async with _make_client(lambda r: httpx.Response(500)) as http:
        with pytest.raises(ValueError, match="token"):
            AdvegoAdapter("", http)


async def test_advego_fault_raises_runtime_error() -> None:
    """Fault handling moved from get_balance (which no longer hits API) to
    get_order_status — same code path through _call wrapper."""
    fault_body = xmlrpc.client.dumps(
        xmlrpc.client.Fault(401, "Invalid token"), methodresponse=True
    ).encode("utf-8")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=fault_body)

    async with _make_client(handler) as http:
        adapter = AdvegoAdapter("test_token", http)
        with pytest.raises(RuntimeError, match=r"fault.*Invalid token"):
            await adapter.get_order_status("123")


async def test_advego_capabilities_task_exchange() -> None:
    async with _make_client(lambda r: httpx.Response(500)) as http:
        adapter = AdvegoAdapter("test_token", http)
        caps = adapter.capabilities()
    assert Capability.CREATE_ORDER in caps
    assert Capability.LIST_SUBMISSIONS in caps
    assert Capability.ACCEPT_SUBMISSION in caps
    assert Capability.REJECT_SUBMISSION in caps
    # GET_BALANCE intentionally absent: advego API has no balance method.
    assert Capability.GET_BALANCE not in caps
    assert Capability.SUPPORTS_CLIENT_ORDER_ID not in caps
