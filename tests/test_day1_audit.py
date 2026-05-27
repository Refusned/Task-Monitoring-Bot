"""Day 1 bug-hunt regression tests.

Each test corresponds to a finding from the Day 1 audit (ruff + Codex code review).
See CLAUDE.md and DESIGN.md for the audit context. Severity tags refer to the
audit findings list (HIGH-a / HIGH-b / HIGH-c / MEDIUM-d / MEDIUM-e / MEDIUM-f).
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

from adapters.smmcode import SmmcodeAdapter
from adapters.unu import UnuAdapter
from config import Settings
from db.database import (
    connect,
    init_db,
    insert_order_creating,
    mark_order_active,
    update_order_status,
)
from models import (
    ExternalSubmission,
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


# --- MEDIUM-d: OrderSpec contract enforcement ----------------------------------


def test_orderspec_requires_source_platform_for_traffic() -> None:
    with pytest.raises(ValidationError):
        OrderSpec(
            scenario=Scenario.SOCIAL_TRAFFIC,
            exchange="unu",
            target="https://example.com",
            quantity=5,
            max_cost=2.0,
            # source_platform missing - must fail (A8)
        )


def test_orderspec_traffic_with_source_platform_ok() -> None:
    spec = OrderSpec(
        scenario=Scenario.SOCIAL_TRAFFIC,
        exchange="unu",
        target="https://example.com",
        quantity=5,
        source_platform=SourcePlatform.VK,
        max_cost=2.0,
    )
    assert spec.source_platform == SourcePlatform.VK


def test_orderspec_rejects_empty_exchange() -> None:
    with pytest.raises(ValidationError):
        OrderSpec(
            scenario=Scenario.ACTIVITY_SUBSCRIBE,
            exchange="",
            target="https://t.me/example",
            quantity=10,
            max_cost=2.0,
        )


def test_orderspec_rejects_empty_target() -> None:
    with pytest.raises(ValidationError):
        OrderSpec(
            scenario=Scenario.ACTIVITY_SUBSCRIBE,
            exchange="smmcode",
            target="",
            quantity=10,
            max_cost=2.0,
        )


# --- HIGH-a: state-machine transitions raise on no-op (no silent failures) -----


async def test_mark_order_active_raises_on_missing_uuid(settings: Settings) -> None:
    await init_db(settings)
    async with connect(settings) as conn:
        with pytest.raises(RuntimeError, match="row missing or not in CREATING"):
            await mark_order_active(conn, "nonexistent-uuid", "ext-1", 0.5)


async def test_mark_order_active_raises_on_wrong_status(settings: Settings) -> None:
    await init_db(settings)
    spec = OrderSpec(
        scenario=Scenario.ACTIVITY_SUBSCRIBE,
        exchange="smmcode",
        target="https://t.me/x",
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
        await mark_order_active(conn, client_uuid, "ext-1", 0.5)  # CREATING -> ACTIVE
        # Row is already ACTIVE - second call must raise
        with pytest.raises(RuntimeError):
            await mark_order_active(conn, client_uuid, "ext-1", 0.5)


async def test_update_order_status_raises_on_missing_uuid(settings: Settings) -> None:
    await init_db(settings)
    async with connect(settings) as conn:
        with pytest.raises(RuntimeError, match="no row updated"):
            await update_order_status(conn, "nonexistent-uuid", OrderStatus.COMPLETED)


# --- HIGH-b: adapter submissions are stable and use the external DTO -----------


async def test_list_submissions_returns_stable_external_dtos() -> None:
    payload = {
        "success": 1,
        "reports": [
            {"id": "111", "worker_id": "w1", "status": 2, "messages": [{"text": "ok"}]},
            {"id": "112", "worker_id": "w2", "status": 1, "messages": [{"text": "ok"}]},
        ],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        body = dict(httpx.QueryParams(request.content.decode()))
        assert body["action"] == "get_reports"
        return httpx.Response(200, json=payload)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        adapter = UnuAdapter("token", http)
        subs1 = await adapter.list_submissions("task-1")
        subs2 = await adapter.list_submissions("task-1")

    assert all(isinstance(s, ExternalSubmission) for s in subs1)
    # Identity is stable: same external_submission_ids across repeated calls
    assert [s.external_submission_id for s in subs1] == [s.external_submission_id for s in subs2]
    # External DTO carries no internal UUID - orchestrator owns that mapping
    assert not hasattr(subs1[0], "submission_uuid")


# --- HIGH-c: adapters refuse placement if cost would exceed spec.max_cost ------


async def test_panel_adapter_refuses_over_max_cost() -> None:
    create_called = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal create_called
        if request.url.path.endswith("/services"):
            return httpx.Response(
                200,
                json={
                    "status": 200,
                    "services": {
                        "telegram": {
                            "subs": {
                                "136": {"service_id": "136", "price": 0.05},
                            }
                        }
                    },
                },
            )
        if request.url.path.endswith("/create_order"):
            create_called = True
        return httpx.Response(500)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        adapter = SmmcodeAdapter("token", http)
        spec = OrderSpec(
            scenario=Scenario.ACTIVITY_SUBSCRIBE,
            exchange="smmcode",
            target="https://t.me/x",
            quantity=100,  # 100 * 0.05 = 5.00 cost
            service_id="136",
            max_cost=1.0,  # below cost
        )
        with pytest.raises(ValueError, match=r"exceeds spec\.max_cost"):
            await adapter.create_order(spec, new_client_order_uuid())
    assert create_called is False


async def test_microtask_exchange_adapter_refuses_over_max_cost() -> None:
    create_called = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal create_called
        body = dict(httpx.QueryParams(request.content.decode()))
        if body.get("action") == "get_tariffs":
            return httpx.Response(
                200,
                json={"success": 1, "tariffs": [{"id": "1", "min_price_rub": 0.1}]},
            )
        if body.get("action") == "add_task_start":
            create_called = True
        return httpx.Response(500)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        adapter = UnuAdapter("token", http)
        spec = OrderSpec(
            scenario=Scenario.SOCIAL_TRAFFIC,
            exchange="unu",
            target="https://example.com",
            quantity=50,  # 50 * 0.10 = 5.00 cost
            source_platform=SourcePlatform.VK,
            service_id="1",
            max_cost=1.0,
        )
        with pytest.raises(ValueError, match=r"exceeds spec\.max_cost"):
            await adapter.create_order(spec, new_client_order_uuid())
    assert create_called is False


# --- MEDIUM-e: SQLite CHECK constraints enforce invariants at the DB layer -----


async def test_db_rejects_negative_quantity(settings: Settings) -> None:
    await init_db(settings)
    async with connect(settings) as conn:
        with pytest.raises(sqlite3.IntegrityError):
            await conn.execute(
                """
                INSERT INTO orders
                (client_order_uuid, exchange, scenario, target, quantity, max_cost,
                 status, spec_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "u1",
                    "x",
                    "activity_subscribe",
                    "t",
                    -1,
                    1.0,
                    "draft",
                    "{}",
                    "2026-01-01T00:00:00",
                    "2026-01-01T00:00:00",
                ),
            )


async def test_db_rejects_invalid_order_status(settings: Settings) -> None:
    await init_db(settings)
    async with connect(settings) as conn:
        with pytest.raises(sqlite3.IntegrityError):
            await conn.execute(
                """
                INSERT INTO orders
                (client_order_uuid, exchange, scenario, target, quantity, max_cost,
                 status, spec_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "u2",
                    "x",
                    "activity_subscribe",
                    "t",
                    1,
                    1.0,
                    "not_a_real_status",
                    "{}",
                    "2026-01-01T00:00:00",
                    "2026-01-01T00:00:00",
                ),
            )


async def test_db_rejects_zero_max_cost(settings: Settings) -> None:
    await init_db(settings)
    async with connect(settings) as conn:
        with pytest.raises(sqlite3.IntegrityError):
            await conn.execute(
                """
                INSERT INTO orders
                (client_order_uuid, exchange, scenario, target, quantity, max_cost,
                 status, spec_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "u3",
                    "x",
                    "activity_subscribe",
                    "t",
                    1,
                    0,
                    "draft",
                    "{}",
                    "2026-01-01T00:00:00",
                    "2026-01-01T00:00:00",
                ),
            )


# --- WAL & money-safety nets ---------------------------------------------------


@pytest.mark.asyncio
async def test_wal_mode_enabled(settings: Settings) -> None:
    await init_db(settings)
    async with connect(settings) as conn:
        cursor = await conn.execute("PRAGMA journal_mode")
        row = await cursor.fetchone()
    assert row is not None
    assert row[0].lower() == "wal"


def test_settings_reject_negative_daily_limit() -> None:
    with pytest.raises(ValidationError):
        Settings(daily_spend_limit=-1.0)


def test_settings_reject_negative_per_order_limit() -> None:
    with pytest.raises(ValidationError):
        Settings(per_order_spend_limit=-1.0)


def test_settings_reject_negative_auto_accept_threshold() -> None:
    with pytest.raises(ValidationError):
        Settings(auto_accept_threshold_cost=-1.0)
