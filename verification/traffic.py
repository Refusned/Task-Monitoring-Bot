"""Traffic verifier: checks social-traffic visits via Yandex Metrica.

A10: real Metrica credentials are optional — when missing the verifier falls back to
mock evidence generation so the orchestrator and demo can run without secrets.
"""

from __future__ import annotations

import random

from models import Order, Scenario, VerificationResult, VerificationVerdict
from verification.base import Verifier, _make_result


class TrafficVerifier(Verifier):
    """Verify SOCIAL_TRAFFIC orders.

    Real mode: queries Yandex Metrica Stat API with UTM-filter.
    Mock mode: synthesizes deterministic visit counts for the DRY_RUN demo.
    """

    def __init__(
        self,
        counter_id: str = "",
        oauth_token: str = "",
        *,
        mock: bool = True,
        mock_hit_ratio: float = 0.9,
    ) -> None:
        self._counter_id = counter_id
        self._oauth_token = oauth_token
        self._mock = mock or not (counter_id and oauth_token)
        self._mock_hit_ratio = max(0.0, min(1.0, mock_hit_ratio))

    async def verify(self, order: Order, **kwargs) -> VerificationResult:
        if order.spec.scenario != Scenario.SOCIAL_TRAFFIC:
            return VerificationResult(
                verdict=VerificationVerdict.NEEDS_HUMAN_REVIEW,
                measured=0.0,
                expected=float(order.spec.quantity),
                reason="TrafficVerifier can only verify SOCIAL_TRAFFIC orders",
                raw_evidence={"verifier": "traffic", "error": "wrong_scenario"},
            )

        expected = float(order.spec.quantity)

        if self._mock:
            measured = self._mock_visits(order)
            return _make_result(
                measured=measured,
                expected=expected,
                reason=f"mock metrica: measured {measured:.0f} vs expected {expected:.0f}",
                raw_evidence={
                    "verifier": "traffic",
                    "mode": "mock",
                    "source_platform": (
                        order.spec.source_platform.value if order.spec.source_platform else None
                    ),
                    "target": order.spec.target,
                },
            )

        # TODO: real Metrica call (Day 6+)
        return VerificationResult(
            verdict=VerificationVerdict.NEEDS_HUMAN_REVIEW,
            measured=0.0,
            expected=expected,
            reason="real Metrica not wired yet",
            raw_evidence={"verifier": "traffic", "mode": "real", "status": "not_implemented"},
        )

    def _mock_visits(self, order: Order) -> float:
        """Deterministic-ish mock: most of the time we hit near the target."""
        seed = hash(order.client_order_uuid) % 10000
        rng = random.Random(seed)
        if rng.random() < self._mock_hit_ratio:
            return float(rng.randint(int(order.spec.quantity * 0.85), order.spec.quantity + 2))
        return float(rng.randint(0, int(order.spec.quantity * 0.40)))
