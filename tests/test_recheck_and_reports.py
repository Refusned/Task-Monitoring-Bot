"""Day 2 tests — anti-fraud recheck job + report_rows writes.

We exercise the SQL helpers (real in-memory DB) and the recheck-decay logic
end-to-end with a stub verifier. The scheduler wrapper isn't tested here —
it's pure plumbing (APScheduler add_job).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from config import Settings
from db.database import (
    _iso_week,
    connect,
    init_db,
    latest_verification_measured,
    list_completed_orders_in_window,
    record_report_row,
)
from models import OrderStatus, SourcePlatform, TaskType


# --- fixtures -------------------------------------------------------------


@pytest.fixture()
def settings(tmp_path: Path) -> Settings:
    return Settings(
        db_path=tmp_path / "test.db",
        dry_run=True,
        dashboard_token="t",
        agent_tools_token="t2",
        recheck_min_age_seconds=60,
        recheck_max_age_seconds=86400 * 3,
    )


async def _seed_order(
    settings: Settings,
    *,
    order_uuid: str,
    status: OrderStatus,
    exchange: str = "smmcode",
    platform: str = "youtube",
    quantity: int = 100,
    cost: float = 10.0,
    updated_at: datetime | None = None,
) -> None:
    iso = (updated_at or datetime.now(UTC)).isoformat(timespec="seconds")
    async with connect(settings) as conn:
        await conn.execute(
            """
            INSERT INTO orders (
                client_order_uuid, exchange, scenario, target, quantity, service_id,
                source_platform, max_cost, cost_actual, status, spec_json,
                created_at, updated_at
            ) VALUES (?, ?, 'activity_like', 'https://example.com', ?, '1',
                      ?, 100.0, ?, ?, '{}', ?, ?)
            """,
            (order_uuid, exchange, quantity, platform, cost, status.value, iso, iso),
        )
        await conn.commit()


async def _seed_verification(
    settings: Settings,
    *,
    order_uuid: str,
    measured: float,
    created_at: datetime | None = None,
) -> None:
    ts = (created_at or datetime.now(UTC) - timedelta(minutes=1)).isoformat(timespec="microseconds")
    async with connect(settings) as conn:
        await conn.execute(
            """
            INSERT INTO verifications
            (verification_uuid, order_uuid, verdict, measured, expected, reason, created_at)
            VALUES (?, ?, 'auto_pass', ?, 100, 'seed', ?)
            """,
            (f"v_{order_uuid[:8]}_{int(measured * 1000)}", order_uuid, measured, ts),
        )
        await conn.commit()


# --- _iso_week ------------------------------------------------------------


def test_iso_week_format() -> None:
    dt = datetime(2026, 5, 28, 12, 0, 0, tzinfo=UTC)  # Thu of W22 2026
    assert _iso_week(dt) == "2026-W22"


def test_iso_week_january_edge() -> None:
    dt = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)  # Thu — ISO week 1 of 2026
    assert _iso_week(dt) == "2026-W01"


# --- record_report_row ----------------------------------------------------


async def test_record_report_row_inserts(settings: Settings) -> None:
    await init_db(settings)
    async with connect(settings) as conn:
        inserted = await record_report_row(
            conn,
            order_uuid="order-1",
            source_platform="youtube",
            exchange="smmcode",
            ordered_count=100,
            actual_count=108,
            cost=9.68,
            status="completed",
        )
        assert inserted is True
        cursor = await conn.execute(
            "SELECT week, exchange, source_platform, ordered_count, actual_count, "
            "cost, status, order_uuid FROM report_rows"
        )
        rows = await cursor.fetchall()
    assert len(rows) == 1
    r = rows[0]
    assert r["exchange"] == "smmcode"
    assert r["source_platform"] == "youtube"
    assert r["ordered_count"] == 100
    assert r["actual_count"] == 108
    assert abs(r["cost"] - 9.68) < 1e-6
    assert r["status"] == "completed"
    assert r["order_uuid"] == "order-1"
    assert r["week"].startswith("20")  # YYYY-Wxx


async def test_record_report_row_is_idempotent(settings: Settings) -> None:
    """Second call with same order_uuid silently no-ops thanks to unique index."""
    await init_db(settings)
    async with connect(settings) as conn:
        first = await record_report_row(
            conn,
            order_uuid="order-dup",
            source_platform="telegram",
            exchange="smmcode",
            ordered_count=10,
            actual_count=11,
            cost=0.47,
            status="completed",
        )
        second = await record_report_row(
            conn,
            order_uuid="order-dup",
            source_platform="telegram",
            exchange="smmcode",
            ordered_count=10,
            actual_count=11,
            cost=0.47,
            status="completed",
        )
        assert first is True
        assert second is False
        cursor = await conn.execute(
            "SELECT COUNT(*) AS c FROM report_rows WHERE order_uuid = ?",
            ("order-dup",),
        )
        row = await cursor.fetchone()
    assert row["c"] == 1


async def test_record_report_row_null_actual_and_cost(settings: Settings) -> None:
    """Failed orders may have no measured value and no cost — both NULL allowed."""
    await init_db(settings)
    async with connect(settings) as conn:
        ok = await record_report_row(
            conn,
            order_uuid="order-fail",
            source_platform="vk",
            exchange="smmcode",
            ordered_count=50,
            actual_count=None,
            cost=None,
            status="failed",
        )
    assert ok is True


# --- list_completed_orders_in_window -------------------------------------


async def test_window_picks_orders_within_range(settings: Settings) -> None:
    await init_db(settings)
    now = datetime.now(UTC)
    # too fresh (10 sec, < 60 sec min_age) → excluded
    await _seed_order(
        settings,
        order_uuid="too-fresh",
        status=OrderStatus.COMPLETED,
        updated_at=now - timedelta(seconds=10),
    )
    # too old (5 days) → excluded
    await _seed_order(
        settings,
        order_uuid="too-old",
        status=OrderStatus.COMPLETED,
        updated_at=now - timedelta(days=5),
    )
    # in window (1 hour ago) → included
    await _seed_order(
        settings,
        order_uuid="in-window",
        status=OrderStatus.COMPLETED,
        updated_at=now - timedelta(hours=1),
    )
    # not completed → excluded
    await _seed_order(
        settings,
        order_uuid="still-active",
        status=OrderStatus.ACTIVE,
        updated_at=now - timedelta(hours=1),
    )

    async with connect(settings) as conn:
        rows = await list_completed_orders_in_window(
            conn,
            min_age_seconds=settings.recheck_min_age_seconds,  # 60
            max_age_seconds=settings.recheck_max_age_seconds,  # 3 days
        )
    uuids = {r["order_uuid"] for r in rows}
    assert uuids == {"in-window"}


# --- latest_verification_measured ----------------------------------------


async def test_latest_verification_returns_most_recent(settings: Settings) -> None:
    await init_db(settings)
    await _seed_order(settings, order_uuid="o1", status=OrderStatus.COMPLETED)
    earlier = datetime.now(UTC) - timedelta(hours=2)
    later = datetime.now(UTC) - timedelta(minutes=5)
    await _seed_verification(settings, order_uuid="o1", measured=100.0, created_at=earlier)
    await _seed_verification(settings, order_uuid="o1", measured=120.0, created_at=later)
    async with connect(settings) as conn:
        result = await latest_verification_measured(conn, "o1")
    assert result == 120.0


async def test_latest_verification_none_when_no_rows(settings: Settings) -> None:
    await init_db(settings)
    await _seed_order(settings, order_uuid="o1", status=OrderStatus.COMPLETED)
    async with connect(settings) as conn:
        result = await latest_verification_measured(conn, "o1")
    assert result is None


# --- recheck_completed_orders (integration with stub verifier) -----------


class _StubVerifier:
    name = "stub"

    def __init__(self, value: float | None) -> None:
        self._value = value
        self.calls = 0

    def supports(self, platform: SourcePlatform, metric: TaskType) -> bool:
        return True

    async def fetch_metric(self, url: str, metric: TaskType) -> float | None:
        self.calls += 1
        return self._value


async def test_recheck_detects_fraud_decay(settings: Settings, monkeypatch) -> None:
    """Prior measurement was 200; current is 100 → 50% drop → fraud event."""
    from app.state import AppState, EventBus
    from db.database import connect as _connect
    from scheduler.jobs import recheck_completed_orders

    await init_db(settings)
    now = datetime.now(UTC)
    await _seed_order(
        settings,
        order_uuid="fraud-order",
        status=OrderStatus.COMPLETED,
        updated_at=now - timedelta(hours=2),
    )
    await _seed_verification(settings, order_uuid="fraud-order", measured=200.0)

    # Seed a snapshot for that order so get_snapshot_for_order can return it.
    async with _connect(settings) as conn:
        await conn.execute(
            """
            INSERT INTO metric_snapshots
            (snapshot_id, platform, target_url, metric, baseline_value, captured_at, raw_json, order_uuid)
            VALUES ('snap-1', 'youtube', 'https://example.com', 'likes', 10.0,
                    ?, '{}', 'fraud-order')
            """,
            (now.isoformat(timespec="seconds"),),
        )
        await conn.commit()

    import httpx

    state = AppState(
        settings=settings,
        http_client=httpx.AsyncClient(),
        adapters={},
        verifiers=[_StubVerifier(value=100.0)],
        event_bus=EventBus(),
    )
    try:
        await recheck_completed_orders(state)
    finally:
        await state.http_client.aclose()

    # Verify: a new verifications row with verdict=fail was written.
    # We don't rely on created_at ordering (seconds-resolution races with the
    # seed row); assert at least one fail row exists with "fraud" in reason.
    async with _connect(settings) as conn:
        cursor = await conn.execute(
            "SELECT verdict, reason FROM verifications WHERE order_uuid = ?",
            ("fraud-order",),
        )
        rows = await cursor.fetchall()
    verdicts = [r["verdict"] for r in rows]
    assert "fail" in verdicts
    fail_reasons = [r["reason"] for r in rows if r["verdict"] == "fail"]
    assert any("fraud" in r.lower() for r in fail_reasons)


async def test_recheck_records_stable_when_no_drop(settings: Settings) -> None:
    from app.state import AppState, EventBus
    from db.database import connect as _connect
    from scheduler.jobs import recheck_completed_orders

    await init_db(settings)
    now = datetime.now(UTC)
    await _seed_order(
        settings, order_uuid="stable-1",
        status=OrderStatus.COMPLETED,
        updated_at=now - timedelta(hours=2),
    )
    await _seed_verification(settings, order_uuid="stable-1", measured=200.0)
    async with _connect(settings) as conn:
        await conn.execute(
            """
            INSERT INTO metric_snapshots
            (snapshot_id, platform, target_url, metric, baseline_value, captured_at, raw_json, order_uuid)
            VALUES ('snap-2', 'youtube', 'https://example.com', 'likes', 10.0,
                    ?, '{}', 'stable-1')
            """,
            (now.isoformat(timespec="seconds"),),
        )
        await conn.commit()

    import httpx

    state = AppState(
        settings=settings,
        http_client=httpx.AsyncClient(),
        adapters={},
        verifiers=[_StubVerifier(value=210.0)],  # slight growth, not a drop
        event_bus=EventBus(),
    )
    try:
        await recheck_completed_orders(state)
    finally:
        await state.http_client.aclose()

    async with _connect(settings) as conn:
        cursor = await conn.execute(
            "SELECT verdict FROM verifications WHERE order_uuid = ? "
            "ORDER BY created_at DESC LIMIT 1",
            ("stable-1",),
        )
        row = await cursor.fetchone()
    assert row["verdict"] == "auto_pass"


async def test_recheck_skips_order_outside_window(settings: Settings) -> None:
    """Order completed 5 days ago shouldn't be re-checked anymore."""
    from app.state import AppState, EventBus
    from scheduler.jobs import recheck_completed_orders

    await init_db(settings)
    now = datetime.now(UTC)
    await _seed_order(
        settings, order_uuid="too-old",
        status=OrderStatus.COMPLETED,
        updated_at=now - timedelta(days=5),
    )
    await _seed_verification(settings, order_uuid="too-old", measured=200.0)

    verifier = _StubVerifier(value=10.0)
    import httpx

    state = AppState(
        settings=settings,
        http_client=httpx.AsyncClient(),
        adapters={},
        verifiers=[verifier],
        event_bus=EventBus(),
    )
    try:
        await recheck_completed_orders(state)
    finally:
        await state.http_client.aclose()

    # Verifier should NOT have been called.
    assert verifier.calls == 0
