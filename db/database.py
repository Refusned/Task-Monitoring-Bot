"""SQLite layer: connection, schema init, and core query helpers.

WAL mode is enabled at connect time. SQLite is the single source of truth.
Money-related writes use short explicit transactions; long-running HTTP calls
happen OUTSIDE any open transaction (see CLAUDE.md -> C2).

State-machine transitions (`mark_order_active`, `update_order_status`,
`update_submission_status`) verify `cursor.rowcount` and raise on a no-op update
(HIGH-a fix from Day 1 audit). The Day 3 helpers (`claim_submission_action`,
`record_payment`, `record_action_log`) implement the C2 pattern: claim with a
conditional UPDATE, commit, do the external call, then record the terminal
state.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import aiosqlite

from config import Settings
from models import (
    Order,
    OrderSpec,
    OrderStatus,
    Submission,
    SubmissionStatus,
)

SCHEMA_PATH = Path(__file__).parent / "schema.sql"


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


@asynccontextmanager
async def connect(settings: Settings) -> AsyncIterator[aiosqlite.Connection]:
    """Open a connection with WAL mode, FKs, and row factory enabled."""
    async with aiosqlite.connect(settings.db_path) as conn:
        await conn.execute("PRAGMA journal_mode = WAL;")
        await conn.execute("PRAGMA foreign_keys = ON;")
        conn.row_factory = aiosqlite.Row
        yield conn


async def init_db(settings: Settings) -> None:
    """Apply schema. Idempotent - uses CREATE TABLE IF NOT EXISTS.

    Also runs lightweight in-place ALTER TABLE migrations for columns added
    after the table was first created on an existing prod DB. SQLite ALTER
    TABLE ADD COLUMN is fast and safe; re-running fails with a known message
    we swallow.
    """
    schema_sql = SCHEMA_PATH.read_text(encoding="utf-8")
    async with connect(settings) as conn:
        await conn.executescript(schema_sql)
        # Day 2: add order_uuid to report_rows on existing DBs (idempotent —
        # second run raises "duplicate column" which we swallow).
        for ddl in (
            "ALTER TABLE report_rows ADD COLUMN order_uuid TEXT",
            # Day 3: track which report_rows we've already pushed to Google
            # Sheets so the weekly cron is idempotent (only pushes new rows).
            "ALTER TABLE report_rows ADD COLUMN pushed_to_sheets_at TEXT",
        ):
            try:
                await conn.execute(ddl)
            except aiosqlite.OperationalError as exc:
                if "duplicate column" not in str(exc).lower():
                    raise
        # Now (after the column exists) build the unique index. Partial unique
        # so legacy NULL rows don't conflict with each other.
        await conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_report_rows_order_uuid "
            "ON report_rows(order_uuid) WHERE order_uuid IS NOT NULL"
        )
        await conn.commit()


# --- orders -------------------------------------------------------------------


async def _insert_order_creating_no_commit(conn: aiosqlite.Connection, order: Order) -> None:
    spec = order.spec
    await conn.execute(
        """
        INSERT INTO orders (
            client_order_uuid, exchange, scenario, target, quantity, service_id,
            source_platform, max_cost, status, spec_json, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            order.client_order_uuid,
            spec.exchange,
            spec.scenario.value,
            spec.target,
            spec.quantity,
            spec.service_id,
            spec.source_platform.value if spec.source_platform else None,
            spec.max_cost,
            OrderStatus.CREATING.value,
            spec.model_dump_json(),
            order.created_at.isoformat(timespec="seconds"),
            order.updated_at.isoformat(timespec="seconds"),
        ),
    )


async def insert_order_creating(conn: aiosqlite.Connection, order: Order) -> None:
    """C1: write the orders row in CREATING status BEFORE the external call."""
    await _insert_order_creating_no_commit(conn, order)
    await conn.commit()


async def reserve_order_creating_with_spend_limits(
    conn: aiosqlite.Connection,
    order: Order,
    *,
    per_order_limit: float,
    daily_limit: float,
) -> None:
    """Atomically reserve spend and insert a CREATING order row.

    Pending `CREATING` rows count by `max_cost`; acknowledged rows count by
    `cost_actual` when known. This closes the C1 daily-limit TOCTOU window where
    concurrent place_order calls could all pass the check before any row became
    visible as spend.
    """
    spec = order.spec
    if spec.max_cost > per_order_limit:
        raise ValueError(
            f"per-order spend limit exceeded: {spec.max_cost:.2f} > {per_order_limit:.2f} "
            "(set PER_ORDER_SPEND_LIMIT to raise)"
        )

    await conn.execute("BEGIN IMMEDIATE")
    try:
        cursor = await conn.execute(
            """
            SELECT COALESCE(SUM(
                CASE
                    WHEN status = 'creating' THEN max_cost
                    ELSE COALESCE(cost_actual, max_cost)
                END
            ), 0) AS reserved
            FROM orders
            WHERE date(created_at) = date('now')
              AND status IN ('creating', 'active', 'verifying', 'completed')
            """
        )
        row = await cursor.fetchone()
        reserved_today = float(row["reserved"] or 0.0) if row else 0.0
        if reserved_today + spec.max_cost > daily_limit:
            raise ValueError(
                f"daily spend limit would be exceeded: reserved {reserved_today:.2f} + "
                f"max_cost {spec.max_cost:.2f} > {daily_limit:.2f} "
                "(set DAILY_SPEND_LIMIT to raise)"
            )
        await _insert_order_creating_no_commit(conn, order)
        await conn.commit()
    except Exception:
        await conn.rollback()
        raise


async def mark_order_active(
    conn: aiosqlite.Connection,
    client_order_uuid: str,
    external_order_id: str,
    cost_actual: float,
) -> None:
    """C1: transition CREATING -> ACTIVE after the exchange acknowledged the order.

    Raises `RuntimeError` if the row is missing or not in CREATING state.
    """
    cursor = await conn.execute(
        """
        UPDATE orders SET status = ?, external_order_id = ?, cost_actual = ?, updated_at = ?
        WHERE client_order_uuid = ? AND status = ?
        """,
        (
            OrderStatus.ACTIVE.value,
            external_order_id,
            cost_actual,
            _now_iso(),
            client_order_uuid,
            OrderStatus.CREATING.value,
        ),
    )
    if cursor.rowcount != 1:
        raise RuntimeError(
            f"mark_order_active: expected 1 row update for {client_order_uuid}, "
            f"got {cursor.rowcount} (row missing or not in CREATING)"
        )
    await conn.commit()


async def update_order_status(
    conn: aiosqlite.Connection,
    client_order_uuid: str,
    new_status: OrderStatus,
    raw_exchange_status: str | None = None,
) -> None:
    """Update an order's status. Raises `RuntimeError` if the UUID is missing."""
    if raw_exchange_status is None:
        cursor = await conn.execute(
            "UPDATE orders SET status = ?, updated_at = ? WHERE client_order_uuid = ?",
            (new_status.value, _now_iso(), client_order_uuid),
        )
    else:
        cursor = await conn.execute(
            """
            UPDATE orders SET status = ?, updated_at = ?, raw_exchange_status = ?
            WHERE client_order_uuid = ?
            """,
            (new_status.value, _now_iso(), raw_exchange_status, client_order_uuid),
        )
    if cursor.rowcount != 1:
        raise RuntimeError(
            f"update_order_status: no row updated for {client_order_uuid} "
            f"(rowcount={cursor.rowcount}; UUID missing?)"
        )
    await conn.commit()


async def claim_order_status(
    conn: aiosqlite.Connection,
    client_order_uuid: str,
    *,
    target: OrderStatus,
    allowed_from: tuple[OrderStatus, ...],
    raw_exchange_status: str | None = None,
) -> bool:
    """Conditional order transition; returns True iff this caller won the claim."""
    placeholders = ",".join("?" for _ in allowed_from)
    params: tuple[Any, ...]
    if raw_exchange_status is None:
        params = (
            target.value,
            _now_iso(),
            client_order_uuid,
            *[status.value for status in allowed_from],
        )
        cursor = await conn.execute(
            f"""
            UPDATE orders SET status = ?, updated_at = ?
            WHERE client_order_uuid = ? AND status IN ({placeholders})
            """,
            params,
        )
    else:
        params = (
            target.value,
            _now_iso(),
            raw_exchange_status,
            client_order_uuid,
            *[status.value for status in allowed_from],
        )
        cursor = await conn.execute(
            f"""
            UPDATE orders SET status = ?, updated_at = ?, raw_exchange_status = ?
            WHERE client_order_uuid = ? AND status IN ({placeholders})
            """,
            params,
        )
    await conn.commit()
    return cursor.rowcount == 1


async def get_order(conn: aiosqlite.Connection, client_order_uuid: str) -> Order | None:
    cursor = await conn.execute(
        "SELECT * FROM orders WHERE client_order_uuid = ?", (client_order_uuid,)
    )
    row = await cursor.fetchone()
    if row is None:
        return None
    spec = OrderSpec.model_validate_json(row["spec_json"])
    return Order(
        client_order_uuid=row["client_order_uuid"],
        spec=spec,
        status=OrderStatus(row["status"]),
        external_order_id=row["external_order_id"],
        cost_actual=row["cost_actual"],
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
    )


async def list_order_uuids_by_status(conn: aiosqlite.Connection, status: OrderStatus) -> list[str]:
    cursor = await conn.execute(
        "SELECT client_order_uuid FROM orders WHERE status = ?", (status.value,)
    )
    return [row["client_order_uuid"] for row in await cursor.fetchall()]


async def list_order_uuids_in_status_older_than(
    conn: aiosqlite.Connection,
    status: OrderStatus,
    cutoff_iso: str,
) -> list[str]:
    """Day 3 audit HIGH-4 helper: orders in `status` with `created_at <= cutoff_iso`.

    Used by `reconcile_creating` to skip orders that may still be in flight
    (their creator just hasn't reached `mark_order_active` yet). The `<=`
    boundary means `min_age_seconds=0` matches rows created at-or-before now,
    which is the intuitive meaning (relevant for tests; in production
    `min_age_seconds` defaults to 300 anyway).
    """
    cursor = await conn.execute(
        "SELECT client_order_uuid FROM orders WHERE status = ? AND created_at <= ?",
        (status.value, cutoff_iso),
    )
    return [row["client_order_uuid"] for row in await cursor.fetchall()]


# --- submissions --------------------------------------------------------------


def _row_to_submission(row: aiosqlite.Row) -> Submission:
    return Submission(
        submission_uuid=row["submission_uuid"],
        order_uuid=row["order_uuid"],
        external_submission_id=row["external_submission_id"],
        executor_hint=row["executor_hint"],
        status=SubmissionStatus(row["status"]),
        evidence=row["evidence"],
        created_at=datetime.fromisoformat(row["created_at"]),
    )


async def insert_submission(conn: aiosqlite.Connection, sub: Submission) -> None:
    iso = sub.created_at.isoformat(timespec="seconds")
    await conn.execute(
        """
        INSERT INTO submissions (
            submission_uuid, order_uuid, external_submission_id, executor_hint,
            status, evidence, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            sub.submission_uuid,
            sub.order_uuid,
            sub.external_submission_id,
            sub.executor_hint,
            sub.status.value,
            sub.evidence,
            iso,
            iso,
        ),
    )
    await conn.commit()


async def ensure_submission_persisted(
    conn: aiosqlite.Connection,
    candidate: Submission,
    exchange: str,
) -> tuple[Submission, bool]:
    """Day 3 audit CRITICAL-1 fix: race-safe persistence of a submission.

    Uses INSERT OR IGNORE against the `UNIQUE(order_uuid, external_submission_id)`
    constraint, then re-fetches to return the canonical row. Two concurrent
    pollers converge on a single local submission_uuid for the same external id.

    Returns (canonical_submission, inserted_now). `inserted_now=False` means
    another caller persisted this submission first; we use the existing row.
    """
    if candidate.external_submission_id is None:
        # Without an external id we can't deduplicate; fall back to plain insert.
        await insert_submission(conn, candidate)
        return candidate, True

    iso = candidate.created_at.isoformat(timespec="seconds")
    cursor = await conn.execute(
        """
        INSERT OR IGNORE INTO submissions (
            submission_uuid, order_uuid, external_submission_id, executor_hint,
            status, evidence, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            candidate.submission_uuid,
            candidate.order_uuid,
            candidate.external_submission_id,
            candidate.executor_hint,
            candidate.status.value,
            candidate.evidence,
            iso,
            iso,
        ),
    )
    inserted_now = cursor.rowcount == 1
    await conn.commit()

    canonical = await get_submission_by_external(conn, exchange, candidate.external_submission_id)
    if canonical is None:
        raise RuntimeError(
            f"ensure_submission_persisted: re-fetch failed for "
            f"{candidate.external_submission_id!r} (exchange={exchange!r})"
        )
    return canonical, inserted_now


async def get_submission(conn: aiosqlite.Connection, submission_uuid: str) -> Submission | None:
    cursor = await conn.execute(
        "SELECT * FROM submissions WHERE submission_uuid = ?", (submission_uuid,)
    )
    row = await cursor.fetchone()
    return _row_to_submission(row) if row else None


async def get_submission_by_external(
    conn: aiosqlite.Connection,
    exchange: str,
    external_submission_id: str,
) -> Submission | None:
    cursor = await conn.execute(
        """
        SELECT s.* FROM submissions s
        JOIN orders o ON s.order_uuid = o.client_order_uuid
        WHERE o.exchange = ? AND s.external_submission_id = ?
        """,
        (exchange, external_submission_id),
    )
    row = await cursor.fetchone()
    return _row_to_submission(row) if row else None


async def update_submission_status(
    conn: aiosqlite.Connection,
    submission_uuid: str,
    new_status: SubmissionStatus,
) -> None:
    """Update a submission's status. Raises if the UUID is missing."""
    cursor = await conn.execute(
        "UPDATE submissions SET status = ?, updated_at = ? WHERE submission_uuid = ?",
        (new_status.value, _now_iso(), submission_uuid),
    )
    if cursor.rowcount != 1:
        raise RuntimeError(f"update_submission_status: no row updated for {submission_uuid}")
    await conn.commit()


async def claim_submission_action(
    conn: aiosqlite.Connection,
    submission_uuid: str,
    *,
    target: SubmissionStatus,
    allowed_from: tuple[SubmissionStatus, ...],
) -> bool:
    """C2: conditional UPDATE. Returns True if the claim succeeded (rowcount == 1).

    Transitions the submission to `target` only when its current status is one
    of `allowed_from`. The thread that gets True is the unique owner of the
    subsequent external call.
    """
    placeholders = ",".join("?" for _ in allowed_from)
    cursor = await conn.execute(
        f"""
        UPDATE submissions SET status = ?, updated_at = ?
        WHERE submission_uuid = ? AND status IN ({placeholders})
        """,
        (
            target.value,
            _now_iso(),
            submission_uuid,
            *[s.value for s in allowed_from],
        ),
    )
    await conn.commit()
    return cursor.rowcount == 1


async def claim_submission_and_open_action(
    conn: aiosqlite.Connection,
    submission_uuid: str,
    *,
    target: SubmissionStatus,
    allowed_from: tuple[SubmissionStatus, ...],
    action_uuid: str,
    action: str,
    order_uuid: str | None,
) -> bool:
    """Day 3 audit HIGH-1 fix: claim + action_log in a SINGLE transaction.

    The previous `claim_submission_action` followed by a separate
    `record_action_log` left a crash window where the submission was
    `ACCEPTING`/`REJECTING` but no action_log row existed. This helper commits
    both rows together so they are observed atomically.

    Returns True iff the claim succeeded.
    """
    now = _now_iso()
    placeholders = ",".join("?" for _ in allowed_from)
    cursor = await conn.execute(
        f"""
        UPDATE submissions SET status = ?, updated_at = ?
        WHERE submission_uuid = ? AND status IN ({placeholders})
        """,
        (target.value, now, submission_uuid, *[s.value for s in allowed_from]),
    )
    if cursor.rowcount != 1:
        # Claim lost; do not write action_log. No commit needed (no changes).
        return False
    await conn.execute(
        """
        INSERT INTO action_log
        (action_uuid, submission_uuid, order_uuid, action, state, started_at)
        VALUES (?, ?, ?, ?, 'in_progress', ?)
        """,
        (action_uuid, submission_uuid, order_uuid, action, now),
    )
    await conn.commit()
    return True


async def count_non_terminal_submissions_for_order(
    conn: aiosqlite.Connection, order_uuid: str
) -> int:
    """Day 3 audit HIGH-5 helper: number of submissions for an order that are
    NOT in a terminal state (ACCEPTED / REWORK_REQUESTED / FAILED).

    Used by `_finalize_completed_order` to keep a task-exchange order in
    VERIFYING while any submission still awaits admin or is mid-action.
    """
    cursor = await conn.execute(
        """
        SELECT COUNT(*) AS c FROM submissions
        WHERE order_uuid = ? AND status NOT IN (?, ?, ?)
        """,
        (
            order_uuid,
            SubmissionStatus.ACCEPTED.value,
            SubmissionStatus.REWORK_REQUESTED.value,
            SubmissionStatus.FAILED.value,
        ),
    )
    row = await cursor.fetchone()
    return int(row["c"]) if row else 0


# --- action_log (C2 in-flight tracking) ---------------------------------------


async def record_action_log(
    conn: aiosqlite.Connection,
    *,
    action_uuid: str,
    submission_uuid: str | None,
    order_uuid: str | None,
    action: str,
) -> None:
    """C2: open an action_log row in `in_progress` state before the external call."""
    await conn.execute(
        """
        INSERT INTO action_log
        (action_uuid, submission_uuid, order_uuid, action, state, started_at)
        VALUES (?, ?, ?, ?, 'in_progress', ?)
        """,
        (action_uuid, submission_uuid, order_uuid, action, _now_iso()),
    )
    await conn.commit()


async def complete_action_log(
    conn: aiosqlite.Connection,
    *,
    action_uuid: str,
    state: str,
    error: str | None = None,
) -> None:
    """C2: close an action_log row with terminal state ('succeeded' | 'failed')."""
    await conn.execute(
        "UPDATE action_log SET state = ?, error = ?, finished_at = ? WHERE action_uuid = ?",
        (state, error, _now_iso(), action_uuid),
    )
    await conn.commit()


# --- payments (C2 terminal money decisions) -----------------------------------


async def record_payment(
    conn: aiosqlite.Connection,
    *,
    exchange: str,
    external_submission_id: str,
    action: str,
    submission_uuid: str,
    decided_by: str,
) -> None:
    """C2: record the terminal money decision. The PRIMARY KEY
    (exchange, external_submission_id) guarantees uniqueness even on replay -
    INSERT OR IGNORE turns a re-record into a no-op.
    """
    await conn.execute(
        """
        INSERT OR IGNORE INTO payments
        (exchange, external_submission_id, action, submission_uuid, decided_at, decided_by)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            exchange,
            external_submission_id,
            action,
            submission_uuid,
            _now_iso(),
            decided_by,
        ),
    )
    await conn.commit()


# --- audit + misc -------------------------------------------------------------


async def append_audit(
    conn: aiosqlite.Connection,
    actor: str,
    event: str,
    order_uuid: str | None = None,
    submission_uuid: str | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    """Append-only audit log of every meaningful action."""
    await conn.execute(
        """
        INSERT INTO audit_log (occurred_at, actor, event, order_uuid, submission_uuid, details_json)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            _now_iso(),
            actor,
            event,
            order_uuid,
            submission_uuid,
            json.dumps(details or {}, ensure_ascii=False, default=str),
        ),
    )
    await conn.commit()


async def count_audit_entries(conn: aiosqlite.Connection) -> int:
    cursor = await conn.execute("SELECT COUNT(*) AS c FROM audit_log")
    row = await cursor.fetchone()
    return int(row["c"]) if row else 0


# --- report_rows (weekly Google Sheets export source) -----------------------


def _iso_week(dt: datetime) -> str:
    """ISO 8601 year-week, e.g. '2026-W22'. Stable across years; matches what
    spreadsheet operators typically expect."""
    year, week, _ = dt.isocalendar()
    return f"{year}-W{week:02d}"


async def record_report_row(
    conn: aiosqlite.Connection,
    *,
    order_uuid: str,
    source_platform: str,
    exchange: str,
    ordered_count: int,
    actual_count: int | None,
    cost: float | None,
    status: str,
) -> bool:
    """Persist one report row keyed by order_uuid. Idempotent: duplicate calls
    with the same order_uuid silently no-op (partial unique index).

    Returns True if the row was newly inserted, False if a row already existed.
    """
    week = _iso_week(datetime.now(UTC))
    cursor = await conn.execute(
        """
        INSERT OR IGNORE INTO report_rows
        (week, source_platform, exchange, ordered_count, actual_count, cost,
         status, created_at, order_uuid)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            week,
            source_platform,
            exchange,
            int(ordered_count),
            None if actual_count is None else int(actual_count),
            None if cost is None else float(cost),
            status,
            _now_iso(),
            order_uuid,
        ),
    )
    await conn.commit()
    return cursor.rowcount == 1


async def list_completed_orders_in_window(
    conn: aiosqlite.Connection,
    *,
    min_age_seconds: int,
    max_age_seconds: int,
) -> list[dict]:
    """For the recheck (anti-fraud) job: completed orders whose age is in
    [min_age_seconds, max_age_seconds], regardless of last-recheck time.

    `updated_at` is the proxy for completion time. A re-check happens at most
    once per scheduler interval; the verifier writes a new `verifications`
    row each pass, which is how the caller distinguishes runs.
    """
    cursor = await conn.execute(
        f"""
        SELECT client_order_uuid, exchange, source_platform, quantity, cost_actual,
               updated_at
        FROM orders
        WHERE status = ?
          AND (julianday('now') - julianday(updated_at)) * 86400 BETWEEN ? AND ?
        """,  # noqa: S608 (table+col names are static)
        (OrderStatus.COMPLETED.value, int(min_age_seconds), int(max_age_seconds)),
    )
    rows = await cursor.fetchall()
    return [
        {
            "order_uuid": r["client_order_uuid"],
            "exchange": r["exchange"],
            "source_platform": r["source_platform"],
            "quantity": int(r["quantity"]),
            "cost_actual": float(r["cost_actual"]) if r["cost_actual"] is not None else None,
            "updated_at": r["updated_at"],
        }
        for r in rows
    ]


async def list_unpushed_report_rows(
    conn: aiosqlite.Connection,
    limit: int = 500,
) -> list[dict]:
    """For the weekly Google Sheets cron: report_rows we haven't synced yet.

    Returns dicts with everything the sheet writer needs in stable column
    order (matches `RowDTO` in reporting/sheets.py).
    """
    cursor = await conn.execute(
        """
        SELECT row_id, week, source_platform, exchange, ordered_count,
               actual_count, cost, status, order_uuid, created_at
        FROM report_rows
        WHERE pushed_to_sheets_at IS NULL
        ORDER BY row_id ASC
        LIMIT ?
        """,
        (int(limit),),
    )
    rows = await cursor.fetchall()
    return [
        {
            "row_id": int(r["row_id"]),
            "week": r["week"],
            "source_platform": r["source_platform"],
            "exchange": r["exchange"],
            "ordered_count": int(r["ordered_count"]),
            "actual_count": (None if r["actual_count"] is None else int(r["actual_count"])),
            "cost": (None if r["cost"] is None else float(r["cost"])),
            "status": r["status"],
            "order_uuid": r["order_uuid"],
            "created_at": r["created_at"],
        }
        for r in rows
    ]


async def mark_report_rows_pushed(
    conn: aiosqlite.Connection,
    row_ids: list[int],
) -> int:
    """Mark report_rows as synced to Sheets. Returns updated count."""
    if not row_ids:
        return 0
    placeholders = ",".join("?" for _ in row_ids)
    cursor = await conn.execute(
        f"UPDATE report_rows SET pushed_to_sheets_at = ? "
        f"WHERE row_id IN ({placeholders}) AND pushed_to_sheets_at IS NULL",
        (_now_iso(), *row_ids),
    )
    await conn.commit()
    return cursor.rowcount or 0


async def latest_verification_measured(
    conn: aiosqlite.Connection,
    order_uuid: str,
) -> float | None:
    """Most recent `measured` value from any verifications row for this order.
    Used by recheck job to compare current vs prior reading and detect decay."""
    cursor = await conn.execute(
        """
        SELECT measured FROM verifications
        WHERE order_uuid = ? AND measured IS NOT NULL
        ORDER BY created_at DESC LIMIT 1
        """,
        (order_uuid,),
    )
    row = await cursor.fetchone()
    if row is None or row["measured"] is None:
        return None
    try:
        return float(row["measured"])
    except (TypeError, ValueError):
        return None
