"""Capability-gate tests for IpgoldAdapter (Day 4 + v4 pivot).

ipgold's API methods aren't publicly confirmed; the adapter declares only the
read-only capabilities needed for smart-selection (the orchestrator + tool
layer route around it for placement). These tests pin the contract:
1. capabilities() excludes CREATE_ORDER + submission verbs so the
   orchestrator refuses placement.
2. Submission/placement methods raise NotImplementedError.
3. No network traffic ever reaches httpx for placement paths.
"""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest

from adapters.base import Capability
from adapters.ipgold import IpgoldAdapter
from models import OrderSpec, Scenario


def _make_client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_ipgold_capabilities_no_writes() -> None:
    async with _make_client(lambda r: httpx.Response(500)) as http:
        adapter = IpgoldAdapter("anything", http)
        caps = adapter.capabilities()
        # Write/placement verbs MUST stay out — those branches are unimplemented.
        assert Capability.CREATE_ORDER not in caps
        assert Capability.ACCEPT_SUBMISSION not in caps
        assert Capability.REJECT_SUBMISSION not in caps
        assert Capability.LIST_SUBMISSIONS not in caps
        # GET_BALANCE also out: ipgold /api/v1 doesn't expose action format publicly.
        assert Capability.GET_BALANCE not in caps
        # Read-only smart-selection verbs are still allowed.
        assert Capability.GET_QUOTE in caps
        assert Capability.GET_TOPUP_INFO in caps


async def test_ipgold_get_balance_returns_none() -> None:
    """ipgold balance unavailable via API → None (dashboard renders 'нет API')."""
    async with _make_client(lambda r: httpx.Response(500)) as http:
        adapter = IpgoldAdapter("", http)
        assert await adapter.get_balance() is None


async def test_ipgold_create_order_raises() -> None:
    async with _make_client(lambda r: httpx.Response(500)) as http:
        adapter = IpgoldAdapter("", http)
        spec = OrderSpec(
            scenario=Scenario.ACTIVITY_SUBSCRIBE,
            exchange="ipgold",
            target="x",
            quantity=1,
            max_cost=1.0,
        )
        with pytest.raises(NotImplementedError, match="ipgold"):
            await adapter.create_order(spec, "uuid-1")


async def test_ipgold_submission_methods_raise() -> None:
    async with _make_client(lambda r: httpx.Response(500)) as http:
        adapter = IpgoldAdapter("", http)
        with pytest.raises(NotImplementedError):
            await adapter.list_submissions("1")
        with pytest.raises(NotImplementedError):
            await adapter.accept_submission("1", "2")
        with pytest.raises(NotImplementedError):
            await adapter.reject_submission("1", "2", "reason")
