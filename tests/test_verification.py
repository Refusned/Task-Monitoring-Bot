"""Tests for verification layer (traffic + activity, mock mode)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from models import Order, OrderSpec, OrderStatus, Scenario, SourcePlatform
from verification.activity import ActivityVerifier
from verification.traffic import TrafficVerifier


def _make_order(
    scenario: Scenario,
    quantity: int = 10,
    source_platform: SourcePlatform | None = None,
) -> Order:
    spec = OrderSpec(
        scenario=scenario,
        exchange="smmcode" if scenario != Scenario.SOCIAL_TRAFFIC else "unu",
        target="https://t.me/test"
        if scenario != Scenario.SOCIAL_TRAFFIC
        else "https://example.com",
        quantity=quantity,
        source_platform=source_platform,
        max_cost=2.0,
    )
    return Order(
        client_order_uuid="test-uuid-1234",
        spec=spec,
        status=OrderStatus.ACTIVE,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )


@pytest.mark.asyncio
async def test_traffic_verifier_mock() -> None:
    verifier = TrafficVerifier(mock=True)
    order = _make_order(Scenario.SOCIAL_TRAFFIC, quantity=5, source_platform=SourcePlatform.VK)
    result = await verifier.verify(order)
    assert result.expected == 5.0
    assert result.verdict in ("auto_pass", "needs_human_review", "fail")
    assert result.raw_evidence["verifier"] == "traffic"
    assert result.raw_evidence["mode"] == "mock"


@pytest.mark.asyncio
async def test_traffic_verifier_wrong_scenario() -> None:
    verifier = TrafficVerifier(mock=True)
    order = _make_order(Scenario.ACTIVITY_SUBSCRIBE)
    result = await verifier.verify(order)
    assert result.verdict == "needs_human_review"
    assert result.reason.startswith("TrafficVerifier can only verify")


@pytest.mark.asyncio
async def test_activity_verifier_mock() -> None:
    verifier = ActivityVerifier(mock=True)
    order = _make_order(Scenario.ACTIVITY_SUBSCRIBE, quantity=20)
    result = await verifier.verify(order)
    assert result.expected == 20.0
    assert result.verdict in ("auto_pass", "needs_human_review", "fail")
    assert result.raw_evidence["verifier"] == "activity"


@pytest.mark.asyncio
async def test_activity_verifier_supports_views() -> None:
    verifier = ActivityVerifier(mock=True)
    order = _make_order(Scenario.ACTIVITY_VIEW, quantity=100)
    result = await verifier.verify(order)
    assert result.expected == 100.0
    assert result.verdict in ("auto_pass", "needs_human_review", "fail")
    assert result.raw_evidence["scenario"] == "activity_view"


@pytest.mark.asyncio
async def test_activity_verifier_wrong_scenario() -> None:
    verifier = ActivityVerifier(mock=True)
    order = _make_order(Scenario.SOCIAL_TRAFFIC, source_platform=SourcePlatform.VK)
    result = await verifier.verify(order)
    assert result.verdict == "needs_human_review"
    assert result.reason.startswith("ActivityVerifier can only verify")
