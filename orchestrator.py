"""Order lifecycle orchestrator: polling → verify → accept/reject.

Implements the core state-machine loop:
  ACTIVE ──poll──▶ VERIFYING ──verify──▶ ACCEPTING / REJECTING / AWAITING_ADMIN

Money-safety (C1/C2):
- C1 (no double-create): handled in cli.py/database.py (CREATING → ACTIVE).
- C2 (no double-pay): payments.UNIQUE(exchange, external_submission_id) +
  action_log claim → commit → external call → record pattern.

The orchestrator does NOT hold long DB transactions across HTTP calls.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from adapters.base import Capability, ExchangeAdapter, TaskExchangeAdapter
from config import Settings
from db.database import (
    append_audit,
    connect,
    get_order,
    get_submissions_for_order,
    insert_submission,
    insert_verification,
    list_active_orders,
    payment_exists,
    record_action_failure,
    record_action_success,
    try_claim_payment_action,
    update_order_status,
    update_submission_status,
)
from models import (
    Order,
    OrderStatus,
    Scenario,
    Submission,
    SubmissionStatus,
    VerificationResult,
    VerificationVerdict,
)
from verification.activity import ActivityVerifier
from verification.traffic import TrafficVerifier


class Orchestrator:
    """Drives the order lifecycle for all active orders."""

    def __init__(
        self,
        settings: Settings,
        adapters: Mapping[str, ExchangeAdapter],
        activity_verifier: ActivityVerifier | None = None,
        traffic_verifier: TrafficVerifier | None = None,
    ) -> None:
        self._settings = settings
        self._adapters = adapters
        self._activity_verifier = activity_verifier or ActivityVerifier(mock=True)
        self._traffic_verifier = traffic_verifier or TrafficVerifier(mock=True)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def poll_all(self) -> list[dict[str, Any]]:
        """Poll every ACTIVE/VERIFYING order and return a summary per order."""
        async with connect(self._settings) as conn:
            orders = await list_active_orders(conn)

        results: list[dict[str, Any]] = []
        for order in orders:
            try:
                summary = await self._poll_one(order)
            except Exception as exc:
                summary = {
                    "order_uuid": order.client_order_uuid,
                    "exchange": order.spec.exchange,
                    "status": "error",
                    "error": f"{type(exc).__name__}: {exc}",
                }
            results.append(summary)
        return results

    async def verify_single_order(self, order_uuid: str) -> dict[str, Any]:
        """Manually trigger verification for a single order (CLI / Telegram)."""
        async with connect(self._settings) as conn:
            order = await get_order(conn, order_uuid)
        if order is None:
            return {"order_uuid": order_uuid, "status": "not_found"}
        if order.status not in (OrderStatus.ACTIVE, OrderStatus.VERIFYING):
            return {
                "order_uuid": order_uuid,
                "status": "skipped",
                "reason": f"order status is {order.status.value}, not active/verifying",
            }
        return await self._poll_one(order)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _poll_one(self, order: Order) -> dict[str, Any]:
        adapter = self._adapters.get(order.spec.exchange)
        if adapter is None:
            return {
                "order_uuid": order.client_order_uuid,
                "status": "error",
                "error": f"no adapter for exchange {order.spec.exchange!r}",
            }

        caps = adapter.capabilities()
        ext_order = order.external_order_id
        if ext_order is None:
            return {
                "order_uuid": order.client_order_uuid,
                "status": "error",
                "error": "order has no external_order_id",
            }

        raw_status: str = "unknown"
        if Capability.GET_ORDER_STATUS in caps:
            raw_status = await adapter.get_order_status(ext_order)  # type: ignore[attr-defined]
        else:
            raw_status = "in_progress"
        await self._audit(
            "poll",
            order.client_order_uuid,
            {"raw_status": raw_status, "exchange": order.spec.exchange},
        )

        # Normalize status to our state machine
        if raw_status == "completed":
            if order.status != OrderStatus.VERIFYING:
                await self._update_order_status(order.client_order_uuid, OrderStatus.VERIFYING)
        elif raw_status == "failed":
            await self._update_order_status(order.client_order_uuid, OrderStatus.FAILED)
            return {
                "order_uuid": order.client_order_uuid,
                "status": "failed",
                "reason": "exchange reported failed",
            }
        else:
            # in_progress — keep ACTIVE if not already
            if order.status != OrderStatus.ACTIVE:
                await self._update_order_status(order.client_order_uuid, OrderStatus.ACTIVE)

        # 2. For panel adapters (no submissions), verify at completed
        if Capability.LIST_SUBMISSIONS not in caps:
            if raw_status == "completed":
                return await self._verify_and_decide_panel(order, adapter)
            return {
                "order_uuid": order.client_order_uuid,
                "status": "in_progress",
                "raw_status": raw_status,
            }

        # 3. Task-exchange path
        if not isinstance(adapter, TaskExchangeAdapter):
            return {
                "order_uuid": order.client_order_uuid,
                "status": "error",
                "error": (
                    f"adapter {order.spec.exchange} claims LIST_SUBMISSIONS "
                    "but is not a TaskExchangeAdapter"
                ),
            }

        result = await self._process_task_exchange(order, adapter)

        # If exchange says completed and all submissions are terminal, mark order completed
        if raw_status == "completed":
            async with connect(self._settings) as conn:
                all_subs = await get_submissions_for_order(conn, order.client_order_uuid)
            terminal = {
                SubmissionStatus.ACCEPTED,
                SubmissionStatus.REWORK_REQUESTED,
                SubmissionStatus.FAILED,
            }
            if all_subs and all(s.status in terminal for s in all_subs):
                await self._update_order_status(order.client_order_uuid, OrderStatus.COMPLETED)
                await self._audit(
                    "order_completed",
                    order.client_order_uuid,
                    {"reason": "all submissions terminal"},
                )
                result["status"] = "completed"

        return result

    # ------------------------------------------------------------------
    # Panel path
    # ------------------------------------------------------------------

    async def _verify_and_decide_panel(
        self, order: Order, adapter: ExchangeAdapter
    ) -> dict[str, Any]:
        verifier = self._pick_verifier(order)
        result = await verifier.verify(order)
        await self._persist_verification(order.client_order_uuid, None, result)

        if result.verdict == VerificationVerdict.AUTO_PASS:
            # Panels are prepaid — no per-submission accept. Mark completed.
            await self._update_order_status(order.client_order_uuid, OrderStatus.COMPLETED)
            await self._audit(
                "panel_completed",
                order.client_order_uuid,
                {"verdict": result.verdict.value, "measured": result.measured},
            )
            return {
                "order_uuid": order.client_order_uuid,
                "status": "completed",
                "verdict": result.verdict.value,
                "measured": result.measured,
            }

        if result.verdict == VerificationVerdict.FAIL:
            # Panel orders are prepaid — cannot reject. Mark failed.
            await self._update_order_status(order.client_order_uuid, OrderStatus.FAILED)
            await self._audit(
                "panel_failed",
                order.client_order_uuid,
                {"verdict": result.verdict.value, "reason": result.reason},
            )
            return {
                "order_uuid": order.client_order_uuid,
                "status": "failed",
                "verdict": result.verdict.value,
                "reason": result.reason,
            }

        # needs_human_review
        await self._audit(
            "panel_needs_review",
            order.client_order_uuid,
            {"verdict": result.verdict.value, "reason": result.reason},
        )
        return {
            "order_uuid": order.client_order_uuid,
            "status": "needs_human_review",
            "verdict": result.verdict.value,
            "reason": result.reason,
        }

    # ------------------------------------------------------------------
    # Task-exchange path
    # ------------------------------------------------------------------

    async def _process_task_exchange(
        self, order: Order, adapter: TaskExchangeAdapter
    ) -> dict[str, Any]:
        external_id = order.external_order_id
        assert external_id is not None

        # Ingest new submissions from the exchange
        external_subs = await adapter.list_submissions(external_id)
        async with connect(self._settings) as conn:
            existing = await get_submissions_for_order(conn, order.client_order_uuid)
        existing_external_ids = {s.external_submission_id for s in existing}

        newly_created: list[Submission] = []
        for es in external_subs:
            if es.external_submission_id in existing_external_ids:
                continue
            now = datetime.now(UTC)
            sub = Submission(
                submission_uuid=str(uuid.uuid4()),
                order_uuid=order.client_order_uuid,
                external_submission_id=es.external_submission_id,
                executor_hint=es.executor_hint,
                status=SubmissionStatus.NEW,
                evidence=es.evidence,
                created_at=now,
            )
            async with connect(self._settings) as conn:
                await insert_submission(conn, sub)
            newly_created.append(sub)
            await self._audit(
                "submission_ingested",
                order.client_order_uuid,
                {
                    "submission_uuid": sub.submission_uuid,
                    "external_submission_id": es.external_submission_id,
                },
            )

        # Load all pending submissions for this order
        async with connect(self._settings) as conn:
            all_subs = await get_submissions_for_order(conn, order.client_order_uuid)
        pending = [s for s in all_subs if s.status == SubmissionStatus.NEW]

        decisions: list[dict[str, Any]] = []
        for sub in pending:
            decision = await self._verify_and_decide_submission(order, adapter, sub)
            decisions.append(decision)

        # If there are still unresolved submissions, order stays in_progress
        # (the exchange will keep it in_progress until all are accepted/rejected)
        return {
            "order_uuid": order.client_order_uuid,
            "status": "processed",
            "new_submissions": len(newly_created),
            "pending_submissions": len(pending),
            "decisions": decisions,
        }

    async def _verify_and_decide_submission(
        self,
        order: Order,
        adapter: TaskExchangeAdapter,
        sub: Submission,
    ) -> dict[str, Any]:
        """Verify a single submission and decide accept/reject/human-review."""
        verifier = self._pick_verifier(order)
        result = await verifier.verify(order, submission=sub)
        await self._persist_verification(order.client_order_uuid, sub.submission_uuid, result)

        if result.verdict == VerificationVerdict.AUTO_PASS:
            return await self._accept_submission(order, adapter, sub, result)

        if result.verdict == VerificationVerdict.FAIL:
            return await self._reject_submission(order, adapter, sub, result)

        # needs_human_review
        await self._update_submission_status(sub.submission_uuid, SubmissionStatus.AWAITING_ADMIN)
        await self._audit(
            "submission_needs_review",
            order.client_order_uuid,
            {
                "submission_uuid": sub.submission_uuid,
                "external_submission_id": sub.external_submission_id,
                "reason": result.reason,
            },
        )
        return {
            "submission_uuid": sub.submission_uuid,
            "decision": "needs_human_review",
            "reason": result.reason,
        }

    async def _accept_submission(
        self,
        order: Order,
        adapter: TaskExchangeAdapter,
        sub: Submission,
        result: VerificationResult,
    ) -> dict[str, Any]:
        """C2-safe accept: claim → check payments → call adapter → record."""
        ext_order = order.external_order_id
        ext_sub = sub.external_submission_id
        if ext_order is None or ext_sub is None:
            return {
                "submission_uuid": sub.submission_uuid,
                "decision": "accept_skipped",
                "reason": "missing external ids",
            }
        assert ext_order is not None and ext_sub is not None
        action_uuid = str(uuid.uuid4())
        async with connect(self._settings) as conn:
            # 1. Claim in-flight action (idempotent guard start)
            await try_claim_payment_action(
                conn, action_uuid, sub.submission_uuid, order.client_order_uuid, "accept"
            )
            # 2. Check if already terminal in payments
            already = await payment_exists(conn, order.spec.exchange, ext_sub)
            if already:
                await record_action_success(conn, action_uuid)
                await self._audit(
                    "accept_skipped",
                    order.client_order_uuid,
                    {"submission_uuid": sub.submission_uuid, "reason": "already paid"},
                )
                return {
                    "submission_uuid": sub.submission_uuid,
                    "decision": "accept_skipped",
                    "reason": "already paid",
                }

        # 3. External call (outside DB transaction)
        try:
            await adapter.accept_submission(ext_order, ext_sub)
        except Exception as exc:
            async with connect(self._settings) as conn:
                await record_action_failure(conn, action_uuid, str(exc))
            await self._audit(
                "accept_failed",
                order.client_order_uuid,
                {
                    "submission_uuid": sub.submission_uuid,
                    "error": str(exc),
                },
            )
            return {
                "submission_uuid": sub.submission_uuid,
                "decision": "accept_failed",
                "error": str(exc),
            }

        # 4. Record terminal payment + update submission status
        async with connect(self._settings) as conn:
            await record_action_success(conn, action_uuid)
            from db.database import insert_payment

            await insert_payment(
                conn,
                exchange=order.spec.exchange,
                external_submission_id=ext_sub,
                action="accept",
                submission_uuid=sub.submission_uuid,
                decided_by="orchestrator",
            )
            await update_submission_status(conn, sub.submission_uuid, SubmissionStatus.ACCEPTED)

        await self._audit(
            "submission_accepted",
            order.client_order_uuid,
            {
                "submission_uuid": sub.submission_uuid,
                "external_submission_id": sub.external_submission_id,
                "measured": result.measured,
            },
        )
        return {
            "submission_uuid": sub.submission_uuid,
            "decision": "accepted",
            "measured": result.measured,
        }

    async def _reject_submission(
        self,
        order: Order,
        adapter: TaskExchangeAdapter,
        sub: Submission,
        result: VerificationResult,
    ) -> dict[str, Any]:
        """A6: auto-reject for fail verdict (safe, no money spent)."""
        ext_order = order.external_order_id
        ext_sub = sub.external_submission_id
        if ext_order is None or ext_sub is None:
            return {
                "submission_uuid": sub.submission_uuid,
                "decision": "reject_skipped",
                "reason": "missing external ids",
            }
        assert ext_order is not None and ext_sub is not None
        action_uuid = str(uuid.uuid4())
        async with connect(self._settings) as conn:
            await try_claim_payment_action(
                conn, action_uuid, sub.submission_uuid, order.client_order_uuid, "reject"
            )
            already = await payment_exists(conn, order.spec.exchange, ext_sub)
            if already:
                await record_action_success(conn, action_uuid)
                return {
                    "submission_uuid": sub.submission_uuid,
                    "decision": "reject_skipped",
                    "reason": "already terminal",
                }

        try:
            await adapter.reject_submission(ext_order, ext_sub, reason=result.reason)
        except Exception as exc:
            async with connect(self._settings) as conn:
                await record_action_failure(conn, action_uuid, str(exc))
            return {
                "submission_uuid": sub.submission_uuid,
                "decision": "reject_failed",
                "error": str(exc),
            }

        async with connect(self._settings) as conn:
            await record_action_success(conn, action_uuid)
            from db.database import insert_payment

            await insert_payment(
                conn,
                exchange=order.spec.exchange,
                external_submission_id=ext_sub,
                action="reject",
                submission_uuid=sub.submission_uuid,
                decided_by="orchestrator",
            )
            await update_submission_status(
                conn, sub.submission_uuid, SubmissionStatus.REWORK_REQUESTED
            )

        await self._audit(
            "submission_rejected",
            order.client_order_uuid,
            {
                "submission_uuid": sub.submission_uuid,
                "external_submission_id": sub.external_submission_id,
                "reason": result.reason,
            },
        )
        return {
            "submission_uuid": sub.submission_uuid,
            "decision": "rejected",
            "reason": result.reason,
        }

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _pick_verifier(self, order: Order):
        if order.spec.scenario == Scenario.SOCIAL_TRAFFIC:
            return self._traffic_verifier
        return self._activity_verifier

    async def _update_order_status(self, order_uuid: str, status: OrderStatus) -> None:
        async with connect(self._settings) as conn:
            await update_order_status(conn, order_uuid, status)

    async def _update_submission_status(
        self, submission_uuid: str, status: SubmissionStatus
    ) -> None:
        async with connect(self._settings) as conn:
            await update_submission_status(conn, submission_uuid, status)

    async def _persist_verification(
        self,
        order_uuid: str,
        submission_uuid: str | None,
        result: VerificationResult,
    ) -> None:
        async with connect(self._settings) as conn:
            await insert_verification(conn, order_uuid, submission_uuid, result)

    async def _audit(self, event: str, order_uuid: str, details: dict[str, Any]) -> None:
        async with connect(self._settings) as conn:
            await append_audit(
                conn, actor="orchestrator", event=event, order_uuid=order_uuid, details=details
            )
