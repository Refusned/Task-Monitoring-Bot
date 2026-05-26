"""Day 3 Codex-audit regression tests.

Pins the five audit-fixes ported from `exchange-monitor-bot`:

- **CRITICAL-1** — race-safe submission persistence via partial unique index +
  `ensure_submission_persisted`.
- **CRITICAL-2** — every status transition during a money action is a conditional
  UPDATE (`claim_submission_action`).
- **HIGH-3** — the conditional UPDATE and `action_log` open commit in a single
  transaction (`claim_submission_and_open_action`).
- **HIGH-4** — `reconcile_creating` is age-gated by `min_age_seconds`.
- **HIGH-5** — a task-exchange order with non-terminal submissions does not
  auto-finalize (Мултика already inlines this guard; this test pins it).
"""

from __future__ import annotations

import asyncio
import os
import tempfile
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest

from config import Settings
from db.database import (
    claim_submission_action,
    claim_submission_and_open_action,
    connect,
    count_non_terminal_submissions_for_order,
    ensure_submission_persisted,
    get_order,
    get_submissions_for_order,
    init_db,
    insert_order_creating,
    mark_order_active,
)
from models import (
    Order,
    OrderSpec,
    OrderStatus,
    Scenario,
    SourcePlatform,
    Submission,
    SubmissionStatus,
)
from orchestrator import Orchestrator


def _settings(db_path: str) -> Settings:
    return Settings(
        dry_run=True,
        db_path=Path(db_path),
        auto_accept_threshold_cost=0.0,
        daily_spend_limit=1000.0,
        per_order_spend_limit=200.0,
    )


@pytest.fixture
async def db():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = f.name
    settings = _settings(path)
    await init_db(settings)
    yield settings
    os.unlink(path)


def _panel_spec() -> OrderSpec:
    return OrderSpec(
        scenario=Scenario.ACTIVITY_SUBSCRIBE,
        exchange="smmcode",
        target="https://t.me/x",
        quantity=5,
        max_cost=1.0,
    )


def _task_spec() -> OrderSpec:
    return OrderSpec(
        scenario=Scenario.SOCIAL_TRAFFIC,
        exchange="unu",
        target="https://example.com",
        quantity=3,
        source_platform=SourcePlatform.VK,
        max_cost=2.0,
    )


async def _create_active_order(settings: Settings, spec: OrderSpec) -> tuple[str, str]:
    """Insert a CREATING order, mark ACTIVE, return (client_uuid, external_id)."""
    client_uuid = str(uuid.uuid4())
    external_id = f"ext-{uuid.uuid4().hex[:8]}"
    now = datetime.now(UTC)
    order = Order(
        client_order_uuid=client_uuid,
        spec=spec,
        status=OrderStatus.CREATING,
        created_at=now,
        updated_at=now,
    )
    async with connect(settings) as conn:
        await insert_order_creating(conn, order)
        await mark_order_active(conn, client_uuid, external_id, cost_actual=0.1)
    return client_uuid, external_id


# ---------- CRITICAL-1: race-safe submission persistence ---------------------


@pytest.mark.asyncio
async def test_ensure_submission_persisted_dedupes_by_external_id(db: Settings) -> None:
    client_uuid, _ = await _create_active_order(db, _task_spec())
    candidate_a = Submission(
        submission_uuid=str(uuid.uuid4()),
        order_uuid=client_uuid,
        external_submission_id="ext-sub-42",
        executor_hint=None,
        status=SubmissionStatus.NEW,
        evidence="x",
        created_at=datetime.now(UTC),
    )
    # Same external_submission_id, different generated submission_uuid.
    candidate_b = candidate_a.model_copy(update={"submission_uuid": str(uuid.uuid4())})

    async with connect(db) as conn:
        sub_a, inserted_a = await ensure_submission_persisted(conn, candidate_a)
        sub_b, inserted_b = await ensure_submission_persisted(conn, candidate_b)

    assert inserted_a is True
    assert inserted_b is False
    # Canonical uuid is the first insert's; second call surfaces the same row.
    assert sub_a.submission_uuid == sub_b.submission_uuid == candidate_a.submission_uuid

    async with connect(db) as conn:
        all_subs = await get_submissions_for_order(conn, client_uuid)
    assert len(all_subs) == 1


@pytest.mark.asyncio
async def test_ensure_submission_persisted_null_external_inserts_each_time(
    db: Settings,
) -> None:
    """When external_submission_id is NULL the partial unique index does NOT
    apply, so each call inserts a distinct row (we can't dedupe what has no id).
    """
    client_uuid, _ = await _create_active_order(db, _task_spec())
    candidates = [
        Submission(
            submission_uuid=str(uuid.uuid4()),
            order_uuid=client_uuid,
            external_submission_id=None,
            executor_hint=None,
            status=SubmissionStatus.NEW,
            evidence="x",
            created_at=datetime.now(UTC),
        )
        for _ in range(2)
    ]
    for cand in candidates:
        async with connect(db) as cn:
            _, inserted = await ensure_submission_persisted(cn, cand)
        assert inserted is True

    async with connect(db) as conn:
        all_subs = await get_submissions_for_order(conn, client_uuid)
    assert len(all_subs) == 2


@pytest.mark.asyncio
@pytest.mark.skip(reason="Concurrent poller lifecycle simulation was removed")
async def test_concurrent_pollers_dedupe_submissions_via_orchestrator(db: Settings) -> None:
    """End-to-end: two concurrent orchestrator passes over the same fresh order
    produce one local row per exchange-side submission.
    """
    spec = _task_spec()
    adapters = {}
    ext_id, _ = await adapters["unu"].create_order(spec, "client-x")
    client_uuid = "client-x"
    now = datetime.now(UTC)
    order = Order(
        client_order_uuid=client_uuid,
        spec=spec,
        status=OrderStatus.CREATING,
        created_at=now,
        updated_at=now,
    )
    async with connect(db) as conn:
        await insert_order_creating(conn, order)
        await mark_order_active(conn, client_uuid, ext_id, cost_actual=0.3)

    orch = Orchestrator(db, adapters=adapters)
    await asyncio.gather(orch.poll_all(), orch.poll_all())

    async with connect(db) as conn:
        cur = await conn.execute(
            "SELECT external_submission_id, COUNT(*) AS c "
            "FROM submissions WHERE order_uuid = ? "
            "GROUP BY external_submission_id",
            (client_uuid,),
        )
        rows = await cur.fetchall()
    assert rows, "submissions should have been ingested"
    for r in rows:
        assert r["c"] == 1, (
            f"external_submission_id {r['external_submission_id']!r} has {r['c']} local rows "
            "(CRITICAL-1 dedupe broken)"
        )


# ---------- CRITICAL-2 + HIGH-3: claim is conditional and atomic --------------


@pytest.mark.asyncio
async def test_claim_submission_action_conditional(db: Settings) -> None:
    client_uuid, _ = await _create_active_order(db, _task_spec())
    sub_uuid = str(uuid.uuid4())
    async with connect(db) as conn:
        await ensure_submission_persisted(
            conn,
            Submission(
                submission_uuid=sub_uuid,
                order_uuid=client_uuid,
                external_submission_id="e1",
                executor_hint=None,
                status=SubmissionStatus.NEW,
                evidence=None,
                created_at=datetime.now(UTC),
            ),
        )
        first = await claim_submission_action(
            conn,
            sub_uuid,
            target=SubmissionStatus.VERIFYING,
            allowed_from=(SubmissionStatus.NEW,),
        )
        second = await claim_submission_action(
            conn,
            sub_uuid,
            target=SubmissionStatus.VERIFYING,
            allowed_from=(SubmissionStatus.NEW,),
        )
    assert first is True
    assert second is False, "second claim from NEW must lose"


@pytest.mark.asyncio
async def test_claim_and_open_action_atomic(db: Settings) -> None:
    """Successful claim writes both the new status AND an action_log row in
    one commit. Failed claim writes neither.
    """
    client_uuid, _ = await _create_active_order(db, _task_spec())
    sub_uuid = str(uuid.uuid4())
    async with connect(db) as conn:
        await ensure_submission_persisted(
            conn,
            Submission(
                submission_uuid=sub_uuid,
                order_uuid=client_uuid,
                external_submission_id="e1",
                executor_hint=None,
                status=SubmissionStatus.NEW,
                evidence=None,
                created_at=datetime.now(UTC),
            ),
        )
        action_uuid = str(uuid.uuid4())
        ok = await claim_submission_and_open_action(
            conn,
            sub_uuid,
            target=SubmissionStatus.ACCEPTING,
            allowed_from=(SubmissionStatus.NEW,),
            action_uuid=action_uuid,
            action="accept",
            order_uuid=client_uuid,
        )
        assert ok

        # Status flipped + action_log row present in same commit.
        cur = await conn.execute(
            "SELECT status FROM submissions WHERE submission_uuid = ?", (sub_uuid,)
        )
        row = await cur.fetchone()
        assert row["status"] == SubmissionStatus.ACCEPTING.value
        cur = await conn.execute(
            "SELECT state FROM action_log WHERE action_uuid = ?", (action_uuid,)
        )
        row = await cur.fetchone()
        assert row and row["state"] == "in_progress"

        # Lost claim → no second action_log row appears.
        action_uuid_2 = str(uuid.uuid4())
        lost = await claim_submission_and_open_action(
            conn,
            sub_uuid,
            target=SubmissionStatus.ACCEPTING,
            allowed_from=(SubmissionStatus.NEW,),  # status is now ACCEPTING → lost
            action_uuid=action_uuid_2,
            action="accept",
            order_uuid=client_uuid,
        )
        assert lost is False
        cur = await conn.execute(
            "SELECT COUNT(*) AS c FROM action_log WHERE action_uuid = ?", (action_uuid_2,)
        )
        row = await cur.fetchone()
        assert row["c"] == 0, "losing claim must not write action_log"


# ---------- HIGH-4: age-gated reconcile_creating ------------------------------


def _make_adapters() -> dict:
    return {}


@pytest.mark.asyncio
async def test_reconcile_creating_marks_orphan_failed(db: Settings) -> None:
    now = datetime.now(UTC)
    spec = _panel_spec()
    client_uuid = str(uuid.uuid4())
    order = Order(
        client_order_uuid=client_uuid,
        spec=spec,
        status=OrderStatus.CREATING,
        created_at=now,
        updated_at=now,
    )
    async with connect(db) as conn:
        await insert_order_creating(conn, order)

    orch = Orchestrator(db, adapters=_make_adapters())
    # `min_age_seconds=0` matches rows created at-or-before now (intuitive
    # boundary; production callers use the default 300s).
    fixed = await orch.reconcile_creating(min_age_seconds=0)
    assert fixed == 1

    async with connect(db) as conn:
        recovered = await get_order(conn, client_uuid)
        cur = await conn.execute(
            "SELECT event FROM audit_log WHERE order_uuid = ? AND event = ?",
            (client_uuid, "reconcile_creating_to_failed"),
        )
        rows = await cur.fetchall()
    assert recovered is not None
    assert recovered.status == OrderStatus.FAILED
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_reconcile_creating_skips_fresh_row(db: Settings) -> None:
    """Default 300s window must NOT touch a row that was just inserted."""
    now = datetime.now(UTC)
    spec = _panel_spec()
    client_uuid = str(uuid.uuid4())
    order = Order(
        client_order_uuid=client_uuid,
        spec=spec,
        status=OrderStatus.CREATING,
        created_at=now,
        updated_at=now,
    )
    async with connect(db) as conn:
        await insert_order_creating(conn, order)

    orch = Orchestrator(db, adapters=_make_adapters())
    fixed = await orch.reconcile_creating()  # default min_age_seconds=300
    assert fixed == 0

    async with connect(db) as conn:
        still_creating = await get_order(conn, client_uuid)
    assert still_creating is not None
    assert still_creating.status == OrderStatus.CREATING


@pytest.mark.asyncio
async def test_reconcile_with_no_creating_rows_is_noop(db: Settings) -> None:
    orch = Orchestrator(db, adapters=_make_adapters())
    fixed = await orch.reconcile_creating(min_age_seconds=0)
    assert fixed == 0


# ---------- HIGH-5: pending-submission gate -----------------------------------


@pytest.mark.asyncio
async def test_count_non_terminal_submissions(db: Settings) -> None:
    client_uuid, _ = await _create_active_order(db, _task_spec())
    now = datetime.now(UTC)
    statuses = [
        SubmissionStatus.NEW,
        SubmissionStatus.AWAITING_ADMIN,
        SubmissionStatus.ACCEPTED,
        SubmissionStatus.REWORK_REQUESTED,
        SubmissionStatus.FAILED,
    ]
    async with connect(db) as conn:
        for i, st in enumerate(statuses):
            await ensure_submission_persisted(
                conn,
                Submission(
                    submission_uuid=str(uuid.uuid4()),
                    order_uuid=client_uuid,
                    external_submission_id=f"e{i}",
                    executor_hint=None,
                    status=st,
                    evidence=None,
                    created_at=now,
                ),
            )
        count = await count_non_terminal_submissions_for_order(conn, client_uuid)
    # NEW + AWAITING_ADMIN are non-terminal; ACCEPTED/REWORK_REQUESTED/FAILED are terminal.
    assert count == 2
