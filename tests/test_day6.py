"""Quick sanity tests for the Day 6 deliverables: bot, posts/watcher, reporting/sheets."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from bot.keyboards import accept_rework, confirm_order
from config import Settings
from db.database import append_audit, connect, count_audit_entries, init_db
from posts.watcher import _extract_last_post_id, _load_state, _save_state
from reporting.sheets import _get_week_range


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        db_path=tmp_path / "test.db",
        dry_run=True,
        target_social_accounts=["https://t.me/test_channel"],
    )


# --- bot/keyboards -----------------------------------------------------------


def test_accept_rework_keyboard() -> None:
    kb = accept_rework("sub-123")
    assert len(kb.inline_keyboard) == 1
    assert kb.inline_keyboard[0][0].callback_data == "accept:sub-123"
    assert kb.inline_keyboard[0][1].callback_data == "reject:sub-123"


def test_confirm_order_keyboard() -> None:
    kb = confirm_order("order-456")
    assert kb.inline_keyboard[0][0].callback_data == "confirm_order:order-456"
    assert kb.inline_keyboard[0][1].callback_data == "cancel_order:order-456"


# --- posts/watcher -----------------------------------------------------------


def test_extract_last_post_id_telegram() -> None:
    html = '<a href="/test_channel/42">x</a><a href="/test_channel/99">y</a>'
    pid = _extract_last_post_id("https://t.me/test_channel", html)
    assert pid == "/test_channel/99"


def test_extract_last_post_id_generic() -> None:
    html = "<html><body>some content here</body></html>"
    pid = _extract_last_post_id("https://example.com", html)
    assert pid is not None
    assert len(pid) == 16


async def test_watcher_state_persistence(tmp_path: Path) -> None:
    import posts.watcher as watcher

    old_path = watcher._WATCHED_STATE_PATH
    watcher._WATCHED_STATE_PATH = tmp_path / "watcher_state.json"
    try:
        _save_state({"https://t.me/x": "post-1"})
        loaded = _load_state()
        assert loaded == {"https://t.me/x": "post-1"}
    finally:
        watcher._WATCHED_STATE_PATH = old_path


async def test_run_once_no_accounts() -> None:
    s = Settings(dry_run=True, target_social_accounts=[])
    from posts.watcher import run_once

    result = await run_once(s)
    assert result == []


# --- reporting/sheets --------------------------------------------------------


def test_week_range_format() -> None:
    anchor = datetime(2026, 5, 24, 12, 0, 0)  # Sunday
    label, iso = _get_week_range(anchor)
    assert "2026-05-18" in label  # Monday
    assert "2026-05-24" in label  # Sunday
    assert iso.startswith("2026-W")


async def test_reporter_without_credentials_warns(settings: Settings) -> None:
    from reporting.sheets import SheetsReporter

    reporter = SheetsReporter(settings)
    with pytest.raises(RuntimeError, match="CREDENTIALS_FILE"):
        reporter._connect()


async def test_audit_log_from_reporting(settings: Settings) -> None:
    await init_db(settings)
    async with connect(settings) as conn:
        before = await count_audit_entries(conn)
        await append_audit(conn, actor="test", event="sheets_ping")
        after = await count_audit_entries(conn)
    assert after == before + 1
