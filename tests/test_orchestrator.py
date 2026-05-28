"""Day 3 orchestrator tests: full lifecycle on fake adapters."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from adapters.fake import FakePanelAdapter, FakeTaskExchangeAdapter
from config import Settings
from db.database import connect, get_order, init_db, insert_order_creating
from models import (
    Order,
    OrderSpec,
    OrderStatus,
    Scenario,
    SourcePlatform,
    SubmissionStatus,
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


async def test_panel_full_lifecycle(settings: Settings) -> None:
    await init_db(settings)
    orch = make_orchestrator(settings)
    spec = OrderSpec(
        scenario=Scenario.ACTIVITY_SUBSCRIBE,
        exchange="fake_panel",
        target="https://t.me/x",
        quantity=10,
        max_cost=2.0,
    )
    uuid_, _, _ = await orch.create_order(spec, actor="test")

    # First poll: in_progress -> stays ACTIVE.
    s1 = await orch.poll_order(uuid_)
    assert s1 == OrderStatus.ACTIVE

    # Second poll: completed -> VERIFYING -> COMPLETED.
    s2 = await orch.poll_order(uuid_)
    assert s2 == OrderStatus.COMPLETED


async def test_task_exchange_full_lifecycle(settings: Settings) -> None:
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

    # Tick 1: submissions appear and are processed (auto-accept good / auto-reject weak).
    await orch.poll_order(uuid_)
    # Tick 2: exchange reports completed.
    s = await orch.poll_order(uuid_)
    assert s == OrderStatus.COMPLETED

    # Verify final submission statuses: 2 accepted, 1 rework_requested
    # (matches fake adapter's deterministic evidence_quality: good, good, weak).
    async with connect(settings) as conn:
        cur = await conn.execute(
            "SELECT status FROM submissions WHERE order_uuid = ? ORDER BY created_at",
            (uuid_,),
        )
        statuses = sorted(r["status"] for r in await cur.fetchall())
    assert statuses == sorted(
        [
            SubmissionStatus.ACCEPTED.value,
            SubmissionStatus.ACCEPTED.value,
            SubmissionStatus.REWORK_REQUESTED.value,
        ]
    )


async def test_live_mode_demo_verifier_does_not_auto_accept(settings: Settings) -> None:
    """Fake verifier hints are allowed in DRY_RUN demos, not in live money mode."""
    settings.dry_run = False
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
            "SELECT status FROM submissions WHERE order_uuid = ? ORDER BY created_at",
            (uuid_,),
        )
        statuses = sorted(r["status"] for r in await cur.fetchall())
    assert statuses == sorted(
        [
            SubmissionStatus.AWAITING_ADMIN.value,
            SubmissionStatus.AWAITING_ADMIN.value,
            SubmissionStatus.REWORK_REQUESTED.value,
        ]
    )


async def test_poll_unknown_order_raises(settings: Settings) -> None:
    await init_db(settings)
    orch = make_orchestrator(settings)
    with pytest.raises(ValueError, match="not found"):
        await orch.poll_order("nonexistent")


async def test_poll_terminal_order_is_noop(settings: Settings) -> None:
    """Polling an order that's already COMPLETED returns the same status without
    touching the adapter or writing further state transitions.
    """
    await init_db(settings)
    orch = make_orchestrator(settings)
    spec = OrderSpec(
        scenario=Scenario.ACTIVITY_SUBSCRIBE,
        exchange="fake_panel",
        target="https://t.me/x",
        quantity=10,
        max_cost=2.0,
    )
    uuid_, _, _ = await orch.create_order(spec, actor="test")
    await orch.poll_order(uuid_)
    final = await orch.poll_order(uuid_)
    assert final == OrderStatus.COMPLETED

    # Third poll on the terminal order: returns COMPLETED without raising.
    again = await orch.poll_order(uuid_)
    assert again == OrderStatus.COMPLETED


async def test_reconcile_creating_marks_failed(settings: Settings) -> None:
    """Orphan CREATING row (crash mid-placement) -> FAILED with audit entry."""
    await init_db(settings)
    spec = OrderSpec(
        scenario=Scenario.ACTIVITY_SUBSCRIBE,
        exchange="fake_panel",
        target="x",
        quantity=1,
        max_cost=1.0,
    )
    now = datetime.now(UTC)
    client_uuid = new_client_order_uuid()
    order = Order(
        client_order_uuid=client_uuid,
        spec=spec,
        status=OrderStatus.CREATING,
        created_at=now,
        updated_at=now,
    )
    async with connect(settings) as conn:
        await insert_order_creating(conn, order)

    orch = make_orchestrator(settings)
    # min_age_seconds=0: the test inserts a fresh row and reconciles
    # immediately; production default (300s) is exercised in test_idempotency.
    fixed = await orch.reconcile_creating(min_age_seconds=0)
    assert fixed == 1

    async with connect(settings) as conn:
        recovered = await get_order(conn, client_uuid)
        cur = await conn.execute(
            "SELECT event FROM audit_log WHERE order_uuid = ? AND event = ?",
            (client_uuid, "reconcile_creating_to_failed"),
        )
        rows = await cur.fetchall()
    assert recovered is not None
    assert recovered.status == OrderStatus.FAILED
    assert len(rows) == 1


async def test_reconcile_with_no_creating_rows_is_noop(settings: Settings) -> None:
    await init_db(settings)
    orch = make_orchestrator(settings)
    fixed = await orch.reconcile_creating()
    assert fixed == 0


async def test_task_order_stays_verifying_when_submission_awaiting_admin(
    settings: Settings,
) -> None:
    """HIGH-5 fix: a task-exchange order whose exchange-side status is `completed`
    but with a local submission in AWAITING_ADMIN must NOT auto-close. The
    orchestrator keeps it in VERIFYING until the admin resolves the submission.
    """
    import uuid as _uuid

    from db.database import insert_submission
    from models import Submission, SubmissionStatus

    await init_db(settings)
    # `submissions_per_order=0` makes the fake task adapter report `completed`
    # on its first poll without ever yielding submissions of its own.
    orch = Orchestrator(
        settings,
        {
            "fake_panel": FakePanelAdapter(),
            "fake_task_exchange": FakeTaskExchangeAdapter(
                polls_to_yield=1, submissions_per_order=0
            ),
        },
    )
    spec = OrderSpec(
        scenario=Scenario.SOCIAL_TRAFFIC,
        exchange="fake_task_exchange",
        target="https://example.com",
        quantity=1,
        source_platform=SourcePlatform.VK,
        max_cost=1.0,
    )
    uuid_, _, _ = await orch.create_order(spec, actor="test")

    # Inject a submission in AWAITING_ADMIN directly (simulating a previous
    # poll that produced an unknown-evidence verdict).
    pending = Submission(
        submission_uuid=str(_uuid.uuid4()),
        order_uuid=uuid_,
        external_submission_id="manual-ext-1",
        executor_hint=None,
        status=SubmissionStatus.AWAITING_ADMIN,
        evidence="unknown",
        created_at=datetime.now(UTC),
    )
    async with connect(settings) as conn:
        await insert_submission(conn, pending)

    # Poll: adapter says `completed`, but our DB has an AWAITING_ADMIN submission.
    # Order must stay in VERIFYING, not COMPLETED.
    result = await orch.poll_order(uuid_, actor="test")
    assert result == OrderStatus.VERIFYING

    async with connect(settings) as conn:
        order = await get_order(conn, uuid_)
    assert order is not None
    assert order.status == OrderStatus.VERIFYING
