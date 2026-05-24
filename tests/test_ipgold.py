"""Contract tests for IpgoldAdapter (Day 4).

The ipgold adapter is provisional — real API endpoints are not confirmed.
Tests verify that it raises RuntimeError in live mode and exposes the correct
capabilities, plus the HIGH-c max_cost safety net.
"""

from __future__ import annotations

import httpx
import pytest

from adapters.base import Capability
from adapters.ipgold import IpgoldAdapter
from models import OrderSpec, Scenario


def _make_client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_ipgold_get_balance_warns_and_returns_zero() -> None:
    with pytest.warns(UserWarning, match="not yet confirmed"):
        async with _make_client(lambda r: httpx.Response(200, json={"balance": 0})) as http:
            adapter = IpgoldAdapter("test_key", http)
            assert await adapter.get_balance() == 0.0


async def test_ipgold_create_order_raises_in_live_mode() -> None:
    async with _make_client(lambda r: httpx.Response(200, json={"order_id": 1})) as http:
        adapter = IpgoldAdapter("test_key", http)
        spec = OrderSpec(
            scenario=Scenario.ACTIVITY_SUBSCRIBE,
            exchange="ipgold",
            target="https://t.me/x",
            quantity=10,
            max_cost=200.0,
        )
        with pytest.raises(RuntimeError, match="provisional"):
            await adapter.create_order(spec, "uuid-1")


async def test_ipgold_create_order_enforces_max_cost_before_provisional_error() -> None:
    """HIGH-c invariant: max_cost check must run before we ever touch the exchange."""
    async with _make_client(lambda r: httpx.Response(200, json={"order_id": 1})) as http:
        adapter = IpgoldAdapter("test_key", http)
        spec = OrderSpec(
            scenario=Scenario.ACTIVITY_SUBSCRIBE,
            exchange="ipgold",
            target="https://t.me/x",
            quantity=1000,  # 1000 * 5.0 = 5000
            max_cost=1.0,
        )
        with pytest.raises(ValueError, match=r"exceeds spec\.max_cost"):
            await adapter.create_order(spec, "uuid-1")


async def test_ipgold_get_order_status_raises() -> None:
    async with _make_client(lambda r: httpx.Response(200, json={"status": "ok"})) as http:
        adapter = IpgoldAdapter("test_key", http)
        with pytest.raises(RuntimeError, match="provisional"):
            await adapter.get_order_status("1")


async def test_ipgold_list_submissions_raises() -> None:
    async with _make_client(lambda r: httpx.Response(200, json={"jobs": []})) as http:
        adapter = IpgoldAdapter("test_key", http)
        with pytest.raises(RuntimeError, match="provisional"):
            await adapter.list_submissions("1")


async def test_ipgold_accept_submission_raises() -> None:
    async with _make_client(lambda r: httpx.Response(200, json={"ok": True})) as http:
        adapter = IpgoldAdapter("test_key", http)
        with pytest.raises(RuntimeError, match="provisional"):
            await adapter.accept_submission("1", "2")


async def test_ipgold_reject_submission_raises() -> None:
    async with _make_client(lambda r: httpx.Response(200, json={"ok": True})) as http:
        adapter = IpgoldAdapter("test_key", http)
        with pytest.raises(RuntimeError, match="provisional"):
            await adapter.reject_submission("1", "2", "reason")


async def test_ipgold_requires_non_empty_key() -> None:
    async with _make_client(lambda r: httpx.Response(500)) as http:
        with pytest.raises(ValueError, match="api_key"):
            IpgoldAdapter("", http)


async def test_ipgold_capabilities_task_exchange() -> None:
    async with _make_client(lambda r: httpx.Response(500)) as http:
        adapter = IpgoldAdapter("test_key", http)
        caps = adapter.capabilities()
        assert Capability.CREATE_ORDER in caps
        assert Capability.GET_BALANCE in caps
        assert Capability.LIST_SUBMISSIONS in caps
        assert Capability.ACCEPT_SUBMISSION in caps
        assert Capability.REJECT_SUBMISSION in caps
        assert Capability.SUPPORTS_CLIENT_ORDER_ID not in caps
