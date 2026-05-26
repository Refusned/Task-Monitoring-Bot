"""Tests for orchestrator: polling, verification, accept/reject, C2 money-safety."""

from __future__ import annotations

import hashlib
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path

import pytest

from config import Settings
from db.database import (
    connect,
    get_order,
    get_submission,
    get_submissions_for_order,
    init_db,
    insert_order_creating,
    insert_submission,
    list_active_orders,
    mark_order_active,
    payment_exists,
)
from models import Order, OrderSpec, OrderStatus, Scenario, SourcePlatform, Submission
from orchestrator import Orchestrator


def _settings(db_path: str) -> Settings:
    return Settings(
        dry_run=True,
        db_path=Path(db_path),
        auto_accept_threshold_cost=0.0,
        daily_spend_limit=1000.0,
        per_order_spend_limit=200.0,
    )


def _make_order_spec(scenario: Scenario) -> OrderSpec:
    if scenario == Scenario.SOCIAL_TRAFFIC:
        return OrderSpec(
            scenario=scenario,
            exchange="unu",
            target="https://example.com",
            quantity=5,
            source_platform=SourcePlatform.VK,
            max_cost=2.0,
        )
    return OrderSpec(
        scenario=scenario,
        exchange="smmcode",
        target="https://t.me/test",
        quantity=10,
        max_cost=2.0,
    )


def _make_adapters(dry_run: bool = True) -> dict:
    return {}


@pytest.fixture
async def db():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = f.name
    settings = _settings(path)
    await init_db(settings)
    yield settings
    os.unlink(path)


@pytest.mark.asyncio
async def test_poll_empty(db: Settings) -> None:
    orch = Orchestrator(db, adapters=_make_adapters())
    results = await orch.poll_all()
    assert results == []
    async with connect(db) as conn:
        active = await list_active_orders(conn)
    assert active == []


@pytest.mark.asyncio
@pytest.mark.skip(reason="Simulated adapter lifecycle was removed")
async def test_panel_lifecycle(db: Settings) -> None:
    adapters = _make_adapters()
    spec = _make_order_spec(Scenario.ACTIVITY_SUBSCRIBE)
    client_uuid = "panel-uuid-1"
    ext_id, cost = await adapters["smmcode"].create_order(spec, client_uuid)

    order = Order(
        client_order_uuid=client_uuid,
        spec=spec,
        status=OrderStatus.ACTIVE,
        external_order_id=ext_id,
        cost_actual=cost,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    async with connect(db) as conn:
        await insert_order_creating(conn, order)
        await mark_order_active(conn, client_uuid, ext_id, cost)

    orch = Orchestrator(db, adapters=adapters)
    results = await orch.poll_all()
    assert len(results) == 1

    async with connect(db) as conn:
        stored = await get_order(conn, client_uuid)
    assert stored is not None
    assert stored.status in (OrderStatus.COMPLETED, OrderStatus.FAILED, OrderStatus.VERIFYING)


@pytest.mark.asyncio
@pytest.mark.skip(reason="Simulated adapter lifecycle was removed")
async def test_task_exchange_submissions(db: Settings) -> None:
    adapters = _make_adapters()
    spec = _make_order_spec(Scenario.SOCIAL_TRAFFIC)
    client_uuid = "task-uuid-1"
    ext_id, cost = await adapters["unu"].create_order(spec, client_uuid)

    order = Order(
        client_order_uuid=client_uuid,
        spec=spec,
        status=OrderStatus.ACTIVE,
        external_order_id=ext_id,
        cost_actual=cost,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    async with connect(db) as conn:
        await insert_order_creating(conn, order)
        await mark_order_active(conn, client_uuid, ext_id, cost)

    orch = Orchestrator(db, adapters=adapters)
    results = await orch.poll_all()
    assert len(results) == 1

    async with connect(db) as conn:
        subs = await get_submissions_for_order(conn, client_uuid)
    assert len(subs) == 3


@pytest.mark.asyncio
@pytest.mark.skip(reason="Simulated adapter lifecycle was removed")
async def test_task_exchange_accept_reject(db: Settings) -> None:
    adapters = _make_adapters()
    spec = _make_order_spec(Scenario.SOCIAL_TRAFFIC)
    client_uuid = "task-uuid-2"
    ext_id, cost = await adapters["unu"].create_order(spec, client_uuid)

    order = Order(
        client_order_uuid=client_uuid,
        spec=spec,
        status=OrderStatus.ACTIVE,
        external_order_id=ext_id,
        cost_actual=cost,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    async with connect(db) as conn:
        await insert_order_creating(conn, order)
        await mark_order_active(conn, client_uuid, ext_id, cost)

    orch = Orchestrator(db, adapters=adapters)

    # Round 1: poll creates submissions, verifies them
    await orch.poll_all()
    # Round 2: exchange now sees all resolved -> completed
    await orch.poll_all()

    async with connect(db) as conn:
        subs = await get_submissions_for_order(conn, client_uuid)
        stored = await get_order(conn, client_uuid)

    assert stored is not None
    for s in subs:
        assert s.status in ("accepted", "rework_requested", "failed", "awaiting_admin")

    accepted_subs = [s for s in subs if s.status == "accepted"]
    for s in accepted_subs:
        async with connect(db) as conn:
            assert await payment_exists(conn, spec.exchange, s.external_submission_id or "")

    if stored.status == OrderStatus.COMPLETED:
        assert all(s.status in ("accepted", "rework_requested", "failed") for s in subs)


@pytest.mark.asyncio
@pytest.mark.skip(reason="Simulated adapter lifecycle was removed")
async def test_c2_no_double_pay(db: Settings) -> None:
    """C2: simulate calling accept twice; second must be skipped."""
    adapters = _make_adapters()
    spec = _make_order_spec(Scenario.SOCIAL_TRAFFIC)
    client_uuid = "task-uuid-3"
    ext_id, cost = await adapters["unu"].create_order(spec, client_uuid)

    order = Order(
        client_order_uuid=client_uuid,
        spec=spec,
        status=OrderStatus.ACTIVE,
        external_order_id=ext_id,
        cost_actual=cost,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    async with connect(db) as conn:
        await insert_order_creating(conn, order)
        await mark_order_active(conn, client_uuid, ext_id, cost)

    orch = Orchestrator(db, adapters=adapters)
    await orch.poll_all()

    # Force re-run of resolution to test duplicate guard
    task_adapter = adapters["unu"]
    
    assert task_adapter is not None
    async with connect(db) as conn:
        subs = await get_submissions_for_order(conn, client_uuid)
    for sub in subs:
        if sub.status == "new":
            await orch._verify_and_decide_submission(order, task_adapter, sub)

    async with connect(db) as conn:
        subs = await get_submissions_for_order(conn, client_uuid)

    for s in subs:
        async with connect(db) as conn:
            exists = await payment_exists(conn, spec.exchange, s.external_submission_id or "")
        if s.status == "accepted":
            assert exists


@pytest.mark.asyncio
@pytest.mark.skip(reason="Admin accept integration needs real exchange adapter credentials")
async def test_admin_accept_completes_human_reviewed_submission(db: Settings) -> None:
    adapters = _make_adapters()
    spec = _make_order_spec(Scenario.SOCIAL_TRAFFIC)
    spec = spec.model_copy(update={"exchange": "unu"})
    client_uuid = "task-admin-1"
    ext_id = "unu-task-admin1"
    suffix = hashlib.sha1(ext_id.encode("utf-8")).hexdigest()[:8]
    ext_sub_id = f"unu-sub-{suffix}-1"

    order = Order(
        client_order_uuid=client_uuid,
        spec=spec,
        status=OrderStatus.ACTIVE,
        external_order_id=ext_id,
        cost_actual=0.5,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    sub = Submission(
        submission_uuid="admin-sub-1",
        order_uuid=client_uuid,
        external_submission_id=ext_sub_id,
        executor_hint="executor-1",
        status="awaiting_admin",
        evidence="manual",
        created_at=datetime.now(UTC),
    )

    async with connect(db) as conn:
        await insert_order_creating(conn, order)
        await mark_order_active(conn, client_uuid, ext_id, 0.5)
        await insert_submission(conn, sub)

    orch = Orchestrator(db, adapters=adapters)
    decision = await orch.admin_accept_submission(sub.submission_uuid, actor="test")

    async with connect(db) as conn:
        stored_sub = await get_submission(conn, sub.submission_uuid)
        stored_order = await get_order(conn, client_uuid)
        paid = await payment_exists(conn, spec.exchange, ext_sub_id)

    assert decision["decision"] == "accepted"
    assert stored_sub is not None
    assert stored_sub.status == "accepted"
    assert stored_order is not None
    assert stored_order.status == OrderStatus.COMPLETED
    assert paid
