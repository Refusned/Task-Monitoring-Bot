"""Placeholder verifiers for Day 3 (orchestrator MVP).

Day 5 replaces these with real verifiers:
- `TrafficVerifier` hits Yandex Metrica filtered by UTM.
- `ActivityVerifier` reads programmatic social-network counters where available,
  falls back to manual baseline + final-count evidence elsewhere.

The orchestrator only sees the `VerificationResult` interface, so the swap is
drop-in.
"""

from __future__ import annotations

from models import Order, VerificationResult, VerificationVerdict


def verify_submission(evidence: str | None) -> VerificationResult:
    """Day 3 stub: trust the `evidence_quality` hint set by the fake adapter.

    Real verification (qualitative content check via API counters or LLM-on-text
    in the optional A11 extension) lands in Day 5.
    """
    e = evidence or ""
    if e == "good":
        verdict = VerificationVerdict.AUTO_PASS
        measured, expected = 1.0, 1.0
        reason = "demo-verdict: evidence=good"
    elif e == "weak":
        verdict = VerificationVerdict.FAIL
        measured, expected = 0.0, 1.0
        reason = "demo-verdict: evidence=weak"
    else:
        verdict = VerificationVerdict.NEEDS_HUMAN_REVIEW
        measured, expected = 0.0, 1.0
        reason = f"demo-verdict: evidence={e!r} - needs human review"

    return VerificationResult(
        verdict=verdict,
        measured=measured,
        expected=expected,
        reason=reason,
        raw_evidence={"evidence": e},
    )


def verify_panel_completion(order: Order) -> VerificationResult:
    """Day 3 stub: if the exchange reported the panel order completed, accept.

    Real activity verification (delta counters where available, manual baseline
    elsewhere - A3 / A4) lands in Day 5.
    """
    return VerificationResult(
        verdict=VerificationVerdict.AUTO_PASS,
        measured=float(order.spec.quantity),
        expected=float(order.spec.quantity),
        reason="demo-verdict: panel exchange reported completed",
        raw_evidence={
            "external_order_id": order.external_order_id,
            "client_order_uuid": order.client_order_uuid,
        },
    )
