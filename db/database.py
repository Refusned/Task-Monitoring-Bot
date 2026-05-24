"""SQLite layer: connection, schema init, and core query helpers.

WAL mode is enabled at connect time. SQLite is the single source of truth.
Money-related writes use short explicit transactions; long-running HTTP calls
happen OUTSIDE any open transaction (see CLAUDE.md -> C2).

State-machine transitions (`mark_order_active`, `update_order_status`) verify
`cursor.rowcount` and raise on a no-op update (HIGH-a fix from Day 1 audit) -
otherwise a missing UUID or wrong-state row would be silently audited as success.
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
    VerificationResult,
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
    """Apply schema. Idempotent - uses CREATE TABLE IF NOT EXISTS."""
    schema_sql = SCHEMA_PATH.read_text(encoding="utf-8")
    async with connect(settings) as conn:
        await conn.executescript(schema_sql)
        await conn.commit()


async def insert_order_creating(conn: aiosqlite.Connection, order: Order) -> None:
    """C1: write the orders row in CREATING status BEFORE the external call."""
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
    await conn.commit()


async def mark_order_active(
    conn: aiosqlite.Connection,
    client_order_uuid: str,
    external_order_id: str,
    cost_actual: float,
) -> None:
    """C1: transition CREATING -> ACTIVE after the exchange acknowledged the order.

    Raises `RuntimeError` if the row is missing or not in CREATING state - closes
    a silent-failure trap where the audit log would record `order_active` even
    though the UPDATE matched zero rows.
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
    """Update an order's status. Raises `RuntimeError` if the UUID is missing.

    HIGH-a fix from Day 1 audit: no silent no-ops on state-machine transitions.
    """
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


async def count_audit_entries(conn: aiosqlite.Connection) -> int:
    cursor = await conn.execute("SELECT COUNT(*) AS c FROM audit_log")
    row = await cursor.fetchone()
    return int(row["c"]) if row else 0


# --- Submission helpers (orchestrator Day 3) ---------------------------------


async def insert_submission(
    conn: aiosqlite.Connection,
    submission: Submission,
) -> None:
    await conn.execute(
        """
        INSERT INTO submissions (
            submission_uuid, order_uuid, external_submission_id,
            executor_hint, status, evidence, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            submission.submission_uuid,
            submission.order_uuid,
            submission.external_submission_id,
            submission.executor_hint,
            submission.status.value,
            submission.evidence,
            submission.created_at.isoformat(timespec="seconds"),
            submission.created_at.isoformat(timespec="seconds"),
        ),
    )
    await conn.commit()


async def get_submissions_for_order(
    conn: aiosqlite.Connection, order_uuid: str
) -> list[Submission]:
    cursor = await conn.execute("SELECT * FROM submissions WHERE order_uuid = ?", (order_uuid,))
    rows = await cursor.fetchall()
    result: list[Submission] = []
    for row in rows:
        result.append(
            Submission(
                submission_uuid=row["submission_uuid"],
                order_uuid=row["order_uuid"],
                external_submission_id=row["external_submission_id"],
                executor_hint=row["executor_hint"],
                status=SubmissionStatus(row["status"]),
                evidence=row["evidence"],
                created_at=datetime.fromisoformat(row["created_at"]),
            )
        )
    return result


async def update_submission_status(
    conn: aiosqlite.Connection,
    submission_uuid: str,
    new_status: SubmissionStatus,
) -> None:
    cursor = await conn.execute(
        """
        UPDATE submissions SET status = ?, updated_at = ? WHERE submission_uuid = ?
        """,
        (new_status.value, _now_iso(), submission_uuid),
    )
    if cursor.rowcount != 1:
        raise RuntimeError(f"update_submission_status: no row updated for {submission_uuid}")
    await conn.commit()


# --- Verification helpers ------------------------------------------------------


async def insert_verification(
    conn: aiosqlite.Connection,
    order_uuid: str,
    submission_uuid: str | None,
    result: VerificationResult,
) -> None:
    from uuid import uuid4

    await conn.execute(
        """
        INSERT INTO verifications (
            verification_uuid, order_uuid, submission_uuid,
            verdict, measured, expected, reason, raw_evidence_json, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            str(uuid4()),
            order_uuid,
            submission_uuid,
            result.verdict.value,
            result.measured,
            result.expected,
            result.reason,
            json.dumps(result.raw_evidence, ensure_ascii=False, default=str),
            _now_iso(),
        ),
    )
    await conn.commit()


# --- Money-safety helpers (C2) -------------------------------------------------


async def try_claim_payment_action(
    conn: aiosqlite.Connection,
    action_uuid: str,
    submission_uuid: str,
    order_uuid: str,
    action: str,
) -> bool:
    """Claim an in-progress action in action_log. Returns True if claimed.

    If a terminal row already exists in payments for the same
    (exchange, external_submission_id), this still returns True because
    the orchestrator checks payments before the external call.
    """
    started = _now_iso()
    await conn.execute(
        """
        INSERT INTO action_log (
            action_uuid, submission_uuid, order_uuid, action, state, started_at
        ) VALUES (?, ?, ?, ?, 'in_progress', ?)
        """,
        (action_uuid, submission_uuid, order_uuid, action, started),
    )
    await conn.commit()
    return True


async def record_action_success(
    conn: aiosqlite.Connection,
    action_uuid: str,
) -> None:
    finished = _now_iso()
    cursor = await conn.execute(
        """
        UPDATE action_log
        SET state = 'succeeded', finished_at = ?
        WHERE action_uuid = ?
        """,
        (finished, action_uuid),
    )
    if cursor.rowcount != 1:
        raise RuntimeError(f"record_action_success: no row for {action_uuid}")
    await conn.commit()


async def record_action_failure(
    conn: aiosqlite.Connection,
    action_uuid: str,
    error: str,
) -> None:
    finished = _now_iso()
    cursor = await conn.execute(
        """
        UPDATE action_log
        SET state = 'failed', finished_at = ?, error = ?
        WHERE action_uuid = ?
        """,
        (finished, error, action_uuid),
    )
    if cursor.rowcount != 1:
        raise RuntimeError(f"record_action_failure: no row for {action_uuid}")
    await conn.commit()


async def insert_payment(
    conn: aiosqlite.Connection,
    exchange: str,
    external_submission_id: str,
    action: str,
    submission_uuid: str,
    decided_by: str,
) -> None:
    """C2: terminal money decision. UNIQUE(exchange, external_submission_id)."""
    await conn.execute(
        """
        INSERT INTO payments (
            exchange, external_submission_id, action,
            submission_uuid, decided_at, decided_by
        )
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


async def payment_exists(
    conn: aiosqlite.Connection,
    exchange: str,
    external_submission_id: str,
) -> bool:
    cursor = await conn.execute(
        "SELECT 1 FROM payments WHERE exchange = ? AND external_submission_id = ?",
        (exchange, external_submission_id),
    )
    row = await cursor.fetchone()
    return row is not None


# --- Report rows ---------------------------------------------------------------


async def insert_report_row(
    conn: aiosqlite.Connection,
    *,
    week: str,
    source_platform: str,
    exchange: str,
    ordered_count: int,
    actual_count: int | None,
    cost: float | None,
    status: str,
) -> None:
    await conn.execute(
        """
        INSERT INTO report_rows (
            week, source_platform, exchange,
            ordered_count, actual_count, cost, status, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (week, source_platform, exchange, ordered_count, actual_count, cost, status, _now_iso()),
    )
    await conn.commit()


async def list_active_orders(conn: aiosqlite.Connection) -> list[Order]:
    cursor = await conn.execute("SELECT * FROM orders WHERE status IN ('active', 'verifying')")
    rows = await cursor.fetchall()
    result: list[Order] = []
    for row in rows:
        spec = OrderSpec.model_validate_json(row["spec_json"])
        result.append(
            Order(
                client_order_uuid=row["client_order_uuid"],
                spec=spec,
                status=OrderStatus(row["status"]),
                external_order_id=row["external_order_id"],
                cost_actual=row["cost_actual"],
                created_at=datetime.fromisoformat(row["created_at"]),
                updated_at=datetime.fromisoformat(row["updated_at"]),
            )
        )
    return result
