"""Tests for verification layer (traffic + activity, mock mode)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from models import Order, OrderSpec, OrderStatus, Scenario, SourcePlatform
from verification.activity import ActivityVerifier
from verification.activity_metrics import ActivityMetricSnapshot, ActivityMetricsProvider
from verification.traffic import TrafficVerifier


class _FakeActivityMetricsProvider(ActivityMetricsProvider):
    async def measure(self, target: str, scenario: Scenario) -> ActivityMetricSnapshot | None:
        assert target
        assert scenario
        return ActivityMetricSnapshot(
            metric="likeCount",
            count=1150,
            source="test",
            raw_evidence={"provider": "fake"},
        )


def _make_order(
    scenario: Scenario,
    quantity: int = 10,
    source_platform: SourcePlatform | None = None,
    target: str | None = None,
    baseline_count: int | None = None,
    baseline_metric: str | None = None,
    baseline_source: str | None = None,
) -> Order:
    spec = OrderSpec(
        scenario=scenario,
        exchange="smmcode" if scenario != Scenario.SOCIAL_TRAFFIC else "unu",
        target=target
        or ("https://t.me/test" if scenario != Scenario.SOCIAL_TRAFFIC else "https://example.com"),
        quantity=quantity,
        source_platform=source_platform,
        max_cost=2.0,
        baseline_count=baseline_count,
        baseline_metric=baseline_metric,
        baseline_source=baseline_source,
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
async def test_activity_verifier_real_mode_uses_baseline_delta() -> None:
    verifier = ActivityVerifier(mock=False, metrics_provider=_FakeActivityMetricsProvider())
    order = _make_order(
        Scenario.ACTIVITY_LIKE,
        quantity=100,
        target="https://youtube.com/watch?v=dQw4w9WgXcQ",
        baseline_count=1000,
        baseline_metric="likeCount",
        baseline_source="test",
    )

    result = await verifier.verify(order)

    assert result.verdict == "auto_pass"
    assert result.measured == 150.0
    assert result.expected == 100.0
    assert result.raw_evidence["baseline_count"] == 1000
    assert result.raw_evidence["current_count"] == 1150
    assert result.raw_evidence["provider"] == "fake"


@pytest.mark.asyncio
async def test_activity_verifier_wrong_scenario() -> None:
    verifier = ActivityVerifier(mock=True)
    order = _make_order(Scenario.SOCIAL_TRAFFIC, source_platform=SourcePlatform.VK)
    result = await verifier.verify(order)
    assert result.verdict == "needs_human_review"
    assert result.reason.startswith("ActivityVerifier can only verify")
