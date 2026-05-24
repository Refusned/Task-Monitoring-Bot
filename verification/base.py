"""Verification base class and result types.

Two verifiers implement independent checks (A4):
- TrafficVerifier: counts from Yandex Metrica Stat API (or mock mode).
- ActivityVerifier: delta on public counters (followers / likes).

Both produce a VerificationResult with verdict auto_pass / needs_human_review / fail.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from models import Order, VerificationResult, VerificationVerdict


class Verifier(ABC):
    """Abstract verifier; one per scenario family."""

    @abstractmethod
    async def verify(self, order: Order, **kwargs: Any) -> VerificationResult: ...


def _make_result(
    measured: float,
    expected: float,
    reason: str,
    raw_evidence: dict,
    auto_pass_threshold_ratio: float = 0.85,
    fail_threshold_ratio: float = 0.30,
) -> VerificationResult:
    """Map measured / expected into a verdict.

    thresholds are config-driven via Settings; defaults provided here.
    """
    if expected <= 0:
        ratio = 1.0 if measured > 0 else 0.0
    else:
        ratio = measured / expected

    if ratio >= auto_pass_threshold_ratio:
        verdict = VerificationVerdict.AUTO_PASS
    elif ratio >= fail_threshold_ratio:
        verdict = VerificationVerdict.NEEDS_HUMAN_REVIEW
    else:
        verdict = VerificationVerdict.FAIL

    return VerificationResult(
        verdict=verdict,
        measured=measured,
        expected=expected,
        reason=reason,
        raw_evidence=raw_evidence,
    )
