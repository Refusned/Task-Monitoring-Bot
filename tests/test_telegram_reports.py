"""Day 4 tests — admin order endpoints + daily digest aggregation.

We don't hit Telegram API (would need a bot token & live network); we patch
the HTTP client at the call site and verify what payload would have been sent.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from config import Settings
from db.database import connect, init_db
from models import OrderStatus


# --- fixtures -----------------------------------------------------------


@pytest.fixture()
def settings(tmp_path: Path) -> Settings:
    return Settings(
        db_path=tmp_path / "test.db",
        dry_run=True,
        dashboard_token="dash",
        agent_tools_token="agent",
        telegram_bot_token="fake:bot:token",
        telegram_admin_ids=[111, 222],
    )


async def _seed_order(
    settings: Settings,
    *,
    uuid: str,
    status: OrderStatus,
    cost: float | None = 10.0,
    quantity: int = 100,
    exchange: str = "smmcode",
    platform: str = "youtube",
    created_offset_hours: float = 0.0,
) -> None:
    iso = (datetime.now(UTC) - timedelta(hours=created_offset_hours)).isoformat(timespec="seconds")
    async with connect(settings) as conn:
        await conn.execute(
            """
            INSERT INTO orders (
                client_order_uuid, exchange, scenario, target, quantity,
                service_id, source_platform, max_cost, cost_actual, status,
                spec_json, created_at, updated_at
            ) VALUES (?, ?, 'activity_like', 'https://example.com', ?, '1',
                      ?, 100.0, ?, ?, '{}', ?, ?)
            """,
            (uuid, exchange, quantity, platform, cost, status.value, iso, iso),
        )
        await conn.commit()


# --- daily digest aggregation -----------------------------------------------


async def test_digest_skips_when_no_telegram(tmp_path: Path) -> None:
    """No bot token / no admins → no work, no errors."""
    from app.state import AppState, EventBus
    from scheduler.jobs import send_daily_digest

    settings = Settings(
        db_path=tmp_path / "x.db", dry_run=True,
        dashboard_token="t", agent_tools_token="t",
        telegram_bot_token="", telegram_admin_ids=[],
    )
    await init_db(settings)
    state = AppState(settings=settings, http_client=httpx.AsyncClient(),
                     adapters={}, verifiers=[], event_bus=EventBus())
    try:
        result = await send_daily_digest(state)
    finally:
        await state.http_client.aclose()
    assert result["sent"] == 0
    assert "not configured" in result["reason"]


async def test_digest_aggregates_24h_orders(settings: Settings) -> None:
    from app.state import AppState, EventBus
    from scheduler.jobs import send_daily_digest

    await init_db(settings)
    # 3 completed in last 24h
    await _seed_order(settings, uuid="c1", status=OrderStatus.COMPLETED, cost=5.0)
    await _seed_order(settings, uuid="c2", status=OrderStatus.COMPLETED, cost=7.5)
    await _seed_order(settings, uuid="c3", status=OrderStatus.COMPLETED, cost=3.0,
                      exchange="unu")
    # 1 failed today
    await _seed_order(settings, uuid="f1", status=OrderStatus.FAILED, cost=None)
    # 1 verifying today (needs review)
    await _seed_order(settings, uuid="v1", status=OrderStatus.VERIFYING, cost=2.0)
    # 1 old (2 days ago) — should NOT count
    await _seed_order(settings, uuid="old1", status=OrderStatus.COMPLETED,
                      cost=999.0, created_offset_hours=48)

    # Capture sent messages.
    sent: list[tuple[int, str]] = []

    async def fake_send(client, token, chat_id, text, **kwargs):
        sent.append((chat_id, text))

    import scheduler.jobs as jobs

    state = AppState(settings=settings, http_client=httpx.AsyncClient(),
                     adapters={}, verifiers=[], event_bus=EventBus())
    original = jobs._telegram_send_html
    jobs._telegram_send_html = fake_send
    try:
        result = await send_daily_digest(state)
    finally:
        jobs._telegram_send_html = original
        await state.http_client.aclose()

    # Two admins → two messages
    assert result["sent"] == 2
    assert {chat for chat, _ in sent} == {111, 222}
    text = sent[0][1]
    # Digest reflects today's counts only (old1 excluded).
    # New format (post-2026-05-28): emphasizes finalized orders for the morning digest.
    assert "Утренняя сводка" in text
    assert "Создано за сутки: <b>5</b>" in text
    # Finalized in last 24h (those whose status moved to completed/failed): 4 (3 completed + 1 failed).
    # All seeded orders have updated_at=now, so all final-state ones count.
    assert "Проверено и закрыто за сутки: 4" in text
    assert "Принято: <b>3</b>" in text
    assert "Возвращено / провал: <b>1</b>" in text
    assert "На проверке: <b>1</b>" in text
    # Money: 5+7.5+3+2 = 17.5 (failed had None, skipped)
    assert "17.50₽" in text


# --- admin order finalize endpoint -----------------------------------------


async def test_admin_finalize_helper_transitions_status(settings: Settings, monkeypatch) -> None:
    """Test `_admin_finalize_order` helper: verifying → completed, writes
    verifications row, writes report_rows entry, idempotent."""
    from app.main import _admin_finalize_order
    from app.state import AppState, EventBus
    from fastapi import HTTPException

    await init_db(settings)
    await _seed_order(settings, uuid="v-1", status=OrderStatus.VERIFYING, cost=10.0)
    # Seed a prior measurement so the report_row has actual_count. Use an
    # offset 1 hour ago so admin's verifications row is strictly newer
    # regardless of seconds-precision races.
    earlier = (datetime.now(UTC) - timedelta(hours=1)).isoformat(timespec="microseconds")
    async with connect(settings) as conn:
        await conn.execute(
            """
            INSERT INTO verifications
            (verification_uuid, order_uuid, verdict, measured, expected, reason, created_at)
            VALUES ('vp1', 'v-1', 'needs_human_review', 99.0, 100, 'between bands', ?)
            """,
            (earlier,),
        )
        await conn.commit()

    state = AppState(settings=settings, http_client=httpx.AsyncClient(),
                     adapters={}, verifiers=[], event_bus=EventBus())
    try:
        result = await _admin_finalize_order(state, "v-1", OrderStatus.COMPLETED, "looks good")
    finally:
        await state.http_client.aclose()

    assert result["new_status"] == "completed"
    assert result["reason"] == "looks good"

    async with connect(settings) as conn:
        # Order status moved
        c = await conn.execute("SELECT status FROM orders WHERE client_order_uuid = ?", ("v-1",))
        row = await c.fetchone()
        assert row["status"] == "completed"
        # Verifications row written. Find by `reason LIKE 'admin:%'` since
        # SQLite's seconds-precision created_at makes DESC ordering unstable
        # vs other rows from the same second.
        c = await conn.execute(
            "SELECT verdict, reason FROM verifications WHERE order_uuid = ? "
            "AND reason LIKE 'admin:%'",
            ("v-1",),
        )
        v = await c.fetchone()
        assert v is not None
        assert v["verdict"] == "auto_pass"
        assert "looks good" in v["reason"]
        # Report row written
        c = await conn.execute(
            "SELECT actual_count, cost, status FROM report_rows WHERE order_uuid = ?", ("v-1",)
        )
        r = await c.fetchone()
        assert r["status"] == "completed"
        assert r["actual_count"] == 99
        assert abs(r["cost"] - 10.0) < 1e-6


async def test_admin_finalize_rejects_non_verifying(settings: Settings) -> None:
    from app.main import _admin_finalize_order
    from app.state import AppState, EventBus
    from fastapi import HTTPException

    await init_db(settings)
    await _seed_order(settings, uuid="c-1", status=OrderStatus.COMPLETED, cost=1.0)
    state = AppState(settings=settings, http_client=httpx.AsyncClient(),
                     adapters={}, verifiers=[], event_bus=EventBus())
    try:
        with pytest.raises(HTTPException) as exc_info:
            await _admin_finalize_order(state, "c-1", OrderStatus.COMPLETED, "x")
        assert exc_info.value.status_code == 409
    finally:
        await state.http_client.aclose()


async def test_admin_finalize_rejects_missing_order(settings: Settings) -> None:
    from app.main import _admin_finalize_order
    from app.state import AppState, EventBus
    from fastapi import HTTPException

    await init_db(settings)
    state = AppState(settings=settings, http_client=httpx.AsyncClient(),
                     adapters={}, verifiers=[], event_bus=EventBus())
    try:
        with pytest.raises(HTTPException) as exc_info:
            await _admin_finalize_order(state, "nope", OrderStatus.COMPLETED, "x")
        assert exc_info.value.status_code == 404
    finally:
        await state.http_client.aclose()


# --- inline keyboard helper -------------------------------------------------


def test_order_inline_keyboard_url_uses_dashboard_base(settings: Settings) -> None:
    from scheduler.jobs import _order_inline_keyboard
    from models import VerificationVerdict

    settings_with_base = settings.model_copy(update={"dashboard_base_url": "https://example.com"})
    kb = _order_inline_keyboard(settings_with_base, "uuid-abc-123", VerificationVerdict.AUTO_PASS)
    assert kb is not None
    btn = kb[0][0]
    assert "example.com" in btn["url"]
    assert "uuid-abc-123" in btn["url"]
    assert "Открыть" in btn["text"]


def test_order_inline_keyboard_for_human_review_changes_label(settings: Settings) -> None:
    from scheduler.jobs import _order_inline_keyboard
    from models import VerificationVerdict

    settings_with_base = settings.model_copy(update={"dashboard_base_url": "https://example.com"})
    kb = _order_inline_keyboard(
        settings_with_base, "u-1", VerificationVerdict.NEEDS_HUMAN_REVIEW
    )
    assert "Подтвердить" in kb[0][0]["text"]
