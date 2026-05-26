"""Quick sanity tests for the Day 6 deliverables: bot, posts/watcher, reporting/sheets."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from bot.keyboards import accept_rework, confirm_order
from config import Settings
from db.database import append_audit, connect, count_audit_entries, init_db, insert_report_row
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
    # Day 6 UX rewrite: new "no:" namespace for new-order callbacks.
    assert kb.inline_keyboard[0][0].callback_data == "no:confirm:order-456"
    assert kb.inline_keyboard[0][1].callback_data == "no:cancel"


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


async def test_report_preview_reads_db_rows(settings: Settings) -> None:
    from reporting.sheets import _get_week_range, preview_weekly_rows

    week_label, iso_week = _get_week_range()
    await init_db(settings)
    async with connect(settings) as conn:
        await insert_report_row(
            conn,
            week=iso_week,
            source_platform="vk",
            exchange="unu",
            ordered_count=5,
            actual_count=4,
            cost=0.5,
            status="completed",
        )

    rows = await preview_weekly_rows(settings)

    assert rows == [
        {
            "week": week_label,
            "source_platform": "vk",
            "exchange": "unu",
            "ordered_count": 5,
            "actual_count": 4,
            "cost": 0.5,
            "status": "completed",
        }
    ]


# --- bot/handlers -----------------------------------------------------------


class _FakeUser:
    id = 42


class _FakeMessage:
    from_user = _FakeUser()

    def __init__(self, text: str = "") -> None:
        self.text = text
        self.answers: list[str] = []

    async def answer(self, text: str, **_kwargs) -> None:
        self.answers.append(text)


async def test_orders_command_admin_wrapper_accepts_state(settings: Settings) -> None:
    from bot.handlers import cmd_orders

    settings = settings.model_copy(update={"telegram_admin_ids": [42]})
    await init_db(settings)

    message = _FakeMessage()
    await cmd_orders(message, object(), settings)

    # Day 6 UX rewrite: empty-list message now suggests the next action.
    assert len(message.answers) == 1
    assert message.answers[0].startswith("Нет активных заказов.")
    assert "Новый заказ" in message.answers[0]


async def test_accept_command_uses_admin_decision(monkeypatch, settings: Settings) -> None:
    """Day 6 UX rewrite: helper renamed to `_decide_submission` and the user-
    facing reply uses Markdown emphasis on the decision verb.
    """
    from bot import handlers

    settings = settings.model_copy(update={"telegram_admin_ids": [42]})
    calls = []

    async def fake_decide(settings_arg, submission_uuid, *, actor, accept, reason=None):
        calls.append((settings_arg, submission_uuid, actor, accept, reason))
        return {"decision": "accepted"}

    monkeypatch.setattr(handlers, "_decide_submission", fake_decide)

    message = _FakeMessage("/accept sub-1")
    await handlers.cmd_accept_submission(message, object(), settings)

    assert message.answers == ["✅ Решение: *accepted*"]
    assert calls == [(settings, "sub-1", "tg:42", True, None)]


async def test_reject_command_accepts_reason(monkeypatch, settings: Settings) -> None:
    from bot import handlers

    settings = settings.model_copy(update={"telegram_admin_ids": [42]})
    calls = []

    async def fake_decide(settings_arg, submission_uuid, *, actor, accept, reason=None):
        calls.append((settings_arg, submission_uuid, actor, accept, reason))
        return {"decision": "rejected"}

    monkeypatch.setattr(handlers, "_decide_submission", fake_decide)

    message = _FakeMessage("/reject sub-2 Нужно исправить отчёт")
    await handlers.cmd_reject_submission(message, object(), settings)

    assert message.answers == ["❌ Решение: *rejected*"]
    assert calls == [(settings, "sub-2", "tg:42", False, "Нужно исправить отчёт")]
