"""C1 + C2 idempotency tests (Day 3, including Day 3 audit fixes).

C1: no duplicate order placement (CREATING row before the external call; orphan
rows reconciled at startup, age-gated to avoid racing live creates).
C2: no double accept/reject (UNIQUE constraint + INSERT OR IGNORE on submission
persistence; conditional UPDATE for every transition; claim + action_log open
in one transaction; payments PRIMARY KEY).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest

from adapters.fake import FakePanelAdapter, FakeTaskExchangeAdapter
from config import Settings
from db.database import (
    claim_order_status,
    connect,
    get_order,
    init_db,
    insert_order_creating,
)
from models import (
    Order,
    OrderSpec,
    OrderStatus,
    Scenario,
    SourcePlatform,
    new_client_order_uuid,
)
from orchestrator import Orchestrator


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(db_path=tmp_path / "test.db")


def make_orchestrator(settings: Settings) -> Orchestrator:
    return Orchestrator(
        settings,
        {
            "fake_panel": FakePanelAdapter(),
            "fake_task_exchange": FakeTaskExchangeAdapter(),
        },
    )


# ---------- C1 ----------------------------------------------------------------


async def test_c1_creating_audit_precedes_active(settings: Settings) -> None:
    """The audit trail must record `order_creating` before `order_active` for the
    same client_order_uuid (proves the CREATING row was written before the
    external adapter call).
    """
    await init_db(settings)
    orch = make_orchestrator(settings)
    spec = OrderSpec(
        scenario=Scenario.ACTIVITY_SUBSCRIBE,
        exchange="fake_panel",
        target="https://t.me/x",
        quantity=5,
        max_cost=1.0,
    )
    uuid_, _, _ = await orch.create_order(spec, actor="test")
    async with connect(settings) as conn:
        cur = await conn.execute(
            "SELECT event FROM audit_log WHERE order_uuid = ? ORDER BY audit_id",
            (uuid_,),
        )
        events = [r["event"] for r in await cur.fetchall()]
    assert "order_creating" in events
    assert "order_active" in events
    assert events.index("order_creating") < events.index("order_active")


async def test_c1_reconcile_orphan_creating(settings: Settings) -> None:
    """A CREATING row found at startup -> FAILED, with reconcile audit entry."""
    await init_db(settings)
    now = datetime.now(UTC)
    spec = OrderSpec(
        scenario=Scenario.ACTIVITY_SUBSCRIBE,
        exchange="fake_panel",
        target="x",
        quantity=1,
        max_cost=1.0,
    )
    order = Order(
        client_order_uuid=new_client_order_uuid(),
        spec=spec,
        status=OrderStatus.CREATING,
        created_at=now,
        updated_at=now,
    )
    async with connect(settings) as conn:
        await insert_order_creating(conn, order)

    orch = make_orchestrator(settings)
    # min_age_seconds=0 because the test inserts a fresh row and reconciles
    # immediately; the production default of 300s is exercised by
    # `test_c1_reconcile_skips_fresh_creating`.
    fixed = await orch.reconcile_creating(min_age_seconds=0)
    assert fixed == 1

    async with connect(settings) as conn:
        recovered = await get_order(conn, order.client_order_uuid)
        cur = await conn.execute(
            "SELECT event FROM audit_log WHERE order_uuid = ? AND event LIKE 'reconcile%'",
            (order.client_order_uuid,),
        )
        events = [r["event"] for r in await cur.fetchall()]
    assert recovered is not None
    assert recovered.status == OrderStatus.FAILED
    assert events == ["reconcile_creating_to_failed"]


async def test_c1_daily_limit_counts_concurrent_reservations(settings: Settings) -> None:
    """Two placements that each fit the daily limit must not both reserve it."""
    settings.daily_spend_limit = 0.07
    await init_db(settings)
    orch = make_orchestrator(settings)

    async def create_one() -> object:
        spec = OrderSpec(
            scenario=Scenario.ACTIVITY_SUBSCRIBE,
            exchange="fake_panel",
            target="https://t.me/x",
            quantity=1,
            max_cost=0.05,
        )
        return await orch.create_order(spec, actor="test")

    results = await asyncio.gather(create_one(), create_one(), return_exceptions=True)
    successes = [r for r in results if not isinstance(r, Exception)]
    failures = [r for r in results if isinstance(r, Exception)]
    assert len(successes) == 1
    assert len(failures) == 1
    assert "daily spend limit" in str(failures[0])

    async with connect(settings) as conn:
        cur = await conn.execute("SELECT COUNT(*) AS c FROM orders")
        count = (await cur.fetchone())["c"]
    assert count == 1


# ---------- C2 ----------------------------------------------------------------


async def test_c2_double_accept_is_noop(settings: Settings) -> None:
    """Calling accept_submission twice on the same submission only triggers one
    external call and one payment row. The second call returns False.
    """
    await init_db(settings)
    orch = make_orchestrator(settings)
    spec = OrderSpec(
        scenario=Scenario.SOCIAL_TRAFFIC,
        exchange="fake_task_exchange",
        target="https://example.com",
        quantity=3,
        source_platform=SourcePlatform.VK,
        max_cost=2.0,
    )
    uuid_, _, _ = await orch.create_order(spec, actor="test")
    await orch.poll_order(uuid_, actor="test")  # auto-accept / auto-reject runs here

    # Find an auto-accepted submission.
    async with connect(settings) as conn:
        cur = await conn.execute(
            "SELECT submission_uuid FROM submissions WHERE order_uuid = ? AND status = 'accepted'",
            (uuid_,),
        )
        rows = await cur.fetchall()
    assert rows, "expected at least one auto-accepted submission"
    sub_uuid = rows[0]["submission_uuid"]

    # Re-tap accept: must be a no-op (already terminal).
    result = await orch.accept_submission(sub_uuid, decided_by="duplicate-tap")
    assert result is False

    # Payments table has exactly one accept for that submission.
    async with connect(settings) as conn:
        cur = await conn.execute(
            "SELECT COUNT(*) AS c FROM payments WHERE submission_uuid = ?",
            (sub_uuid,),
        )
        row = await cur.fetchone()
    assert row is not None and row["c"] == 1


async def test_c2_payments_unique_key_prevents_duplicate(settings: Settings) -> None:
    """The (exchange, external_submission_id) PRIMARY KEY on `payments` keeps
    the terminal record unique even if a duplicate INSERT OR IGNORE somehow runs.
    """
    await init_db(settings)
    orch = make_orchestrator(settings)
    spec = OrderSpec(
        scenario=Scenario.SOCIAL_TRAFFIC,
        exchange="fake_task_exchange",
        target="https://example.com",
        quantity=3,
        source_platform=SourcePlatform.VK,
        max_cost=2.0,
    )
    uuid_, _, _ = await orch.create_order(spec, actor="test")
    await orch.poll_order(uuid_, actor="test")

    async with connect(settings) as conn:
        cur = await conn.execute("SELECT exchange, external_submission_id FROM payments LIMIT 1")
        row = await cur.fetchone()
        assert row is not None
        # Direct duplicate INSERT OR IGNORE: must NOT create a second row.
        await conn.execute(
            "INSERT OR IGNORE INTO payments "
            "(exchange, external_submission_id, action, submission_uuid, decided_at, decided_by) "
            "VALUES (?, ?, 'accept', 'duplicate-uuid', '2026-01-01T00:00:00', 'test')",
            (row["exchange"], row["external_submission_id"]),
        )
        await conn.commit()
        cur2 = await conn.execute(
            "SELECT COUNT(*) AS c FROM payments WHERE exchange = ? AND external_submission_id = ?",
            (row["exchange"], row["external_submission_id"]),
        )
        count = (await cur2.fetchone())["c"]
    assert count == 1


async def test_c2_concurrent_pollers_no_duplicate_submissions(settings: Settings) -> None:
    """CRITICAL-1 fix: two concurrent pollers produce one local submission per
    external_submission_id - the UNIQUE constraint + INSERT OR IGNORE handle the
    race.
    """
    await init_db(settings)
    orch = make_orchestrator(settings)
    spec = OrderSpec(
        scenario=Scenario.SOCIAL_TRAFFIC,
        exchange="fake_task_exchange",
        target="https://example.com",
        quantity=3,
        source_platform=SourcePlatform.VK,
        max_cost=2.0,
    )
    uuid_, _, _ = await orch.create_order(spec, actor="test")

    # Two pollers concurrently process the same fresh order.
    await asyncio.gather(
        orch.poll_order(uuid_, actor="test"),
        orch.poll_order(uuid_, actor="test"),
    )

    async with connect(settings) as conn:
        cur = await conn.execute(
            "SELECT external_submission_id, COUNT(*) AS c FROM submissions "
            "WHERE order_uuid = ? GROUP BY external_submission_id",
            (uuid_,),
        )
        rows = await cur.fetchall()
    assert rows, "expected submissions to be persisted"
    assert all(r["c"] == 1 for r in rows), (
        "each external_submission_id must have exactly one local row"
    )
    # Final statuses match the canonical happy-path outcome (2 accepted, 1 rework).
    async with connect(settings) as conn:
        cur = await conn.execute("SELECT status FROM submissions WHERE order_uuid = ?", (uuid_,))
        statuses = sorted(r["status"] for r in await cur.fetchall())
    assert statuses == ["accepted", "accepted", "rework_requested"]


async def test_claim_order_status_refuses_terminal_regression(settings: Settings) -> None:
    await init_db(settings)
    orch = make_orchestrator(settings)
    spec = OrderSpec(
        scenario=Scenario.ACTIVITY_SUBSCRIBE,
        exchange="fake_panel",
        target="https://t.me/x",
        quantity=1,
        max_cost=1.0,
    )
    uuid_, _, _ = await orch.create_order(spec, actor="test")
    async with connect(settings) as conn:
        claimed = await claim_order_status(
            conn,
            uuid_,
            target=OrderStatus.COMPLETED,
            allowed_from=(OrderStatus.ACTIVE,),
        )
    assert claimed is True

    async with connect(settings) as conn:
        regressed = await claim_order_status(
            conn,
            uuid_,
            target=OrderStatus.VERIFYING,
            allowed_from=(OrderStatus.ACTIVE,),
        )
        order = await get_order(conn, uuid_)
    assert regressed is False
    assert order is not None
    assert order.status == OrderStatus.COMPLETED


async def test_c2_concurrent_pollers_single_payment_per_submission(
    settings: Settings,
) -> None:
    """CRITICAL-1 corollary: even with concurrent pollers, `payments` has
    exactly one row per submission (one accept or reject - never both, never
    duplicate).
    """
    await init_db(settings)
    orch = make_orchestrator(settings)
    spec = OrderSpec(
        scenario=Scenario.SOCIAL_TRAFFIC,
        exchange="fake_task_exchange",
        target="https://example.com",
        quantity=3,
        source_platform=SourcePlatform.VK,
        max_cost=2.0,
    )
    uuid_, _, _ = await orch.create_order(spec, actor="test")
    await asyncio.gather(
        orch.poll_order(uuid_, actor="test"),
        orch.poll_order(uuid_, actor="test"),
    )

    async with connect(settings) as conn:
        cur = await conn.execute(
            "SELECT submission_uuid, COUNT(*) AS c FROM payments GROUP BY submission_uuid"
        )
        rows = await cur.fetchall()
    assert rows
    assert all(r["c"] == 1 for r in rows)


async def test_c2_stale_poll_does_not_reset_terminal_status(
    settings: Settings,
) -> None:
    """CRITICAL-2 fix: a second poll after submissions are decided must not
    overwrite their terminal status back to VERIFYING.
    """
    await init_db(settings)
    orch = make_orchestrator(settings)
    spec = OrderSpec(
        scenario=Scenario.SOCIAL_TRAFFIC,
        exchange="fake_task_exchange",
        target="https://example.com",
        quantity=3,
        source_platform=SourcePlatform.VK,
        max_cost=2.0,
    )
    uuid_, _, _ = await orch.create_order(spec, actor="test")
    await orch.poll_order(uuid_, actor="test")

    async with connect(settings) as conn:
        cur = await conn.execute(
            "SELECT submission_uuid, status FROM submissions WHERE order_uuid = ?",
            (uuid_,),
        )
        before = {r["submission_uuid"]: r["status"] for r in await cur.fetchall()}

    # Second poll (the order is still ACTIVE because we polled once - the fake
    # only reports `completed` on the second adapter poll).
    await orch.poll_order(uuid_, actor="test")

    async with connect(settings) as conn:
        cur = await conn.execute(
            "SELECT submission_uuid, status FROM submissions WHERE order_uuid = ?",
            (uuid_,),
        )
        after = {r["submission_uuid"]: r["status"] for r in await cur.fetchall()}
    assert before == after, "terminal submission statuses must not change on replay"


async def test_c2_claim_and_action_log_are_atomic(settings: Settings) -> None:
    """HIGH-3 fix: every successfully-decided submission has a matching
    action_log row in the SUCCEEDED state. They are committed together.
    """
    await init_db(settings)
    orch = make_orchestrator(settings)
    spec = OrderSpec(
        scenario=Scenario.SOCIAL_TRAFFIC,
        exchange="fake_task_exchange",
        target="https://example.com",
        quantity=3,
        source_platform=SourcePlatform.VK,
        max_cost=2.0,
    )
    uuid_, _, _ = await orch.create_order(spec, actor="test")
    await orch.poll_order(uuid_, actor="test")

    async with connect(settings) as conn:
        cur = await conn.execute(
            """
            SELECT s.submission_uuid, s.status, al.state, al.action
            FROM submissions s
            LEFT JOIN action_log al ON s.submission_uuid = al.submission_uuid
            WHERE s.order_uuid = ?
            """,
            (uuid_,),
        )
        rows = await cur.fetchall()
    assert len(rows) == 3
    for r in rows:
        assert r["state"] == "succeeded", (
            f"submission {r['submission_uuid']} has status={r['status']} but no "
            f"matching action_log row (claim/action_log atomicity broken)"
        )


async def test_c1_reconcile_skips_fresh_creating(settings: Settings) -> None:
    """HIGH-4 fix: a CREATING row younger than min_age_seconds is NOT touched.
    Only after the threshold (or with min_age_seconds=0) does it get FAILED.
    """
    await init_db(settings)
    now = datetime.now(UTC)
    spec = OrderSpec(
        scenario=Scenario.ACTIVITY_SUBSCRIBE,
        exchange="fake_panel",
        target="x",
        quantity=1,
        max_cost=1.0,
    )
    order = Order(
        client_order_uuid=new_client_order_uuid(),
        spec=spec,
        status=OrderStatus.CREATING,
        created_at=now,
        updated_at=now,
    )
    async with connect(settings) as conn:
        await insert_order_creating(conn, order)

    orch = make_orchestrator(settings)
    # Default min_age=300 - the fresh row must NOT be reconciled.
    fixed_default = await orch.reconcile_creating()
    assert fixed_default == 0

    async with connect(settings) as conn:
        still_creating = await get_order(conn, order.client_order_uuid)
    assert still_creating is not None
    assert still_creating.status == OrderStatus.CREATING

    # With min_age_seconds=0, the same row IS reconciled.
    fixed_aggressive = await orch.reconcile_creating(min_age_seconds=0)
    assert fixed_aggressive == 1

    async with connect(settings) as conn:
        recovered = await get_order(conn, order.client_order_uuid)
    assert recovered is not None
    assert recovered.status == OrderStatus.FAILED


async def test_c2_action_log_records_succeeded_actions(settings: Settings) -> None:
    """Every accept/reject runs through action_log: in_progress -> succeeded."""
    await init_db(settings)
    orch = make_orchestrator(settings)
    spec = OrderSpec(
        scenario=Scenario.SOCIAL_TRAFFIC,
        exchange="fake_task_exchange",
        target="https://example.com",
        quantity=3,
        source_platform=SourcePlatform.VK,
        max_cost=2.0,
    )
    uuid_, _, _ = await orch.create_order(spec, actor="test")
    await orch.poll_order(uuid_, actor="test")

    async with connect(settings) as conn:
        cur = await conn.execute(
            "SELECT action, state, finished_at FROM action_log ORDER BY started_at"
        )
        rows = await cur.fetchall()

    # 3 submissions -> 3 actions (2 accepts + 1 reject), all succeeded with finished_at set.
    assert len(rows) == 3
    actions = sorted(r["action"] for r in rows)
    assert actions == ["accept", "accept", "reject"]
    assert all(r["state"] == "succeeded" for r in rows)
    assert all(r["finished_at"] is not None for r in rows)


async def test_c2_reject_writes_payment_with_reject_action(settings: Settings) -> None:
    """The rejected submission produces a payments row with action='reject'."""
    await init_db(settings)
    orch = make_orchestrator(settings)
    spec = OrderSpec(
        scenario=Scenario.SOCIAL_TRAFFIC,
        exchange="fake_task_exchange",
        target="https://example.com",
        quantity=3,
        source_platform=SourcePlatform.VK,
        max_cost=2.0,
    )
    uuid_, _, _ = await orch.create_order(spec, actor="test")
    await orch.poll_order(uuid_, actor="test")

    async with connect(settings) as conn:
        cur = await conn.execute("SELECT action FROM payments ORDER BY decided_at")
        actions = [r["action"] for r in await cur.fetchall()]
    assert sorted(actions) == sorted(["accept", "accept", "reject"])
