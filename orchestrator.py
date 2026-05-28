"""Order lifecycle orchestrator.

Owns the state machine: creates orders, polls them, processes submissions (for
microtask exchanges), and performs accept/reject actions through the C2
idempotency pattern.

## State machine

Order:

    DRAFT -> CREATING -> ACTIVE -> VERIFYING -> COMPLETED
                            |         |
                            +-> FAILED <-+
                          (and CANCELLED for admin-initiated; not in Day 3)

Submission (microtask exchanges only):

    NEW -> VERIFYING -> AWAITING_ADMIN
                            |
                            +-> ACCEPTING  -> ACCEPTED
                            +-> REJECTING  -> REWORK_REQUESTED
                            +-> (either)   -> FAILED  (external call errored)

## Money invariants

- **C1** - no duplicate placement. `insert_order_creating` writes the CREATING
  row before the adapter call; `mark_order_active` requires the row to still be
  in CREATING. `reconcile_creating` only acts on rows older than
  `min_age_seconds` (default 300) so it never races with a live create.

- **C2** - no double accept/reject. Race-safety relies on three SQLite-level
  guards:
    1. `UNIQUE(order_uuid, external_submission_id)` on `submissions` + INSERT
       OR IGNORE in `ensure_submission_persisted` - one local row per external
       submission.
    2. Every status transition during a money action is a CONDITIONAL UPDATE
       (`claim_submission_action`, `claim_submission_and_open_action`). A stale
       reader can never overwrite a later state.
    3. The claim and the `action_log` open are committed in the SAME
       transaction; the external HTTP call happens between commits, never
       inside one. `payments` PRIMARY KEY (exchange, external_submission_id)
       keeps the terminal record unique even if a replay reaches the recorder.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from adapters.base import ExchangeAdapter, PanelAdapter, TaskExchangeAdapter
from config import Settings
from db.database import (
    append_audit,
    claim_order_status,
    claim_submission_action,
    claim_submission_and_open_action,
    complete_action_log,
    connect,
    count_non_terminal_submissions_for_order,
    ensure_submission_persisted,
    get_order,
    get_submission,
    list_order_uuids_in_status_older_than,
    mark_order_active,
    record_payment,
    reserve_order_creating_with_spend_limits,
    update_order_status,
)
from models import (
    Order,
    OrderSpec,
    OrderStatus,
    Submission,
    SubmissionStatus,
    VerificationResult,
    VerificationVerdict,
    new_client_order_uuid,
)
from verification.fake import verify_panel_completion, verify_submission


async def persist_and_create_order(
    settings: Settings,
    adapter: ExchangeAdapter,
    spec: OrderSpec,
    actor: str,
    client_uuid: str | None = None,
) -> tuple[str, str, float]:
    """C1: write the CREATING row, call adapter.create_order, mark ACTIVE.

    On adapter failure, moves the CREATING row to FAILED and re-raises so the
    audit log reflects what actually happened (HIGH-a invariant: no silent
    transitions).

    Returns (client_order_uuid, external_order_id, cost).
    """
    if not isinstance(adapter, (PanelAdapter, TaskExchangeAdapter)):
        raise TypeError(f"adapter {adapter.name!r} cannot create orders")

    now = datetime.now(UTC)
    client_uuid = client_uuid or new_client_order_uuid()
    order = Order(
        client_order_uuid=client_uuid,
        spec=spec,
        status=OrderStatus.CREATING,
        created_at=now,
        updated_at=now,
    )
    async with connect(settings) as conn:
        await reserve_order_creating_with_spend_limits(
            conn,
            order,
            per_order_limit=settings.per_order_spend_limit,
            daily_limit=settings.daily_spend_limit,
        )
        await append_audit(
            conn,
            actor=actor,
            event="order_creating",
            order_uuid=client_uuid,
            details={
                "exchange": spec.exchange,
                "scenario": spec.scenario.value,
                "target": spec.target,
            },
        )

    try:
        external_id, cost = await adapter.create_order(spec, client_uuid)
    except Exception as exc:
        async with connect(settings) as conn:
            await update_order_status(conn, client_uuid, OrderStatus.FAILED)
            await append_audit(
                conn,
                actor=actor,
                event="order_create_failed",
                order_uuid=client_uuid,
                details={"error": str(exc), "error_type": type(exc).__name__},
            )
        raise

    async with connect(settings) as conn:
        await mark_order_active(conn, client_uuid, external_id, cost)
        await append_audit(
            conn,
            actor=actor,
            event="order_active",
            order_uuid=client_uuid,
            details={"external_order_id": external_id, "cost": cost},
        )
    return client_uuid, external_id, cost


class Orchestrator:
    """Drives orders through their lifecycle.

    Holds a registry of adapters keyed by exchange name. Methods read state from
    SQLite, ask the adapter what's happening on the exchange side, and advance
    the state machine - writing every transition to the audit log.
    """

    def __init__(self, settings: Settings, adapters: dict[str, ExchangeAdapter]) -> None:
        self._settings = settings
        self._adapters = adapters

    def adapter_for(self, exchange_name: str) -> ExchangeAdapter:
        adapter = self._adapters.get(exchange_name)
        if adapter is None:
            raise ValueError(f"no adapter registered for exchange {exchange_name!r}")
        return adapter

    async def create_order(self, spec: OrderSpec, actor: str) -> tuple[str, str, float]:
        return await persist_and_create_order(
            self._settings, self.adapter_for(spec.exchange), spec, actor
        )

    async def poll_order(self, client_order_uuid: str, actor: str = "system") -> OrderStatus:
        """Advance the order one tick. Returns the new OrderStatus."""
        async with connect(self._settings) as conn:
            order = await get_order(conn, client_order_uuid)
        if order is None:
            raise ValueError(f"order {client_order_uuid!r} not found")
        if order.status not in (OrderStatus.ACTIVE, OrderStatus.VERIFYING):
            return order.status  # terminal / pre-active - nothing to do
        if order.external_order_id is None:
            return order.status  # defensive - shouldn't happen for ACTIVE

        adapter = self.adapter_for(order.spec.exchange)
        raw_status = await adapter.get_order_status(order.external_order_id)

        async with connect(self._settings) as conn:
            await append_audit(
                conn,
                actor=actor,
                event="poll",
                order_uuid=client_order_uuid,
                details={"raw_status": raw_status},
            )

        if raw_status == "failed":
            async with connect(self._settings) as conn:
                claimed = await claim_order_status(
                    conn,
                    client_order_uuid,
                    target=OrderStatus.FAILED,
                    allowed_from=(OrderStatus.ACTIVE, OrderStatus.VERIFYING),
                    raw_exchange_status=raw_status,
                )
                if not claimed:
                    current = await get_order(conn, client_order_uuid)
                    return current.status if current else OrderStatus.FAILED
                await append_audit(
                    conn,
                    actor=actor,
                    event="order_failed",
                    order_uuid=client_order_uuid,
                )
            return OrderStatus.FAILED

        if isinstance(adapter, TaskExchangeAdapter):
            await self._process_submissions(adapter, order, actor)

        if raw_status == "completed":
            return await self._finalize_completed_order(adapter, order, actor)

        return order.status  # still ACTIVE / VERIFYING

    async def _process_submissions(
        self,
        adapter: TaskExchangeAdapter,
        order: Order,
        actor: str,
    ) -> None:
        """For task-exchange orders: list submissions, persist new ones, decide.

        Day 3 audit fixes:
        - Submission persistence uses INSERT OR IGNORE (UNIQUE constraint),
          so concurrent pollers converge on a single local row (CRITICAL-1).
        - Status transitions use claim_submission_action - a conditional UPDATE
          that refuses to overwrite a more-progressed state (CRITICAL-2).
        - A loser of either race simply skips the submission and moves on.
        """
        assert order.external_order_id is not None
        external_subs = await adapter.list_submissions(order.external_order_id)
        for ext_sub in external_subs:
            candidate = Submission(
                submission_uuid=str(uuid.uuid4()),
                order_uuid=order.client_order_uuid,
                external_submission_id=ext_sub.external_submission_id,
                executor_hint=ext_sub.executor_hint,
                status=SubmissionStatus.NEW,
                evidence=ext_sub.evidence,
                created_at=datetime.now(UTC),
            )
            async with connect(self._settings) as conn:
                sub, inserted_now = await ensure_submission_persisted(
                    conn, candidate, order.spec.exchange
                )
                if inserted_now:
                    await append_audit(
                        conn,
                        actor=actor,
                        event="submission_new",
                        submission_uuid=sub.submission_uuid,
                        order_uuid=order.client_order_uuid,
                        details={"external_submission_id": ext_sub.external_submission_id},
                    )

            # CRITICAL-2 fix: NEW -> VERIFYING via conditional UPDATE.
            # If the row is no longer NEW (another worker progressed it, or it's
            # already terminal) the claim fails and we skip - we MUST NOT roll
            # state back.
            async with connect(self._settings) as conn:
                claimed_verification = await claim_submission_action(
                    conn,
                    sub.submission_uuid,
                    target=SubmissionStatus.VERIFYING,
                    allowed_from=(SubmissionStatus.NEW,),
                )
            if not claimed_verification:
                continue  # already past NEW - someone else is handling this submission

            result = verify_submission(sub.evidence)
            if (
                not self._settings.dry_run
                and result.verdict == VerificationVerdict.AUTO_PASS
                and result.reason.startswith("demo-verdict:")
            ):
                result = VerificationResult(
                    verdict=VerificationVerdict.NEEDS_HUMAN_REVIEW,
                    measured=result.measured,
                    expected=result.expected,
                    reason="live mode: demo verifier cannot auto-accept paid submissions",
                    raw_evidence=result.raw_evidence,
                )
            async with connect(self._settings) as conn:
                await append_audit(
                    conn,
                    actor=actor,
                    event="submission_verified",
                    submission_uuid=sub.submission_uuid,
                    order_uuid=order.client_order_uuid,
                    details={"verdict": result.verdict.value, "reason": result.reason},
                )

            if result.verdict == VerificationVerdict.AUTO_PASS:
                await self.accept_submission(sub.submission_uuid, decided_by=f"auto:{actor}")
            elif result.verdict == VerificationVerdict.FAIL:
                await self.reject_submission(
                    sub.submission_uuid,
                    reason=result.reason,
                    decided_by=f"auto:{actor}",
                )
            else:  # NEEDS_HUMAN_REVIEW
                async with connect(self._settings) as conn:
                    # VERIFYING -> AWAITING_ADMIN, also conditional (CRITICAL-2).
                    await claim_submission_action(
                        conn,
                        sub.submission_uuid,
                        target=SubmissionStatus.AWAITING_ADMIN,
                        allowed_from=(SubmissionStatus.VERIFYING,),
                    )
                    await append_audit(
                        conn,
                        actor=actor,
                        event="submission_awaiting_admin",
                        submission_uuid=sub.submission_uuid,
                        order_uuid=order.client_order_uuid,
                    )

    async def _finalize_completed_order(
        self,
        adapter: ExchangeAdapter,
        order: Order,
        actor: str,
    ) -> OrderStatus:
        """raw_status == 'completed': ACTIVE -> VERIFYING -> COMPLETED / FAILED.

        For task-exchange orders, audit HIGH-5 fix: do NOT close the order while
        any local submission is still non-terminal (NEW / VERIFYING /
        AWAITING_ADMIN / ACCEPTING / REJECTING). Keep it in VERIFYING and let a
        future poll close it once all submissions reach a terminal state.
        """
        if order.status == OrderStatus.ACTIVE:
            async with connect(self._settings) as conn:
                claimed = await claim_order_status(
                    conn,
                    order.client_order_uuid,
                    target=OrderStatus.VERIFYING,
                    allowed_from=(OrderStatus.ACTIVE,),
                    raw_exchange_status="completed",
                )
                if not claimed:
                    current = await get_order(conn, order.client_order_uuid)
                    return current.status if current else order.status

        if isinstance(adapter, PanelAdapter):
            verdict = verify_panel_completion(order)
            if (
                not self._settings.dry_run
                and verdict.verdict == VerificationVerdict.AUTO_PASS
                and verdict.reason.startswith("demo-verdict:")
            ):
                verdict = VerificationResult(
                    verdict=VerificationVerdict.NEEDS_HUMAN_REVIEW,
                    measured=verdict.measured,
                    expected=verdict.expected,
                    reason="live mode: demo panel verifier cannot auto-complete paid orders",
                    raw_evidence=verdict.raw_evidence,
                )
                async with connect(self._settings) as conn:
                    await append_audit(
                        conn,
                        actor=actor,
                        event="panel_verification",
                        order_uuid=order.client_order_uuid,
                        details={"verdict": verdict.verdict.value, "reason": verdict.reason},
                    )
                return OrderStatus.VERIFYING
            final = (
                OrderStatus.COMPLETED
                if verdict.verdict == VerificationVerdict.AUTO_PASS
                else OrderStatus.FAILED
            )
            async with connect(self._settings) as conn:
                await append_audit(
                    conn,
                    actor=actor,
                    event="panel_verification",
                    order_uuid=order.client_order_uuid,
                    details={"verdict": verdict.verdict.value, "reason": verdict.reason},
                )
                claimed = await claim_order_status(
                    conn,
                    order.client_order_uuid,
                    target=final,
                    allowed_from=(OrderStatus.VERIFYING,),
                    raw_exchange_status="completed",
                )
                if not claimed:
                    current = await get_order(conn, order.client_order_uuid)
                    return current.status if current else order.status
                await append_audit(
                    conn,
                    actor=actor,
                    event=f"order_{final.value}",
                    order_uuid=order.client_order_uuid,
                )
            return final

        # Task-exchange branch (HIGH-5 fix).
        async with connect(self._settings) as conn:
            pending = await count_non_terminal_submissions_for_order(conn, order.client_order_uuid)
        if pending > 0:
            async with connect(self._settings) as conn:
                await append_audit(
                    conn,
                    actor=actor,
                    event="order_completion_pending",
                    order_uuid=order.client_order_uuid,
                    details={"pending_submissions": pending},
                )
            return OrderStatus.VERIFYING  # keep open until all submissions terminal

        async with connect(self._settings) as conn:
            claimed = await claim_order_status(
                conn,
                order.client_order_uuid,
                target=OrderStatus.COMPLETED,
                allowed_from=(OrderStatus.VERIFYING,),
                raw_exchange_status="completed",
            )
            if not claimed:
                current = await get_order(conn, order.client_order_uuid)
                return current.status if current else order.status
            await append_audit(
                conn,
                actor=actor,
                event="order_completed",
                order_uuid=order.client_order_uuid,
            )
        return OrderStatus.COMPLETED

    # --- C2 money actions ----------------------------------------------------

    async def accept_submission(self, submission_uuid: str, decided_by: str) -> bool:
        """C2 pattern. Returns True iff this call ran the external action."""
        return await self._decide_submission(
            submission_uuid,
            target_in_progress=SubmissionStatus.ACCEPTING,
            target_terminal=SubmissionStatus.ACCEPTED,
            action="accept",
            decided_by=decided_by,
            reason=None,
        )

    async def reject_submission(self, submission_uuid: str, reason: str, decided_by: str) -> bool:
        """C2 pattern for rework. Returns True iff this call ran the external action."""
        return await self._decide_submission(
            submission_uuid,
            target_in_progress=SubmissionStatus.REJECTING,
            target_terminal=SubmissionStatus.REWORK_REQUESTED,
            action="reject",
            decided_by=decided_by,
            reason=reason,
        )

    async def _decide_submission(
        self,
        submission_uuid: str,
        *,
        target_in_progress: SubmissionStatus,
        target_terminal: SubmissionStatus,
        action: str,
        decided_by: str,
        reason: str | None,
    ) -> bool:
        async with connect(self._settings) as conn:
            sub = await get_submission(conn, submission_uuid)
        if sub is None:
            raise ValueError(f"submission {submission_uuid!r} not found")
        async with connect(self._settings) as conn:
            order = await get_order(conn, sub.order_uuid)
        if order is None:
            raise ValueError(
                f"order {sub.order_uuid!r} not found for submission {submission_uuid!r}"
            )
        adapter = self.adapter_for(order.spec.exchange)
        if not isinstance(adapter, TaskExchangeAdapter):
            raise TypeError(f"{action} called for non-task-exchange adapter {adapter.name!r}")

        # HIGH-3 fix: claim + action_log open in a SINGLE transaction.
        action_uuid = str(uuid.uuid4())
        async with connect(self._settings) as conn:
            claimed = await claim_submission_and_open_action(
                conn,
                submission_uuid,
                target=target_in_progress,
                allowed_from=(
                    SubmissionStatus.NEW,
                    SubmissionStatus.VERIFYING,
                    SubmissionStatus.AWAITING_ADMIN,
                ),
                action_uuid=action_uuid,
                action=action,
                order_uuid=sub.order_uuid,
            )
        if not claimed:
            return False

        # EXTERNAL CALL (outside any open DB transaction).
        assert sub.external_submission_id is not None
        assert order.external_order_id is not None
        try:
            if action == "accept":
                await adapter.accept_submission(order.external_order_id, sub.external_submission_id)
            else:
                await adapter.reject_submission(
                    order.external_order_id,
                    sub.external_submission_id,
                    reason or "",
                )
        except Exception as exc:
            async with connect(self._settings) as conn:
                # Force-set FAILED regardless of current status; this caller is the
                # unique claimer and the action just errored.
                cursor = await conn.execute(
                    "UPDATE submissions SET status = ?, updated_at = ? WHERE submission_uuid = ?",
                    (
                        SubmissionStatus.FAILED.value,
                        datetime.now(UTC).isoformat(timespec="seconds"),
                        submission_uuid,
                    ),
                )
                if cursor.rowcount != 1:
                    # Submission row disappeared mid-action; extremely unlikely. Surface it.
                    raise RuntimeError(
                        f"_decide_submission: submission {submission_uuid!r} vanished"
                    ) from exc
                await conn.commit()
                await complete_action_log(
                    conn, action_uuid=action_uuid, state="failed", error=str(exc)
                )
                await append_audit(
                    conn,
                    actor=decided_by,
                    event=f"submission_{action}_failed",
                    submission_uuid=submission_uuid,
                    order_uuid=sub.order_uuid,
                    details={"error": str(exc)},
                )
            raise

        # SUCCESS: terminal payment row (INSERT OR IGNORE), terminal submission
        # status (conditional UPDATE: only the unique claimer expects ACCEPTING /
        # REJECTING here), action_log -> succeeded, audit entry.
        async with connect(self._settings) as conn:
            await record_payment(
                conn,
                exchange=order.spec.exchange,
                external_submission_id=sub.external_submission_id,
                action=action,
                submission_uuid=submission_uuid,
                decided_by=decided_by,
            )
            terminal_claimed = await claim_submission_action(
                conn,
                submission_uuid,
                target=target_terminal,
                allowed_from=(target_in_progress,),
            )
            if not terminal_claimed:
                # Shouldn't happen - we held the claim. Surface for audit.
                raise RuntimeError(
                    f"_decide_submission: failed to transition "
                    f"{submission_uuid!r} {target_in_progress.value} -> "
                    f"{target_terminal.value}"
                )
            await complete_action_log(conn, action_uuid=action_uuid, state="succeeded")
            await append_audit(
                conn,
                actor=decided_by,
                event=f"submission_{target_terminal.value}",
                submission_uuid=submission_uuid,
                order_uuid=sub.order_uuid,
                details={"decided_by": decided_by, "reason": reason},
            )
        return True

    # --- C1 startup reconciliation -------------------------------------------

    async def reconcile_creating(
        self,
        actor: str = "reconcile",
        min_age_seconds: int = 300,
    ) -> int:
        """Move ORPHAN CREATING orders to FAILED.

        Day 3 audit HIGH-4 fix: only act on rows older than `min_age_seconds`
        (default 5 minutes) so we never race with an in-flight `create_order`
        whose `mark_order_active` is still pending.

        For a true crash-mid-placement (process killed between adapter call and
        `mark_order_active`), the conservative choice remains: mark FAILED +
        audit, let a human reconcile against the exchange dashboard. Per-adapter
        exchange-side lookup is future scope.

        Returns the number of rows reconciled.
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
