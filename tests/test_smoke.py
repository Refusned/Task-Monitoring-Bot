"""Day 1 smoke tests: foundation wiring works end-to-end on fake adapters."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from adapters.base import Capability
from adapters.fake import FakePanelAdapter, FakeTaskExchangeAdapter
from config import Settings
from db.database import (
    append_audit,
    connect,
    count_audit_entries,
    get_order,
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
    new_client_order_uuid,
)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(db_path=tmp_path / "test.db")


async def test_init_db_idempotent(settings: Settings) -> None:
    await init_db(settings)
    await init_db(settings)  # second call must not error


async def test_fake_panel_capabilities() -> None:
    adapter = FakePanelAdapter()
    caps = adapter.capabilities()
    assert Capability.CREATE_ORDER in caps
    assert Capability.GET_ORDER_STATUS in caps
    # Panels do not accept/reject submissions
    assert Capability.ACCEPT_SUBMISSION not in caps


async def test_fake_task_exchange_capabilities() -> None:
    adapter = FakeTaskExchangeAdapter()
    caps = adapter.capabilities()
    assert Capability.LIST_SUBMISSIONS in caps
    assert Capability.ACCEPT_SUBMISSION in caps
    assert Capability.REJECT_SUBMISSION in caps


async def test_create_panel_order_round_trip(settings: Settings) -> None:
    await init_db(settings)
    adapter = FakePanelAdapter()
    spec = OrderSpec(
        scenario=Scenario.ACTIVITY_SUBSCRIBE,
        exchange="fake_panel",
        target="https://t.me/example",
        quantity=10,
        max_cost=2.0,
    )
    client_uuid = new_client_order_uuid()
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
        await append_audit(conn, actor="test", event="order_creating", order_uuid=client_uuid)

    external_id, cost = await adapter.create_order(spec, client_uuid)
    assert external_id.startswith("fake-panel-")
    assert cost == pytest.approx(10 * 0.05)

    async with connect(settings) as conn:
        await mark_order_active(conn, client_uuid, external_id, cost)
        stored = await get_order(conn, client_uuid)
    assert stored is not None
    assert stored.status == OrderStatus.ACTIVE
    assert stored.external_order_id == external_id

    # FakePanelAdapter completes after 2 polls
    assert await adapter.get_order_status(external_id) == "in_progress"
    assert await adapter.get_order_status(external_id) == "completed"


async def test_fake_task_exchange_yields_submissions() -> None:
    adapter = FakeTaskExchangeAdapter(polls_to_yield=1, submissions_per_order=3)
    spec = OrderSpec(
        scenario=Scenario.SOCIAL_TRAFFIC,
        exchange="fake_task_exchange",
        target="https://example.com",
        quantity=3,
        source_platform=SourcePlatform.VK,
        max_cost=2.0,
    )
    external_id, _cost = await adapter.create_order(spec, new_client_order_uuid())

    # First poll yields submissions; order still in progress while they're unresolved
    assert await adapter.get_order_status(external_id) == "in_progress"
    subs = await adapter.list_submissions(external_id)
    assert len(subs) == 3

    # Accept first two (good evidence), reject the third (weak)
    await adapter.accept_submission(external_id, subs[0].external_submission_id)
    await adapter.accept_submission(external_id, subs[1].external_submission_id)
    await adapter.reject_submission(
        external_id, subs[2].external_submission_id, reason="weak evidence"
    )

    # Once all submissions are terminal, the order is completed
    assert await adapter.get_order_status(external_id) == "completed"


async def test_audit_log_appends(settings: Settings) -> None:
    await init_db(settings)
    async with connect(settings) as conn:
        before = await count_audit_entries(conn)
        await append_audit(conn, actor="test", event="ping")
        after = await count_audit_entries(conn)
    assert after == before + 1
