"""APScheduler jobs. Started from `app.main.lifespan` so the scheduler shares
the FastAPI app's AppState (adapters, verifiers, event bus, http client)."""

from __future__ import annotations

import asyncio
import html
import json
import logging
from datetime import UTC, datetime, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from adapters.base import Capability
from app.state import (
    AppState,
    cached_balance,
    emit_event,
    get_snapshot_for_order,
    list_pending_topups,
    resolve_topup_request,
)
from db.database import (
    append_audit,
    claim_order_status,
    connect,
    latest_verification_measured,
    list_completed_orders_in_window,
    list_order_uuids_by_status,
    list_unpushed_report_rows,
    mark_report_rows_pushed,
    record_report_row,
)
from models import OrderStatus, SourcePlatform, TaskType, VerificationVerdict
from verification.base import select_verifier

_LOG = logging.getLogger("scheduler")


def start_scheduler(state: AppState) -> AsyncIOScheduler:
    """Build + start an AsyncIOScheduler bound to the running event loop."""
    interval = max(state.settings.verifier_poll_interval_seconds, 15)
    recheck_interval = max(state.settings.recheck_interval_seconds, 300)
    sched = AsyncIOScheduler(timezone="UTC")
    sched.add_job(
        heartbeat,
        trigger="interval",
        seconds=interval,
        args=[state],
        id="heartbeat",
        max_instances=1,
        coalesce=True,
        next_run_time=datetime.now(UTC),  # fire immediately on startup
    )
    sched.add_job(
        recheck_completed_orders,
        trigger="interval",
        seconds=recheck_interval,
        args=[state],
        id="recheck_completed_orders",
        max_instances=1,
        coalesce=True,
    )
    # Daily Google Sheets push — 10:00 Moscow time (UTC+3 -> 07:00 UTC).
    # Synced with the Telegram digest so both morning reports land together.
    # The job is idempotent (partial unique index on order_uuid) so running
    # daily never duplicates rows. Also safe to call ad-hoc via /api/sheets/sync_now.
    sched.add_job(
        push_report_rows_to_sheets,
        trigger="cron",
        hour=7,
        minute=0,
        args=[state],
        id="daily_sheets_push",
        max_instances=1,
        coalesce=True,
    )
    # Daily digest to Telegram admin(s) — 10:00 MSK = 07:00 UTC.
    sched.add_job(
        send_daily_digest,
        trigger="cron",
        hour=7,
        minute=0,
        args=[state],
        id="daily_digest_telegram",
        max_instances=1,
        coalesce=True,
    )
    sched.start()
    print(
        f"[scheduler] started; heartbeat every {interval}s, "
        f"anti-fraud recheck every {recheck_interval}s, "
        "daily sheets push 10:00 MSK, daily digest 10:00 MSK"
    )
    return sched


async def heartbeat(state: AppState) -> None:
    """One pass: poll active orders → process submissions → verify completed → check topups."""
    try:
        await poll_active_orders(state)
    except Exception as e:
        _LOG.exception("poll_active_orders failed: %r", e)
    try:
        await poll_task_exchange_submissions(state)
    except Exception as e:
        _LOG.exception("poll_task_exchange_submissions failed: %r", e)
    try:
        await verify_completed_orders(state)
    except Exception as e:
        _LOG.exception("verify_completed_orders failed: %r", e)
    try:
        await recheck_balance_after_topup(state)
    except Exception as e:
        _LOG.exception("recheck_balance_after_topup failed: %r", e)


# ===== Job 1: poll active orders =====


async def poll_active_orders(state: AppState) -> None:
    """For each ACTIVE order: ask the adapter for current status; transition
    ACTIVE → COMPLETED (or FAILED) when the exchange says so."""
    async with connect(state.settings) as conn:
        uuids = await list_order_uuids_by_status(conn, OrderStatus.ACTIVE)
        # Pull external_id + exchange in a second query to avoid coupling.
        if not uuids:
            return
        placeholders = ",".join("?" for _ in uuids)
        cursor = await conn.execute(
            f"""
            SELECT client_order_uuid, exchange, external_order_id
            FROM orders WHERE client_order_uuid IN ({placeholders})
            """,
            uuids,
        )
        rows = await cursor.fetchall()

    for row in rows:
        exchange = row["exchange"]
        external_id = row["external_order_id"]
        uuid_ = row["client_order_uuid"]
        if not external_id:
            continue
        adapter = state.adapters.get(exchange)
        if adapter is None or Capability.GET_ORDER_STATUS not in adapter.capabilities():
            continue
        try:
            fresh = await adapter.get_order_status(external_id)
        except Exception as e:
            await emit_event(
                state.settings,
                kind="poll_error",
                payload={"exchange": exchange, "order_uuid": uuid_, "error": repr(e)},
                order_uuid=uuid_,
            )
            continue
        if fresh == "completed":
            try:
                async with connect(state.settings) as conn:
                    claimed = await claim_order_status(
                        conn,
                        uuid_,
                        target=OrderStatus.VERIFYING,
                        allowed_from=(OrderStatus.ACTIVE,),
                        raw_exchange_status=fresh,
                    )
                    if not claimed:
                        continue
                    await append_audit(
                        conn,
                        actor="scheduler",
                        event="order_active_to_verifying",
                        order_uuid=uuid_,
                        details={"raw": fresh},
                    )
                ev = await emit_event(
                    state.settings,
                    kind="order_status_changed",
                    payload={"from": "active", "to": "verifying", "exchange": exchange},
                    order_uuid=uuid_,
                )
                await state.event_bus.publish(ev)
            except RuntimeError:
                # row not in ACTIVE — already transitioned by a competing thread; skip
                pass
        elif fresh == "failed":
            try:
                async with connect(state.settings) as conn:
                    claimed = await claim_order_status(
                        conn,
                        uuid_,
                        target=OrderStatus.FAILED,
                        allowed_from=(OrderStatus.ACTIVE,),
                        raw_exchange_status=fresh,
                    )
                    if not claimed:
                        continue
                    await append_audit(
                        conn,
                        actor="scheduler",
                        event="order_active_to_failed",
                        order_uuid=uuid_,
                        details={"raw": fresh},
                    )
                ev = await emit_event(
                    state.settings,
                    kind="order_status_changed",
                    payload={"from": "active", "to": "failed", "exchange": exchange},
                    order_uuid=uuid_,
                )
                await state.event_bus.publish(ev)
            except RuntimeError:
                pass
        # 'in_progress' or unknown: keep ACTIVE


# ===== Job 1b: task-exchange submission lifecycle =====


async def poll_task_exchange_submissions(state: AppState) -> None:
    """For ACTIVE orders on TaskExchangeAdapter (unu/advego/ipgold), drive the
    submission state machine via Orchestrator.poll_order. Each call lists new
    submissions, persists them race-safe, runs verify_submission, and
    auto-accepts/rejects per the existing C2 idempotency pattern.

    Without this, task-exchange orders sit ACTIVE forever — placement works but
    no executor report ever gets processed.
    """
    from adapters.base import TaskExchangeAdapter
    from orchestrator import Orchestrator

    async with connect(state.settings) as conn:
        uuids = await list_order_uuids_by_status(conn, OrderStatus.ACTIVE)
        if not uuids:
            return
        placeholders = ",".join("?" for _ in uuids)
        cursor = await conn.execute(
            f"SELECT client_order_uuid, exchange FROM orders "
            f"WHERE client_order_uuid IN ({placeholders})",
            uuids,
        )
        rows = await cursor.fetchall()

    orch = Orchestrator(state.settings, state.adapters)
    for row in rows:
        adapter = state.adapters.get(row["exchange"])
        if not isinstance(adapter, TaskExchangeAdapter):
            continue
        try:
            await orch.poll_order(row["client_order_uuid"], actor="scheduler:submissions")
        except Exception as e:
            await emit_event(
                state.settings,
                kind="poll_error",
                payload={
                    "exchange": row["exchange"],
                    "order_uuid": row["client_order_uuid"],
                    "scope": "submissions",
                    "error": repr(e),
                },
                order_uuid=row["client_order_uuid"],
            )


# ===== Job 2: verify completed orders =====


async def _fetch_metric_for_snapshot(verifier, snap: dict, metric: TaskType) -> float | None:
    raw = snap.get("raw") if isinstance(snap.get("raw"), dict) else {}
    if (
        raw.get("counting_mode") == "fixed_window_from_snapshot"
        and hasattr(verifier, "fetch_metric_since")
    ):
        return await verifier.fetch_metric_since(snap["target_url"], metric, snap["captured_at"])
    return await verifier.fetch_metric(snap["target_url"], metric)


async def verify_completed_orders(state: AppState) -> None:
    """For each order in VERIFYING (set by poll job) without a verification row,
    run the matching verifier and finalize as COMPLETED or FAILED."""
    async with connect(state.settings) as conn:
        cursor = await conn.execute(
            """
            SELECT o.client_order_uuid, o.quantity, o.exchange, o.source_platform
            FROM orders o
            WHERE o.status = ?
            AND NOT EXISTS (SELECT 1 FROM verifications v WHERE v.order_uuid = o.client_order_uuid)
            """,
            (OrderStatus.VERIFYING.value,),
        )
        rows = await cursor.fetchall()

    for row in rows:
        uuid_ = row["client_order_uuid"]
        expected = float(row["quantity"])
        snap = await get_snapshot_for_order(state.settings, uuid_)
        if snap is None:
            # No baseline — auto-fail so admin can investigate.
            await _finalize_no_snapshot(state, uuid_, expected)
            continue
        try:
            platform = SourcePlatform(snap["platform"])
            metric = TaskType(snap["metric"])
        except ValueError:
            await _finalize_no_snapshot(state, uuid_, expected)
            continue
        verifier = select_verifier(state.verifiers, platform, metric)
        if verifier is None:
            await _record_verdict(
                state,
                uuid_,
                expected,
                snap["baseline_value"],
                None,
                VerificationVerdict.NEEDS_HUMAN_REVIEW,
                "no verifier wired for this platform/metric — admin to confirm manually",
                verifier_name="none",
            )
            # Stay in VERIFYING so the admin can re-trigger from the dashboard later.
            continue
        try:
            current = await _fetch_metric_for_snapshot(verifier, snap, metric)
        except Exception as e:
            current = None
            reason_extra = f" (verifier err: {e!r})"
        else:
            reason_extra = ""
        if current is None:
            await _record_verdict(
                state,
                uuid_,
                expected,
                snap["baseline_value"],
                None,
                VerificationVerdict.NEEDS_HUMAN_REVIEW,
                "verifier returned no value" + reason_extra,
                verifier_name=verifier.name,
            )
            continue
        baseline = float(snap["baseline_value"])
        delta = current - baseline
        # Upper bound (delta > 3x expected) rules out organic-growth false-positives
        # on popular URLs — see app/main.py:tool_check_delta for the same logic.
        organic_ceiling = expected * 3.0
        if expected * 0.8 <= delta <= organic_ceiling:
            verdict = VerificationVerdict.AUTO_PASS
            reason = f"delta {delta:.0f} within [80%, 300%] of expected {expected:.0f}"
            await _record_verdict(
                state, uuid_, expected, baseline, current, verdict, reason,
                verifier_name=verifier.name,
            )
            await _finalize_order(state, uuid_, OrderStatus.COMPLETED)
            await _push_report_for_order(state, uuid_, verdict, current, baseline, delta, expected)
        elif delta > organic_ceiling:
            verdict = VerificationVerdict.NEEDS_HUMAN_REVIEW
            reason = (
                f"delta {delta:.0f} > 300% of expected {expected:.0f} — "
                "likely organic noise, admin to confirm"
            )
            await _record_verdict(
                state, uuid_, expected, baseline, current, verdict, reason,
                verifier_name=verifier.name,
            )
            # Stay in VERIFYING — admin decides.
        elif delta < expected * 0.2:
            verdict = VerificationVerdict.FAIL
            reason = f"delta {delta:.0f} < 20% of expected {expected:.0f}"
            await _record_verdict(
                state, uuid_, expected, baseline, current, verdict, reason,
                verifier_name=verifier.name,
            )
            await _finalize_order(state, uuid_, OrderStatus.FAILED)
            await _push_report_for_order(state, uuid_, verdict, current, baseline, delta, expected)
        else:
            verdict = VerificationVerdict.NEEDS_HUMAN_REVIEW
            reason = f"delta {delta:.0f} between 20%–80% of expected {expected:.0f}"
            await _record_verdict(
                state, uuid_, expected, baseline, current, verdict, reason,
                verifier_name=verifier.name,
            )
            # Stay in VERIFYING — admin decides.


async def _finalize_no_snapshot(state: AppState, order_uuid: str, expected: float) -> None:
    await _record_verdict(
        state,
        order_uuid,
        expected,
        baseline=0.0,
        current=None,
        verdict=VerificationVerdict.FAIL,
        reason="missing baseline snapshot — cannot verify",
        verifier_name="none",
    )
    await _finalize_order(state, order_uuid, OrderStatus.FAILED)


async def _record_verdict(
    state: AppState,
    order_uuid: str,
    expected: float,
    baseline: float,
    current: float | None,
    verdict: VerificationVerdict,
    reason: str,
    *,
    verifier_name: str,
) -> None:
    import uuid as _uuid

    async with connect(state.settings) as conn:
        await conn.execute(
            """
            INSERT INTO verifications
            (verification_uuid, order_uuid, verdict, measured, expected, reason,
             raw_evidence_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                f"verif_{_uuid.uuid4().hex[:12]}",
                order_uuid,
                verdict.value,
                current,
                expected,
                reason,
                json.dumps(
                    {"baseline": baseline, "current": current, "verifier": verifier_name},
                    ensure_ascii=False,
                    default=str,
                ),
                datetime.now(UTC).isoformat(timespec="seconds"),
            ),
        )
        await conn.commit()
    ev = await emit_event(
        state.settings,
        kind="verification_completed",
        payload={
            "verdict": verdict.value,
            "baseline": baseline,
            "current": current,
            "expected": expected,
            "reason": reason,
            "verifier": verifier_name,
        },
        order_uuid=order_uuid,
    )
    await state.event_bus.publish(ev)


async def _finalize_order(state: AppState, order_uuid: str, target: OrderStatus) -> None:
    try:
        async with connect(state.settings) as conn:
            claimed = await claim_order_status(
                conn,
                order_uuid,
                target=target,
                allowed_from=(OrderStatus.VERIFYING,),
            )
            if not claimed:
                return
            await append_audit(
                conn,
                actor="scheduler",
                event=f"order_verifying_to_{target.value}",
                order_uuid=order_uuid,
            )
        ev = await emit_event(
            state.settings,
            kind="order_status_changed",
            payload={"from": "verifying", "to": target.value},
            order_uuid=order_uuid,
        )
        await state.event_bus.publish(ev)
        # Day 2: weekly Google Sheets feed. Write a snapshot of the terminal
        # state to report_rows; partial unique index on order_uuid keeps this
        # idempotent against duplicate _finalize_order calls.
        await _write_report_row_for(state, order_uuid, target)
    except RuntimeError:
        pass


async def _write_report_row_for(
    state: AppState,
    order_uuid: str,
    target: OrderStatus,
) -> None:
    async with connect(state.settings) as conn:
        cursor = await conn.execute(
            "SELECT exchange, source_platform, quantity, cost_actual "
            "FROM orders WHERE client_order_uuid = ?",
            (order_uuid,),
        )
        row = await cursor.fetchone()
        if row is None:
            return
        measured = await latest_verification_measured(conn, order_uuid)
        # ordered_count is the order's expected quantity; actual_count is the
        # measured value from the latest verification (may be None for orders
        # that never got a real verifier — placeholder snapshots, etc).
        actual: int | None = None
        if measured is not None:
            try:
                actual = int(measured)
            except (TypeError, ValueError):
                actual = None
        try:
            await record_report_row(
                conn,
                order_uuid=order_uuid,
                source_platform=row["source_platform"] or "",
                exchange=row["exchange"],
                ordered_count=int(row["quantity"]),
                actual_count=actual,
                cost=row["cost_actual"],
                status=target.value,
            )
        except Exception as e:
            await emit_event(
                state.settings,
                kind="report_row_write_failed",
                payload={"order_uuid": order_uuid, "error": repr(e)},
                order_uuid=order_uuid,
            )


async def _push_report_for_order(
    state: AppState,
    order_uuid: str,
    verdict: VerificationVerdict,
    current: float | None,
    baseline: float,
    delta: float,
    expected: float,
) -> None:
    """Build a user-facing summary; push to Telegram for both the original
    chat (if known) and any admin in `telegram_admin_ids`. HTML formatting +
    deep-link to the dashboard drill-down so the operator can take action."""
    chat_id = await _find_user_chat_id(state, order_uuid)
    if verdict == VerificationVerdict.AUTO_PASS:
        prefix = "✅ <b>Заказ доставлен</b>"
    elif verdict == VerificationVerdict.FAIL:
        prefix = "❌ <b>Заказ не доставлен</b>"
    else:
        prefix = "⚠️ <b>Нужна проверка вручную</b>"
    delta_str = f"{delta:.0f}" if current is not None else "—"
    cur_str = f"{current:.0f}" if current is not None else "—"
    summary_html = (
        f"{prefix}\n"
        f"<code>{html.escape(order_uuid[:8])}…</code>\n"
        f"Метрика выросла на <b>{delta_str}</b> "
        f"(с {baseline:.0f} → {cur_str}, ожидалось +{expected:.0f})"
    )
    keyboard = _order_inline_keyboard(state.settings, order_uuid, verdict)

    # Persist the event regardless of whether we can deliver to Telegram.
    ev = await emit_event(
        state.settings,
        kind="report",
        payload={"summary_html": summary_html, "user_chat_id": chat_id, "auto": True},
        order_uuid=order_uuid,
    )
    await state.event_bus.publish(ev)

    if not state.settings.telegram_bot_token:
        return

    # Deduplicate recipients: original chat + admins (some setups have the user
    # as admin, no point in two messages).
    targets: list[int] = []
    seen: set[int] = set()
    if chat_id:
        targets.append(int(chat_id))
        seen.add(int(chat_id))
    for admin_id in state.settings.telegram_admin_ids:
        if int(admin_id) not in seen:
            targets.append(int(admin_id))
            seen.add(int(admin_id))

    for tid in targets:
        try:
            await _telegram_send_html(
                state.http_client,
                state.settings.telegram_bot_token,
                tid,
                summary_html,
                inline_keyboard=keyboard,
            )
        except Exception as e:
            await emit_event(
                state.settings,
                kind="telegram_push_failed",
                payload={"chat_id": tid, "error": repr(e)},
                order_uuid=order_uuid,
            )


def _order_inline_keyboard(settings, order_uuid: str, verdict: VerificationVerdict) -> list[list[dict]] | None:
    """One-button keyboard pointing at the dashboard drill-down. We don't use
    Telegram callback_data here — Telegram requires a polling/webhook
    dispatcher, and a single `url` button keeps the deploy headless. The
    dashboard's order modal has the Accept/Reject controls.

    Token is bundled into the URL so the operator gets one-tap auth in the
    browser; the dashboard sets a cookie + redirects to a clean URL.
    """
    base = settings.dashboard_base_url or _default_dashboard_url(settings)
    if not base:
        return None
    token = settings.dashboard_token
    drilldown = f"{base}/dashboard?token={token}#order={order_uuid}"
    label = "🔍 Открыть в дашборде"
    if verdict == VerificationVerdict.NEEDS_HUMAN_REVIEW:
        label = "👀 Подтвердить вручную"
    return [[{"text": label, "url": drilldown}]]


def _default_dashboard_url(settings) -> str:
    host = settings.app_host or "127.0.0.1"
    # 0.0.0.0 in app_host means "bind everywhere" — pick something useful for
    # the URL we send to humans. Default to the public hostname env var if set.
    public_host = settings.public_hostname or (host if host != "0.0.0.0" else "127.0.0.1")
    return f"http://{public_host}:{settings.app_port}"


async def _find_user_chat_id(state: AppState, order_uuid: str) -> int | None:
    """Walk agent_events for the most recent tool_call.place_order with user_chat_id."""
    async with connect(state.settings) as conn:
        cursor = await conn.execute(
            """
            SELECT payload_json FROM agent_events
            WHERE order_uuid = ? AND kind = 'tool_call'
            ORDER BY event_id ASC
            """,
            (order_uuid,),
        )
        rows = await cursor.fetchall()
    for row in rows:
        try:
            p = json.loads(row["payload_json"])
        except (json.JSONDecodeError, TypeError):
            continue
        if p.get("tool") == "place_order":
            chat_id = p.get("user_chat_id")
            if chat_id:
                try:
                    return int(chat_id)
                except (TypeError, ValueError):
                    pass
    return None


async def _telegram_send(client, bot_token: str, chat_id: int, text: str) -> None:
    """Legacy Markdown sender — kept for the topup-resolved path. New code
    should use _telegram_send_html (escapes are safer)."""
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}
    response = await client.post(url, json=payload, timeout=15.0)
    response.raise_for_status()


async def _telegram_send_html(
    client,
    bot_token: str,
    chat_id: int,
    html_text: str,
    *,
    inline_keyboard: list[list[dict]] | None = None,
    disable_notification: bool = False,
) -> None:
    """HTML-mode sender with optional inline keyboard. Caller MUST html-escape
    user-supplied substrings before composing `html_text`."""
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload: dict = {
        "chat_id": chat_id,
        "text": html_text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if disable_notification:
        payload["disable_notification"] = True
    if inline_keyboard:
        payload["reply_markup"] = {"inline_keyboard": inline_keyboard}
    response = await client.post(url, json=payload, timeout=15.0)
    response.raise_for_status()


# ===== Job 3: recheck balance after topup =====


async def recheck_balance_after_topup(state: AppState) -> None:
    """For each pending topup_request, refresh balance on that exchange; when
    it crosses the requested amount, mark resolved and notify the user."""
    pending = await list_pending_topups(state.settings)
    if not pending:
        return
    for tp in pending:
        adapter = state.adapters.get(tp["exchange"])
        if adapter is None:
            continue
        balance = await cached_balance(state.settings, adapter, force_refresh=True)
        if balance.amount + 1e-9 >= float(tp["requested_amount"]) and not balance.stale:
            await resolve_topup_request(state.settings, tp["topup_uuid"], "resolved")
            ev = await emit_event(
                state.settings,
                kind="topup_resolved",
                payload={
                    "exchange": tp["exchange"],
                    "amount": balance.amount,
                    "requested": tp["requested_amount"],
                    "topup_uuid": tp["topup_uuid"],
                },
            )
            await state.event_bus.publish(ev)
            chat_id = tp.get("user_chat_id")
            if chat_id and state.settings.telegram_bot_token:
                msg = (
                    f"💰 На бирже *{tp['exchange']}* появилось "
                    f"{balance.amount:.2f} {balance.currency} — баланс достаточен. "
                    f"Если автозапуск включён, я размещаю заказ сейчас. "
                    f"Иначе — напиши, что разместить."
                )
                try:
                    await _telegram_send(
                        state.http_client,
                        state.settings.telegram_bot_token,
                        int(chat_id),
                        msg,
                    )
                except Exception as e:
                    await emit_event(
                        state.settings,
                        kind="telegram_push_failed",
                        payload={"chat_id": chat_id, "error": repr(e)},
                    )


# ===== Job 4: anti-fraud re-check on completed orders =====


async def recheck_completed_orders(state: AppState) -> None:
    """Re-verify orders that completed within the configured window.

    Why: SMM panels often deliver counters that "stick" then decay within
    24-72h. A single final check at the moment of biz-side completion can't
    catch this; re-running the verifier at T+1h…T+72h does.

    Rule: if measured value dropped > `recheck_fraud_drop_threshold` (default
    30%) vs the previous reading, write a `verifications` row with
    verdict=fail + reason="fraud (recheck decay)" and emit a high-priority
    event. We do NOT flip order status back to FAILED — that's a manual
    decision (and on SMM panels the money was already spent). The signal
    surfaces in the dashboard + Telegram so the operator can switch exchanges.
    """
    settings = state.settings
    threshold = float(settings.recheck_fraud_drop_threshold)
    async with connect(settings) as conn:
        orders = await list_completed_orders_in_window(
            conn,
            min_age_seconds=settings.recheck_min_age_seconds,
            max_age_seconds=settings.recheck_max_age_seconds,
        )
    if not orders:
        return

    for order in orders:
        uuid_ = order["order_uuid"]
        snap = await get_snapshot_for_order(settings, uuid_)
        if snap is None:
            continue
        try:
            platform = SourcePlatform(snap["platform"])
            metric = TaskType(snap["metric"])
        except ValueError:
            continue
        verifier = select_verifier(state.verifiers, platform, metric)
        if verifier is None:
            continue  # nothing to re-check against
        try:
            current = await verifier.fetch_metric(snap["target_url"], metric)
        except Exception as exc:
            await emit_event(
                settings,
                kind="recheck_error",
                payload={"order_uuid": uuid_, "error": repr(exc)},
                order_uuid=uuid_,
            )
            continue
        if current is None:
            # Verifier transient miss — skip silently, next recheck will retry.
            continue

        async with connect(settings) as conn:
            prior = await latest_verification_measured(conn, uuid_)
        baseline = float(snap["baseline_value"])
        delta_now = current - baseline

        # If we have no prior measurement somehow, just record a baseline
        # recheck row and move on.
        if prior is None:
            await _record_verdict(
                state,
                uuid_,
                expected=float(order["quantity"]),
                baseline=baseline,
                current=current,
                verdict=VerificationVerdict.AUTO_PASS,
                reason="recheck baseline established",
                verifier_name=verifier.name,
            )
            continue

        # Decay = how much the measured metric dropped vs prior reading.
        # Use prior - current normalized by prior; only matters when current < prior.
        if prior > 0 and current < prior:
            drop_fraction = (prior - current) / prior
        else:
            drop_fraction = 0.0

        if drop_fraction > threshold:
            verdict = VerificationVerdict.FAIL
            reason = (
                f"fraud: recheck shows decay {drop_fraction:.0%} "
                f"(prior {prior:.0f} → current {current:.0f}, baseline {baseline:.0f}, "
                f"delta_now {delta_now:.0f})"
            )
            await _record_verdict(
                state, uuid_,
                expected=float(order["quantity"]),
                baseline=baseline, current=current,
                verdict=verdict, reason=reason, verifier_name=verifier.name,
            )
            fraud_payload = {
                "order_uuid": uuid_,
                "exchange": order["exchange"],
                "drop_fraction": round(drop_fraction, 3),
                "prior": prior,
                "current": current,
                "baseline": baseline,
            }
            await emit_event(
                settings,
                kind="verification_recheck_fraud",
                payload=fraud_payload,
                order_uuid=uuid_,
            )
            await push_failure_alert(
                state, "verification_recheck_fraud", fraud_payload, order_uuid=uuid_
            )
        else:
            await _record_verdict(
                state, uuid_,
                expected=float(order["quantity"]),
                baseline=baseline, current=current,
                verdict=VerificationVerdict.AUTO_PASS,
                reason=f"recheck stable (prior {prior:.0f} → current {current:.0f})",
                verifier_name=verifier.name,
            )


# ===== Job 5: daily Google Sheets push =====


async def push_report_rows_to_sheets(state: AppState) -> dict:
    """Pull unpushed report_rows, append them to the operator's Google Sheet,
    then mark them as synced. Idempotent (partial unique index on order_uuid) —
    repeated runs only push new rows, so the daily cron never duplicates.

    Returns a small summary dict (also used by the manual sync endpoint).
    """
    writer = state.sheets_writer
    if writer is None:
        await emit_event(
            state.settings,
            kind="sheets_push_skipped",
            payload={"reason": "sheets_writer not configured (set GOOGLE_SHEETS_*)"},
        )
        return {"pushed": 0, "skipped": 0, "reason": "not_configured"}

    async with connect(state.settings) as conn:
        rows = await list_unpushed_report_rows(conn, limit=500)
    if not rows:
        return {"pushed": 0, "skipped": 0, "reason": "no_unpushed"}

    try:
        result = await writer.append_rows(rows)
    except Exception as exc:
        ev = await emit_event(
            state.settings,
            kind="sheets_push_failed",
            payload={"error": repr(exc), "row_count": len(rows)},
        )
        await state.event_bus.publish(ev)
        return {"pushed": 0, "skipped": 0, "error": repr(exc)}

    if result.pushed > 0:
        async with connect(state.settings) as conn:
            await mark_report_rows_pushed(conn, [r["row_id"] for r in rows[: result.pushed]])

    ev = await emit_event(
        state.settings,
        kind="sheets_push_completed",
        payload={
            "pushed": result.pushed,
            "skipped": result.skipped,
            "spreadsheet": result.spreadsheet_title,
            "tab": result.tab_title,
        },
    )
    await state.event_bus.publish(ev)
    return {
        "pushed": result.pushed,
        "skipped": result.skipped,
        "spreadsheet": result.spreadsheet_title,
        "tab": result.tab_title,
    }


# ===== Job 6: daily Telegram digest =====


async def send_daily_digest(state: AppState) -> dict:
    """Push a one-screen summary to each admin chat.

    Aggregates last 24h of orders: counts by status, money spent, top exchanges,
    fraud hits. Designed so an admin can read it half-asleep over morning coffee.
    Idempotent — same content every run, no DB writes (just events).
    """
    settings = state.settings
    if not settings.telegram_bot_token or not settings.telegram_admin_ids:
        return {"sent": 0, "reason": "telegram not configured"}

    now = datetime.now(UTC)
    since = now - timedelta(hours=24)
    since_iso = since.isoformat(timespec="seconds")

    async with connect(settings) as conn:
        # All orders created in the last 24h (for context: load/spend).
        cursor = await conn.execute(
            """
            SELECT status, exchange, source_platform, cost_actual
            FROM orders WHERE created_at >= ?
            """,
            (since_iso,),
        )
        orders = await cursor.fetchall()
        # Orders that REACHED terminal state in the last 24h (regardless of
        # when they were placed) — this is what the operator actually wants
        # to know about in the morning digest.
        cursor = await conn.execute(
            """
            SELECT client_order_uuid, exchange, source_platform, quantity,
                   cost_actual, status, target, updated_at
            FROM orders
            WHERE status IN ('completed', 'failed')
              AND updated_at >= ?
            ORDER BY updated_at DESC
            """,
            (since_iso,),
        )
        finalized = await cursor.fetchall()
        # Recheck fraud count for context.
        cursor = await conn.execute(
            """
            SELECT COUNT(*) AS c FROM agent_events
            WHERE occurred_at >= ? AND kind = 'verification_recheck_fraud'
            """,
            (since_iso,),
        )
        fraud_row = await cursor.fetchone()

    fraud_count = int(fraud_row["c"]) if fraud_row else 0
    total = len(orders)
    by_status: dict[str, int] = {}
    by_exchange: dict[str, int] = {}
    spent = 0.0
    for o in orders:
        by_status[o["status"]] = by_status.get(o["status"], 0) + 1
        by_exchange[o["exchange"]] = by_exchange.get(o["exchange"], 0) + 1
        if o["cost_actual"] is not None:
            try:
                spent += float(o["cost_actual"])
            except (TypeError, ValueError):
                pass

    finalized_completed = [f for f in finalized if f["status"] == "completed"]
    finalized_failed = [f for f in finalized if f["status"] == "failed"]
    completed = by_status.get("completed", 0)
    failed = by_status.get("failed", 0)
    verifying = by_status.get("verifying", 0)
    active = by_status.get("active", 0)
    awaiting_review = verifying  # orders stuck in verifying = needing eyes

    date_str = now.strftime("%d.%m.%Y")
    lines = [
        f"📅 <b>Утренняя сводка · {html.escape(date_str)}</b>",
        "",
        f"<b>Проверено и закрыто за сутки: {len(finalized)}</b>",
        f"  ✅ Принято: <b>{len(finalized_completed)}</b>",
        f"  ❌ Возвращено / провал: <b>{len(finalized_failed)}</b>",
    ]
    # Brief list of the most recent finalized orders (cap at 5 so the message
    # stays readable on mobile).
    if finalized:
        lines.append("")
        lines.append("<b>Последние закрытые:</b>")
        for f in finalized[:5]:
            mark = "✅" if f["status"] == "completed" else "❌"
            target = (f["target"] or "")[:40]
            cost = f"{float(f['cost_actual']):.2f}₽" if f["cost_actual"] is not None else "—"
            lines.append(
                f"  {mark} {html.escape(f['exchange'])}/{html.escape(f['source_platform'] or '')} "
                f"qty={f['quantity']} · {cost} · <code>{html.escape(f['client_order_uuid'][:8])}</code>"
            )

    lines.append("")
    lines.append(
        f"📦 Создано за сутки: <b>{total}</b> · "
        f"⚠ На проверке: <b>{awaiting_review}</b> · 🔄 В работе: <b>{active}</b>"
    )
    lines.append(f"💰 Потрачено: <b>{spent:.2f}₽</b>")
    if fraud_count > 0:
        lines.append(
            f"🚨 Антифрод сработал: <b>{fraud_count}</b> "
            f"(метрика просела >30% за 24-72ч после закрытия)"
        )
    if by_exchange:
        top = sorted(by_exchange.items(), key=lambda x: -x[1])[:3]
        lines.append("Топ биржи: " + " · ".join(f"{e} {n}" for e, n in top))

    digest_html = "\n".join(lines)
    sent = 0
    errors: list[str] = []
    for admin_id in settings.telegram_admin_ids:
        try:
            await _telegram_send_html(
                state.http_client,
                settings.telegram_bot_token,
                int(admin_id),
                digest_html,
                disable_notification=False,
            )
            sent += 1
        except Exception as e:
            errors.append(f"{admin_id}: {e!r}")

    ev = await emit_event(
        settings,
        kind="daily_digest_sent",
        payload={
            "total": total,
            "completed": completed,
            "failed": failed,
            "awaiting_review": awaiting_review,
            "active": active,
            "spent": round(spent, 2),
            "fraud_count": fraud_count,
            "sent_to": sent,
            "errors": errors,
        },
    )
    await state.event_bus.publish(ev)
    return {"sent": sent, "total_orders_24h": total, "errors": errors}


# ===== Job 7: failure alerts =====


async def push_failure_alert(state: AppState, kind: str, payload: dict, order_uuid: str | None = None) -> None:
    """Synchronous-feeling alert to admin chats for high-severity events.

    Called inline from places that emit place_order_failed / poll_error etc.
    so the operator sees the problem fast. Quiet hours could be added later.
    """
    settings = state.settings
    if not settings.telegram_bot_token or not settings.telegram_admin_ids:
        return
    label = {
        "place_order_failed": "⚠️ <b>Не удалось разместить заказ</b>",
        "poll_error": "⚠️ <b>Ошибка опроса биржи</b>",
        "verification_recheck_fraud": "🚨 <b>Антифрод: метрика просела</b>",
        "low_balance": "💸 <b>Низкий баланс на бирже</b>",
        "sheets_push_failed": "📊 <b>Сбой записи в Google Sheets</b>",
    }.get(kind, f"⚠️ <b>{html.escape(kind)}</b>")
    summary = html.escape(json.dumps(payload, ensure_ascii=False, default=str)[:300])
    text = f"{label}\n<code>{summary}</code>"
    if order_uuid:
        text += f"\norder: <code>{html.escape(order_uuid[:8])}…</code>"

    for admin_id in settings.telegram_admin_ids:
        try:
            await _telegram_send_html(
                state.http_client,
                settings.telegram_bot_token,
                int(admin_id),
                text,
            )
        except Exception:
            pass  # alert failure — don't escalate (would loop)


# Tiny utility for tests / manual sanity.
async def main_oneshot() -> None:  # pragma: no cover
    from app.state import build_app_state

    state = await build_app_state()
    try:
        await heartbeat(state)
    finally:
        await state.http_client.aclose()


if __name__ == "__main__":  # pragma: no cover
    asyncio.run(main_oneshot())
