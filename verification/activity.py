"""Activity verifier: checks subscribe / like / view completion.

A4: independent check, not trusting exchange status.
For MVP we use deterministic mock evidence (public counters are hard to query
without auth for every platform). Real implementation = platform-specific scrapers
or API calls (Day 6+).
"""

from __future__ import annotations

import random

from models import Order, Scenario, Submission, VerificationResult, VerificationVerdict
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
    ) -> None:
        self._mock = mock
        self._mock_hit_ratio = max(0.0, min(1.0, mock_hit_ratio))

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

        # TODO: real platform APIs / scraping (Day 6+)
        return VerificationResult(
            verdict=VerificationVerdict.NEEDS_HUMAN_REVIEW,
            measured=0.0,
            expected=expected,
            reason="real activity check not wired yet",
            raw_evidence={"verifier": "activity", "mode": "real", "status": "not_implemented"},
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
