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
from datetime import UTC, datetime, timedelta
from typing import Any

from adapters.base import Capability, ExchangeAdapter, TaskExchangeAdapter
from config import Settings
from db.database import (
    append_audit,
    claim_submission_action,
    claim_submission_and_open_action,
    connect,
    ensure_submission_persisted,
    get_order,
    get_submission,
    get_submissions_for_order,
    insert_report_row,
    insert_verification,
    list_active_orders,
    list_order_uuids_in_status_older_than,
    payment_exists,
    record_action_failure,
    record_action_success,
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
        self._activity_verifier = activity_verifier or ActivityVerifier(mock=settings.dry_run)
        self._traffic_verifier = traffic_verifier or TrafficVerifier(
            counter_id=settings.metrica_counter_id,
            oauth_token=settings.metrica_oauth_token,
            mock=settings.dry_run,
            verification_window_days=settings.metrica_verification_window_days,
        )

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

    async def admin_accept_submission(self, submission_uuid: str, actor: str) -> dict[str, Any]:
        """Accept a human-reviewed submission through the same C2 guard as auto-accept."""
        if self._settings.dry_run:
            return {
                "submission_uuid": submission_uuid,
                "status": "dry_run_blocked",
                "reason": "DRY_RUN=true: external accept is disabled",
            }
        order, sub, adapter_or_error = await self._load_admin_decision_context(submission_uuid)
        if isinstance(adapter_or_error, dict):
            return adapter_or_error
        adapter = adapter_or_error
        result = VerificationResult(
            verdict=VerificationVerdict.AUTO_PASS,
            measured=float(order.spec.quantity),
            expected=float(order.spec.quantity),
            reason=f"manual accept by {actor}",
            raw_evidence={"mode": "admin", "actor": actor},
        )
        decision = await self._accept_submission(order, adapter, sub, result, decided_by=actor)
        await self._complete_order_if_all_submissions_terminal(order)
        await self._audit(
            "admin_accept",
            order.client_order_uuid,
            {"submission_uuid": submission_uuid, "actor": actor, "decision": decision},
        )
        return decision

    async def admin_reject_submission(
        self,
        submission_uuid: str,
        actor: str,
        reason: str = "Возвращено администратором на доработку",
    ) -> dict[str, Any]:
        """Reject a human-reviewed submission through the same C2 guard as auto-reject."""
        if self._settings.dry_run:
            return {
                "submission_uuid": submission_uuid,
                "status": "dry_run_blocked",
                "reason": "DRY_RUN=true: external reject is disabled",
            }
        order, sub, adapter_or_error = await self._load_admin_decision_context(submission_uuid)
        if isinstance(adapter_or_error, dict):
            return adapter_or_error
        adapter = adapter_or_error
        result = VerificationResult(
            verdict=VerificationVerdict.FAIL,
            measured=0.0,
            expected=float(order.spec.quantity),
            reason=reason,
            raw_evidence={"mode": "admin", "actor": actor},
        )
        decision = await self._reject_submission(order, adapter, sub, result, decided_by=actor)
        await self._complete_order_if_all_submissions_terminal(order)
        await self._audit(
            "admin_reject",
            order.client_order_uuid,
            {"submission_uuid": submission_uuid, "actor": actor, "decision": decision},
        )
        return decision

    async def reconcile_creating(
        self,
        actor: str = "reconcile",
        min_age_seconds: int = 300,
    ) -> int:
        """C1 startup reconciliation: move orphan CREATING orders to FAILED.

        Day 3 audit (HIGH-4 fix): age-gated so we never race a live in-flight
        `create_order`. A row created within `min_age_seconds` is left alone —
        the most likely explanation is that another worker is still between the
        `INSERT INTO orders` and `mark_order_active`. After the threshold, the
        conservative choice is to mark the row FAILED + log an audit entry; a
        human (or the per-adapter exchange-side lookup, future scope)
        determines whether the order actually made it to the exchange.

        Returns the number of rows reconciled. Default `min_age_seconds=300`
        (5 minutes) — production callers (`main.py` startup) use the default;
        tests pass `0` to act on freshly-inserted fixture rows.
        """
        cutoff_iso = (datetime.now(UTC) - timedelta(seconds=max(0, min_age_seconds))).isoformat(
            timespec="seconds"
        )
        async with connect(self._settings) as conn:
            uuids = await list_order_uuids_in_status_older_than(
                conn, OrderStatus.CREATING, cutoff_iso
            )

        for client_uuid in uuids:
            async with connect(self._settings) as conn:
                await update_order_status(conn, client_uuid, OrderStatus.FAILED)
                await append_audit(
                    conn,
                    actor=actor,
                    event="reconcile_creating_to_failed",
                    order_uuid=client_uuid,
                    details={
                        "reason": (
                            "CREATING row older than min_age threshold; "
                            "conservative fail-and-flag pending per-adapter reconcile"
                        ),
                        "min_age_seconds": min_age_seconds,
                    },
                )
        return len(uuids)

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
                await self._record_report_row(order)
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
            await self._record_report_row(order, actual_count=int(result.measured))
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
            await self._record_report_row(order, actual_count=int(result.measured), status="failed")
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
        if self._settings.auto_reject_uncertain_results:
            await self._update_order_status(order.client_order_uuid, OrderStatus.FAILED)
            await self._record_report_row(order, actual_count=int(result.measured), status="failed")
            await self._audit(
                "panel_uncertain_auto_failed",
                order.client_order_uuid,
                {
                    "verdict": result.verdict.value,
                    "reason": result.reason,
                    "policy": "auto_reject_uncertain_results",
                },
            )
            return {
                "order_uuid": order.client_order_uuid,
                "status": "failed",
                "verdict": result.verdict.value,
                "reason": "uncertain verification auto-failed by policy",
            }

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

        # Ingest new submissions from the exchange.
        # Day 3 audit (CRITICAL-1 fix): race-safe via ensure_submission_persisted —
        # INSERT OR IGNORE against the partial unique index, then re-fetch the
        # canonical row. Two concurrent pollers converge on the same submission_uuid.
        external_subs = await adapter.list_submissions(external_id)
        newly_created: list[Submission] = []
        for es in external_subs:
            now = datetime.now(UTC)
            candidate = Submission(
                submission_uuid=str(uuid.uuid4()),
                order_uuid=order.client_order_uuid,
                external_submission_id=es.external_submission_id,
                executor_hint=es.executor_hint,
                status=SubmissionStatus.NEW,
                evidence=es.evidence,
                created_at=now,
            )
            async with connect(self._settings) as conn:
                sub, inserted_now = await ensure_submission_persisted(conn, candidate)
            if inserted_now:
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
        """Verify a single submission and decide accept/reject/human-review.

        Day 3 audit (CRITICAL-2 fix): the NEW → VERIFYING transition is a
        conditional UPDATE. A second concurrent poller seeing the same
        SubmissionStatus.NEW row will lose the claim and skip — only one caller
        runs the external verifier + accept/reject flow.
        """
        async with connect(self._settings) as conn:
            claimed = await claim_submission_action(
                conn,
                sub.submission_uuid,
                target=SubmissionStatus.VERIFYING,
                allowed_from=(SubmissionStatus.NEW,),
            )
        if not claimed:
            return {
                "submission_uuid": sub.submission_uuid,
                "decision": "skipped",
                "reason": "concurrent worker already processing",
            }

        verifier = self._pick_verifier(order)
        result = await verifier.verify(order, submission=sub)
        await self._persist_verification(order.client_order_uuid, sub.submission_uuid, result)

        if self._settings.dry_run and result.verdict in (
            VerificationVerdict.AUTO_PASS,
            VerificationVerdict.FAIL,
        ):
            async with connect(self._settings) as conn:
                await claim_submission_action(
                    conn,
                    sub.submission_uuid,
                    target=SubmissionStatus.AWAITING_ADMIN,
                    allowed_from=(SubmissionStatus.VERIFYING,),
                )
            await self._audit(
                "submission_dry_run_decision_blocked",
                order.client_order_uuid,
                {
                    "submission_uuid": sub.submission_uuid,
                    "external_submission_id": sub.external_submission_id,
                    "would_have_verdict": result.verdict.value,
                    "reason": "DRY_RUN=true: no external accept/reject",
                },
            )
            return {
                "submission_uuid": sub.submission_uuid,
                "decision": "dry_run_blocked",
                "would_have_verdict": result.verdict.value,
                "reason": "DRY_RUN=true: no external accept/reject",
            }

        if result.verdict == VerificationVerdict.AUTO_PASS:
            return await self._accept_submission(order, adapter, sub, result)

        if result.verdict == VerificationVerdict.FAIL:
            return await self._reject_submission(order, adapter, sub, result)

        if self._settings.auto_reject_uncertain_results and not self._settings.dry_run:
            result = VerificationResult(
                verdict=VerificationVerdict.FAIL,
                measured=result.measured,
                expected=result.expected,
                reason=f"uncertain verification auto-rejected by policy: {result.reason}",
                raw_evidence={
                    **result.raw_evidence,
                    "original_verdict": VerificationVerdict.NEEDS_HUMAN_REVIEW.value,
                    "policy": "auto_reject_uncertain_results",
                },
            )
            return await self._reject_submission(order, adapter, sub, result)

        # needs_human_review: VERIFYING → AWAITING_ADMIN (conditional, CRITICAL-2).
        async with connect(self._settings) as conn:
            await claim_submission_action(
                conn,
                sub.submission_uuid,
                target=SubmissionStatus.AWAITING_ADMIN,
                allowed_from=(SubmissionStatus.VERIFYING,),
            )
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
        *,
        decided_by: str = "orchestrator",
    ) -> dict[str, Any]:
        """C2-safe accept: claim → check payments → call adapter → record.

        Day 3 audit (HIGH-3 fix): the submission claim (→ ACCEPTING) and the
        `action_log` open commit in a SINGLE transaction via
        `claim_submission_and_open_action`. A losing concurrent caller — or one
        that finds the row already terminal — returns `accept_skipped` without
        making the external call.
        """
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
            # 1. Atomic claim + action_log open. Allowed-from includes the
            #    statuses an admin or earlier verifier could have left the row
            #    in. A loser of the race returns False and we skip cleanly.
            claimed = await claim_submission_and_open_action(
                conn,
                sub.submission_uuid,
                target=SubmissionStatus.ACCEPTING,
                allowed_from=(
                    SubmissionStatus.NEW,
                    SubmissionStatus.VERIFYING,
                    SubmissionStatus.AWAITING_ADMIN,
                ),
                action_uuid=action_uuid,
                action="accept",
                order_uuid=order.client_order_uuid,
            )
            already = await payment_exists(conn, order.spec.exchange, ext_sub)
        if not claimed:
            await self._audit(
                "accept_skipped",
                order.client_order_uuid,
                {
                    "submission_uuid": sub.submission_uuid,
                    "reason": "claim lost (concurrent worker or terminal status)",
                },
            )
            return {
                "submission_uuid": sub.submission_uuid,
                "decision": "accept_skipped",
                "reason": "claim lost",
            }
        if already:
            # Healed-state path: payment row already exists from an earlier
            # attempt that crashed AFTER recording payment but BEFORE moving
            # submission to ACCEPTED. We hold the claim → finish the
            # transition + close action_log. NO external call (already paid).
            async with connect(self._settings) as conn:
                await record_action_success(conn, action_uuid)
                await update_submission_status(conn, sub.submission_uuid, SubmissionStatus.ACCEPTED)
            await self._audit(
                "accept_skipped",
                order.client_order_uuid,
                {
                    "submission_uuid": sub.submission_uuid,
                    "reason": "already paid (healed status → ACCEPTED)",
                },
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
                decided_by=decided_by,
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
        *,
        decided_by: str = "orchestrator",
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
            claimed = await claim_submission_and_open_action(
                conn,
                sub.submission_uuid,
                target=SubmissionStatus.REJECTING,
                allowed_from=(
                    SubmissionStatus.NEW,
                    SubmissionStatus.VERIFYING,
                    SubmissionStatus.AWAITING_ADMIN,
                ),
                action_uuid=action_uuid,
                action="reject",
                order_uuid=order.client_order_uuid,
            )
            already = await payment_exists(conn, order.spec.exchange, ext_sub)
        if not claimed:
            return {
                "submission_uuid": sub.submission_uuid,
                "decision": "reject_skipped",
                "reason": "claim lost",
            }
        if already:
            # Healed-state path: payment row already exists (from an earlier
            # crashed attempt). Close action_log and move submission to its
            # terminal state. No external call.
            async with connect(self._settings) as conn:
                await record_action_success(conn, action_uuid)
                await update_submission_status(
                    conn, sub.submission_uuid, SubmissionStatus.REWORK_REQUESTED
                )
            return {
                "submission_uuid": sub.submission_uuid,
                "decision": "reject_skipped",
                "reason": "already terminal (healed status → REWORK_REQUESTED)",
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
                decided_by=decided_by,
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

    async def _load_admin_decision_context(
        self, submission_uuid: str
    ) -> tuple[Order, Submission, TaskExchangeAdapter] | tuple[None, None, dict[str, Any]]:
        async with connect(self._settings) as conn:
            sub = await get_submission(conn, submission_uuid)
            if sub is None:
                return None, None, {"submission_uuid": submission_uuid, "status": "not_found"}
            order = await get_order(conn, sub.order_uuid)
            if order is None:
                return (
                    None,
                    None,
                    {
                        "submission_uuid": submission_uuid,
                        "status": "error",
                        "error": "parent order not found",
                    },
                )

        adapter = self._adapters.get(order.spec.exchange)
        if not isinstance(adapter, TaskExchangeAdapter):
            return (
                None,
                None,
                {
                    "submission_uuid": submission_uuid,
                    "status": "error",
                    "error": f"no task-exchange adapter for {order.spec.exchange!r}",
                },
            )
        return order, sub, adapter

    async def _record_report_row(
        self,
        order: Order,
        *,
        actual_count: int | None = None,
        status: str | None = None,
    ) -> None:
        if order.spec.scenario != Scenario.SOCIAL_TRAFFIC or order.spec.source_platform is None:
            return

        if actual_count is None:
            async with connect(self._settings) as conn:
                subs = await get_submissions_for_order(conn, order.client_order_uuid)
            actual_count = sum(1 for sub in subs if sub.status == SubmissionStatus.ACCEPTED)

        week = datetime.now(UTC).strftime("%G-W%V")
        async with connect(self._settings) as conn:
            await insert_report_row(
                conn,
                week=week,
                source_platform=order.spec.source_platform.value,
                exchange=order.spec.exchange,
                ordered_count=order.spec.quantity,
                actual_count=actual_count,
                cost=order.cost_actual,
                status=status or OrderStatus.COMPLETED.value,
            )

    async def _complete_order_if_all_submissions_terminal(self, order: Order) -> None:
        async with connect(self._settings) as conn:
            subs = await get_submissions_for_order(conn, order.client_order_uuid)
            stored = await get_order(conn, order.client_order_uuid)
        if stored is None or stored.status in {
            OrderStatus.COMPLETED,
            OrderStatus.FAILED,
            OrderStatus.CANCELLED,
        }:
            return
        terminal = {
            SubmissionStatus.ACCEPTED,
            SubmissionStatus.REWORK_REQUESTED,
            SubmissionStatus.FAILED,
        }
        if subs and all(sub.status in terminal for sub in subs):
            await self._update_order_status(order.client_order_uuid, OrderStatus.COMPLETED)
            await self._record_report_row(order)

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
