"""Traffic verifier: checks social-traffic visits via Yandex Metrica.

A10: real Metrica credentials are optional — when missing the verifier falls back to
mock evidence generation so the orchestrator and demo can run without secrets.
"""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from models import Order, Scenario, Submission, VerificationResult, VerificationVerdict
from verification.base import Verifier, _make_result

_METRIKA_DATA_URL = "https://api-metrika.yandex.net/stat/v1/data"
_SOURCE_TO_UTM = {
    "vk": "vk",
    "x": "x",
    "youtube": "youtube",
    "telegram": "telegram",
    "dzen": "dzen",
    "pinterest": "pinterest",
}


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
        verification_window_days: int = 7,
    ) -> None:
        self._counter_id = counter_id
        self._oauth_token = oauth_token
        self._mock = mock or not (counter_id and oauth_token)
        self._mock_hit_ratio = max(0.0, min(1.0, mock_hit_ratio))
        self._verification_window_days = max(1, verification_window_days)

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
        submission = kwargs.get("submission")

        if self._mock:
            measured = self._mock_visits(order, submission=submission)
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

        try:
            measured, evidence = await self._query_metrica(order)
        except Exception as exc:
            return VerificationResult(
                verdict=VerificationVerdict.NEEDS_HUMAN_REVIEW,
                measured=0.0,
                expected=expected,
                reason=f"Metrica check failed: {type(exc).__name__}: {exc}",
                raw_evidence={
                    "verifier": "traffic",
                    "mode": "real",
                    "status": "error",
                    "error_type": type(exc).__name__,
                },
            )

        return _make_result(
            measured=measured,
            expected=expected,
            reason=f"metrica: measured {measured:.0f} vs expected {expected:.0f}",
            raw_evidence=evidence,
        )

    def _mock_visits(self, order: Order, submission: Submission | None = None) -> float:
        """Deterministic-ish mock: most of the time we hit near the target."""
        if submission is not None and submission.evidence == "good":
            return float(order.spec.quantity)
        if submission is not None and submission.evidence == "weak":
            return float(max(0, int(order.spec.quantity * 0.2)))

        seed = hash(order.client_order_uuid) % 10000
        rng = random.Random(seed)
        if rng.random() < self._mock_hit_ratio:
            return float(rng.randint(int(order.spec.quantity * 0.85), order.spec.quantity + 2))
        return float(rng.randint(0, int(order.spec.quantity * 0.40)))

    async def _query_metrica(self, order: Order) -> tuple[float, dict[str, Any]]:
        date2 = datetime.now(UTC).date()
        date1 = date2 - timedelta(days=self._verification_window_days - 1)
        filters = self._build_filters(order)
        params = {
            "ids": self._counter_id,
            "metrics": "ym:s:visits",
            "date1": date1.isoformat(),
            "date2": date2.isoformat(),
            "accuracy": "full",
            "attribution": "lastsign",
        }
        if filters:
            params["filters"] = filters

        headers = {"Authorization": f"OAuth {self._oauth_token}"}
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(_METRIKA_DATA_URL, params=params, headers=headers)
            response.raise_for_status()
            payload = response.json()

        totals = payload.get("totals") or []
        measured = float(totals[0]) if totals else 0.0
        return measured, {
            "verifier": "traffic",
            "mode": "real",
            "status": "ok",
            "counter_id": self._counter_id,
            "date1": date1.isoformat(),
            "date2": date2.isoformat(),
            "filters": filters,
            "contains_sensitive_data": payload.get("contains_sensitive_data"),
        }

    def _build_filters(self, order: Order) -> str:
        filters = [
            "ym:s:<attribution>TrafficSource=='social'",
            f"ym:pv:URL=@'{_escape_filter_value(order.spec.target)}'",
        ]
        if order.spec.source_platform is not None:
            source = _SOURCE_TO_UTM.get(order.spec.source_platform.value)
            if source:
                filters.append(f"ym:s:<attribution>UTMSource=='{_escape_filter_value(source)}'")
        return " AND ".join(filters)


def _escape_filter_value(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")
