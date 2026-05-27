"""Activity verifier: checks subscribe / like / view completion."""

from __future__ import annotations

import random

from models import Order, Scenario, Submission, VerificationResult, VerificationVerdict
from verification.activity_metrics import (
    ActivityMetricsProvider,
    build_activity_metrics_provider,
)
from verification.base import Verifier, _make_result


class ActivityVerifier(Verifier):
    """Verify ACTIVITY_SUBSCRIBE / ACTIVITY_LIKE / ACTIVITY_VIEW orders.

    Mock mode (default): synthesises completion counts.
    """

    def __init__(
        self,
        *,
        mock: bool = True,
        mock_hit_ratio: float = 0.9,
        metrics_provider: ActivityMetricsProvider | None = None,
        youtube_api_key: str = "",
    ) -> None:
        self._mock = mock
        self._mock_hit_ratio = max(0.0, min(1.0, mock_hit_ratio))
        self._metrics_provider = metrics_provider or build_activity_metrics_provider(
            youtube_api_key=youtube_api_key
        )

    async def verify(self, order: Order, **kwargs) -> VerificationResult:
        if order.spec.scenario not in (
            Scenario.ACTIVITY_SUBSCRIBE,
            Scenario.ACTIVITY_LIKE,
            Scenario.ACTIVITY_VIEW,
        ):
            return VerificationResult(
                verdict=VerificationVerdict.NEEDS_HUMAN_REVIEW,
                measured=0.0,
                expected=float(order.spec.quantity),
                reason="ActivityVerifier can only verify ACTIVITY orders",
                raw_evidence={"verifier": "activity", "error": "wrong_scenario"},
            )

        expected = float(order.spec.quantity)
        submission = kwargs.get("submission")

        if self._mock:
            measured = self._mock_delta(order, submission=submission)
            return _make_result(
                measured=measured,
                expected=expected,
                reason=f"mock activity: measured {measured:.0f} vs expected {expected:.0f}",
                raw_evidence={
                    "verifier": "activity",
                    "mode": "mock",
                    "scenario": order.spec.scenario.value,
                    "target": order.spec.target,
                },
            )

        if order.spec.baseline_count is None:
            return VerificationResult(
                verdict=VerificationVerdict.NEEDS_HUMAN_REVIEW,
                measured=0.0,
                expected=expected,
                reason="activity baseline is missing; cannot prove delivered delta",
                raw_evidence={
                    "verifier": "activity",
                    "mode": "real",
                    "status": "missing_baseline",
                    "scenario": order.spec.scenario.value,
                    "target": order.spec.target,
                },
            )

        try:
            snapshot = await self._metrics_provider.measure(order.spec.target, order.spec.scenario)
        except Exception as exc:
            return VerificationResult(
                verdict=VerificationVerdict.NEEDS_HUMAN_REVIEW,
                measured=0.0,
                expected=expected,
                reason=f"activity check failed: {type(exc).__name__}: {exc}",
                raw_evidence={
                    "verifier": "activity",
                    "mode": "real",
                    "status": "error",
                    "error_type": type(exc).__name__,
                    "scenario": order.spec.scenario.value,
                    "target": order.spec.target,
                },
            )

        if snapshot is None:
            return VerificationResult(
                verdict=VerificationVerdict.NEEDS_HUMAN_REVIEW,
                measured=0.0,
                expected=expected,
                reason="no activity metrics provider supports this target",
                raw_evidence={
                    "verifier": "activity",
                    "mode": "real",
                    "status": "unsupported_target",
                    "scenario": order.spec.scenario.value,
                    "target": order.spec.target,
                },
            )

        measured_delta = max(0, snapshot.count - order.spec.baseline_count)
        return _make_result(
            measured=float(measured_delta),
            expected=expected,
            reason=(
                f"{snapshot.source}: {snapshot.metric} delta {measured_delta} "
                f"(baseline {order.spec.baseline_count} -> current {snapshot.count})"
            ),
            raw_evidence={
                "verifier": "activity",
                "mode": "real",
                "status": "ok",
                "scenario": order.spec.scenario.value,
                "target": order.spec.target,
                "baseline_count": order.spec.baseline_count,
                "current_count": snapshot.count,
                "metric": snapshot.metric,
                "source": snapshot.source,
                **snapshot.raw_evidence,
            },
        )

    def _mock_delta(self, order: Order, submission: Submission | None = None) -> float:
        if submission is not None and submission.evidence == "good":
            return float(order.spec.quantity)
        if submission is not None and submission.evidence == "weak":
            return float(max(0, int(order.spec.quantity * 0.2)))

        seed = hash(order.client_order_uuid) % 10000
        rng = random.Random(seed)
        if rng.random() < self._mock_hit_ratio:
            return float(rng.randint(int(order.spec.quantity * 0.88), order.spec.quantity + 3))
        return float(rng.randint(0, int(order.spec.quantity * 0.35)))
