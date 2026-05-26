"""End-to-end tests for the button-driven (interactive) bot UX.

We exercise the new-order FSM, the reply-keyboard shortcuts, the orders list
keyboard, and the cancel callback against synthetic `Message` / `CallbackQuery`
shims and an in-memory `FSMContext`. No real Telegram involved.

The test scope is the contract surface the user actually hits in chat: every
flow that the persistent menu and inline buttons can produce.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

from bot import handlers, keyboards
from config import Settings
from db.database import connect, init_db, insert_order_creating, mark_order_active
from models import Order, OrderSpec, OrderStatus, Scenario

# ---------------------------------------------------------------------------
# Shims
# ---------------------------------------------------------------------------


class _FakeUser:
    def __init__(self, user_id: int = 42) -> None:
        self.id = user_id


class _FakeChat:
    def __init__(self, chat_id: int = 42) -> None:
        self.id = chat_id


class _FakeMessage:
    """Minimal Message stand-in that records `answer` calls.

    The bot handlers only call `answer(...)` on incoming Message objects, so the
    shim only needs to capture text + reply_markup.
    """

    def __init__(self, text: str = "", user_id: int = 42) -> None:
        self.text = text
        self.from_user = _FakeUser(user_id)
        self.chat = _FakeChat(user_id)
        self.answers: list[tuple[str, Any]] = []  # (text, reply_markup)

    async def answer(self, text: str, **kwargs: Any) -> None:
        self.answers.append((text, kwargs.get("reply_markup")))


class _FakeInlineMessage:
    """Stand-in for the `message` attached to a CallbackQuery."""

    def __init__(self) -> None:
        self.edits: list[tuple[str, Any]] = []

    async def edit_text(self, text: str, **kwargs: Any) -> None:
        self.edits.append((text, kwargs.get("reply_markup")))


class _FakeQuery:
    def __init__(self, data: str, user_id: int = 42) -> None:
        self.data = data
        self.from_user = _FakeUser(user_id)
        self.message = _FakeInlineMessage()
        self.answers: list[tuple[str | None, bool]] = []  # (text, show_alert)

    async def answer(self, text: str | None = None, show_alert: bool = False, **_kw) -> None:
        self.answers.append((text, show_alert))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def settings(tmp_path: Path) -> Settings:
    s = Settings(
        db_path=tmp_path / "test.db",
        dry_run=True,
        telegram_admin_ids=[42],
        per_order_spend_limit=2.0,
    )
    await init_db(s)
    return s


@pytest.fixture
def state() -> FSMContext:
    storage = MemoryStorage()
    return FSMContext(
        storage=storage,
        key=StorageKey(bot_id=1, chat_id=42, user_id=42),
    )


# ---------------------------------------------------------------------------
# /start → main menu surfaces all top-level actions
# ---------------------------------------------------------------------------


async def test_start_shows_main_menu_and_clears_state(
    state: FSMContext, settings: Settings
) -> None:
    await state.update_data(garbage="data")
    msg = _FakeMessage()
    await handlers.cmd_start(msg, state, settings=settings)
    assert len(msg.answers) == 1
    text, kb = msg.answers[0]
    assert "Бот мониторинга бирж" in text
    # Reply keyboard is attached and contains the 8 main-menu buttons.
    assert kb is not None
    labels = {btn.text for row in kb.keyboard for btn in row}
    for expected in (
        keyboards.BTN_NEW_ORDER,
        keyboards.BTN_DASHBOARD,
        keyboards.BTN_ORDERS,
        keyboards.BTN_CHECK,
        keyboards.BTN_REVIEW,
        keyboards.BTN_REPORT,
        keyboards.BTN_HEALTH,
        keyboards.BTN_CANCEL,
    ):
        assert expected in labels
    # FSM cleared.
    assert await state.get_state() is None


async def test_menu_clears_state_and_shows_keyboard(state: FSMContext, settings: Settings) -> None:
    await state.set_state(handlers.NewOrderFSM.target)
    msg = _FakeMessage("/menu")
    await handlers.cmd_menu(msg, state, settings=settings)
    assert await state.get_state() is None
    assert msg.answers[0][1] is not None  # main_menu keyboard attached


# ---------------------------------------------------------------------------
# Reply-keyboard taps act like commands
# ---------------------------------------------------------------------------


async def test_reply_new_order_taps_into_scenario_step(
    state: FSMContext, settings: Settings
) -> None:
    msg = _FakeMessage(keyboards.BTN_NEW_ORDER)
    await handlers.reply_new_order(msg, state, settings=settings)
    assert await state.get_state() == handlers.NewOrderFSM.scenario.state
    # The scenario_choice inline keyboard appears under the prompt.
    _text, kb = msg.answers[-1]
    assert kb is not None
    callback_data = [btn.callback_data for row in kb.inline_keyboard for btn in row]
    assert any(cd and cd.startswith("no:scenario:") for cd in callback_data)


async def test_reply_dashboard_works(state: FSMContext, settings: Settings) -> None:
    msg = _FakeMessage(keyboards.BTN_DASHBOARD)
    await handlers.reply_dashboard(msg, state, settings=settings)
    text, _ = msg.answers[0]
    assert "Сводка" in text or "Dashboard" in text


async def test_reply_orders_empty(state: FSMContext, settings: Settings) -> None:
    msg = _FakeMessage(keyboards.BTN_ORDERS)
    await handlers.reply_orders(msg, state, settings=settings)
    text, _ = msg.answers[0]
    assert "Нет активных заказов" in text


async def test_reply_cancel_clears_state(state: FSMContext, settings: Settings) -> None:
    await state.set_state(handlers.NewOrderFSM.quantity)
    msg = _FakeMessage(keyboards.BTN_CANCEL)
    await handlers.reply_cancel(msg, state, settings=settings)
    assert await state.get_state() is None
    assert "отменено" in msg.answers[0][0].lower()


async def test_reply_health_lists_integrations(state: FSMContext, settings: Settings) -> None:
    msg = _FakeMessage(keyboards.BTN_HEALTH)
    await handlers.reply_health(msg, state, settings=settings)
    text, _ = msg.answers[0]
    for name in ("smmcode", "unu", "advego"):
        assert name in text


# ---------------------------------------------------------------------------
# New-order FSM end-to-end via inline buttons
# ---------------------------------------------------------------------------


async def test_new_order_full_flow_activity_subscribe(
    state: FSMContext, settings: Settings
) -> None:
    """Full happy-path: scenario → exchange → target → quantity → confirm."""
    # Step 1: scenario
    msg = _FakeMessage(keyboards.BTN_NEW_ORDER)
    await handlers.reply_new_order(msg, state, settings=settings)
    assert await state.get_state() == handlers.NewOrderFSM.scenario.state

    # Step 1 → 2: tap ACTIVITY_SUBSCRIBE
    q = _FakeQuery("no:scenario:activity_subscribe")
    await handlers.cb_scenario(q, state, settings=settings)
    assert await state.get_state() == handlers.NewOrderFSM.exchange.state
    assert "activity_subscribe" in q.message.edits[-1][0]

    # Step 2 → 3: tap smmcode. With empty API key in tests, the catalogue
    # fetch fails and the bot routes the user to the manual-entry fallback.
    q2 = _FakeQuery("no:exchange:smmcode")
    await handlers.cb_exchange(q2, state, settings=settings)
    assert await state.get_state() == handlers.NewOrderFSM.service.state

    # Step 3 → 3.5: tap «Ввести ID вручную»
    q_manual = _FakeQuery("no:service_manual")
    await handlers.cb_service_manual(q_manual, state, settings=settings)
    assert await state.get_state() == handlers.NewOrderFSM.service_manual.state

    # Step 3.5 → 4: type service_id by hand
    msg_sid = _FakeMessage("136")
    await handlers.fsm_service_manual(msg_sid, state, settings=settings)
    assert await state.get_state() == handlers.NewOrderFSM.target.state

    # Step 4 → 5: target free-text
    msg2 = _FakeMessage("https://t.me/our_channel")
    await handlers.fsm_target(msg2, state, settings=settings)
    assert await state.get_state() == handlers.NewOrderFSM.quantity.state

    # Step 5 → confirm card
    msg3 = _FakeMessage("10")
    await handlers.fsm_quantity(msg3, state, settings=settings)
    assert await state.get_state() == handlers.NewOrderFSM.confirm.state
    confirm_text, confirm_kb = msg3.answers[-1]
    assert "подтверждение" in confirm_text.lower()
    confirm_cb = [btn.callback_data for row in confirm_kb.inline_keyboard for btn in row]
    assert any(cd and cd.startswith("no:confirm:") for cd in confirm_cb)


async def test_new_order_traffic_requires_platform(state: FSMContext, settings: Settings) -> None:
    """SOCIAL_TRAFFIC scenario triggers the extra source-platform step."""
    await handlers.reply_new_order(_FakeMessage(), state, settings=settings)
    await handlers.cb_scenario(_FakeQuery("no:scenario:social_traffic"), state, settings=settings)
    await handlers.cb_exchange(_FakeQuery("no:exchange:unu"), state, settings=settings)
    # Pass through the service step via manual entry (catalogue fetch fails
    # without a real API key in tests).
    await handlers.cb_service_manual(_FakeQuery("no:service_manual"), state, settings=settings)
    await handlers.fsm_service_manual(_FakeMessage("48"), state, settings=settings)
    await handlers.fsm_target(_FakeMessage("https://example.com"), state, settings=settings)
    await handlers.fsm_quantity(_FakeMessage("3"), state, settings=settings)
    assert await state.get_state() == handlers.NewOrderFSM.source_platform.state

    q = _FakeQuery("no:platform:vk")
    await handlers.cb_source_platform(q, state, settings=settings)
    assert await state.get_state() == handlers.NewOrderFSM.confirm.state
    # The confirm card was rendered via edit_text on the platform-choice msg.
    edited_text, edited_kb = q.message.edits[-1]
    assert "vk" in edited_text or "VK" in edited_text
    assert any(
        cd and cd.startswith("no:confirm:")
        for row in edited_kb.inline_keyboard
        for cd in (btn.callback_data for btn in row)
    )


async def test_new_order_cancel_at_any_step(state: FSMContext, settings: Settings) -> None:
    """`no:cancel` callback clears the FSM no matter where we are."""
    await handlers.reply_new_order(_FakeMessage(), state, settings=settings)
    await handlers.cb_scenario(_FakeQuery("no:scenario:activity_like"), state, settings=settings)
    q = _FakeQuery("no:cancel")
    await handlers.cb_new_order_cancel(q, state)
    assert await state.get_state() is None
    assert "отменено" in q.message.edits[-1][0].lower()


async def test_new_order_rejects_bad_quantity(state: FSMContext, settings: Settings) -> None:
    await handlers.reply_new_order(_FakeMessage(), state, settings=settings)
    await handlers.cb_scenario(
        _FakeQuery("no:scenario:activity_subscribe"), state, settings=settings
    )
    await handlers.cb_exchange(_FakeQuery("no:exchange:smmcode"), state, settings=settings)
    await handlers.cb_service_manual(_FakeQuery("no:service_manual"), state, settings=settings)
    await handlers.fsm_service_manual(_FakeMessage("136"), state, settings=settings)
    await handlers.fsm_target(_FakeMessage("https://t.me/x"), state, settings=settings)
    # Non-numeric
    msg = _FakeMessage("not-a-number")
    await handlers.fsm_quantity(msg, state, settings=settings)
    assert "целое" in msg.answers[-1][0].lower()
    # Negative
    msg2 = _FakeMessage("-5")
    await handlers.fsm_quantity(msg2, state, settings=settings)
    assert "> 0" in msg2.answers[-1][0]
    # Still in quantity state
    assert await state.get_state() == handlers.NewOrderFSM.quantity.state


async def test_new_order_confirm_is_blocked_in_dry_run(
    state: FSMContext, settings: Settings
) -> None:
    """DRY_RUN no longer simulates order placement after removing test exchanges."""
    await handlers.reply_new_order(_FakeMessage(), state, settings=settings)
    await handlers.cb_scenario(
        _FakeQuery("no:scenario:activity_subscribe"), state, settings=settings
    )
    await handlers.cb_exchange(_FakeQuery("no:exchange:smmcode"), state, settings=settings)
    await handlers.cb_service_manual(_FakeQuery("no:service_manual"), state, settings=settings)
    await handlers.fsm_service_manual(_FakeMessage("136"), state, settings=settings)
    await handlers.fsm_target(_FakeMessage("https://t.me/x"), state, settings=settings)
    await handlers.fsm_quantity(_FakeMessage("5"), state, settings=settings)

    data = await state.get_data()
    draft_uuid = data["draft_uuid"]
    q = _FakeQuery(f"no:confirm:{draft_uuid}")
    await handlers.cb_confirm_order(q, state, settings=settings)
    assert await state.get_state() is None
    error_text = q.message.edits[-1][0]
    assert "Ошибка создания" in error_text
    assert "DRY_RUN=true" in error_text


# ---------------------------------------------------------------------------
# Non-admin gating
# ---------------------------------------------------------------------------


async def test_non_admin_query_is_rejected(state: FSMContext, settings: Settings) -> None:
    # Admin id is 42; use 99 for the impostor.
    q = _FakeQuery("no:scenario:activity_subscribe", user_id=99)
    await state.set_state(handlers.NewOrderFSM.scenario)
    await handlers.cb_scenario(q, state, settings=settings)
    # The handler answers with show_alert + "⛔" and does NOT advance the FSM.
    assert q.answers and q.answers[0][1] is True
    assert await state.get_state() == handlers.NewOrderFSM.scenario.state


# ---------------------------------------------------------------------------
# Orders list / detail flow
# ---------------------------------------------------------------------------


async def test_orders_list_empty_then_seeded(
    state: FSMContext, settings: Settings, monkeypatch
) -> None:
    """Empty list shows hint; after seeding an order we see the inline button."""
    # Empty
    msg = _FakeMessage()
    await handlers.cmd_orders(msg, object(), settings=settings)
    assert "Нет активных заказов" in msg.answers[0][0]

    now = datetime.now(UTC)
    order = Order(
        client_order_uuid="seed-order-1",
        spec=OrderSpec(
            scenario=Scenario.ACTIVITY_SUBSCRIBE,
            exchange="smmcode",
            target="https://t.me/x",
            quantity=5,
            max_cost=2.0,
        ),
        status=OrderStatus.CREATING,
        created_at=now,
        updated_at=now,
    )
    async with connect(settings) as conn:
        await insert_order_creating(conn, order)
        await mark_order_active(conn, order.client_order_uuid, "ext-seed-1", 0.5)

    msg2 = _FakeMessage()
    await handlers.cmd_orders(msg2, object(), settings=settings)
    text, kb = msg2.answers[0]
    assert "Активные заказы" in text
    # At least one tappable row exists.
    assert kb.inline_keyboard
    callback_data = [btn.callback_data for row in kb.inline_keyboard for btn in row]
    assert any(cd and cd.startswith("order:open:") for cd in callback_data)


# ---------------------------------------------------------------------------
# Dispatcher builds with no warnings
# ---------------------------------------------------------------------------


def test_dispatcher_builds(settings_factory: Any = None) -> None:
    """Smoke-build the dispatcher to surface any aiogram routing errors at
    import time (decorators evaluate to handlers; bad filters explode here).
    """
    s = Settings(
        db_path=Path("/tmp/_smoke.db"),
        dry_run=True,
        telegram_admin_ids=[42],
        telegram_bot_token="not-used",
    )
    dp = handlers.build_dispatcher(s)
    assert dp is not None
    assert dp.workflow_data.get("settings") is s


# ---------------------------------------------------------------------------
# Accept / reject decision callbacks short-circuit when admin lacks rights
# ---------------------------------------------------------------------------


async def test_accept_callback_non_admin_blocked(monkeypatch, settings: Settings) -> None:
    called = False

    async def fake_decide(*_a, **_kw):
        nonlocal called
        called = True
        return {"decision": "should-not-fire"}

    monkeypatch.setattr(handlers, "_decide_submission", fake_decide)
    q = _FakeQuery("accept:sub-x", user_id=99)  # not admin
    await handlers.cb_accept(q, settings=settings)
    assert called is False
    assert q.answers and q.answers[0][1] is True


async def test_accept_callback_admin_calls_decide(monkeypatch, settings: Settings) -> None:
    fake = AsyncMock(return_value={"decision": "accepted"})
    monkeypatch.setattr(handlers, "_decide_submission", fake)
    q = _FakeQuery("accept:sub-x", user_id=42)
    await handlers.cb_accept(q, settings=settings)
    fake.assert_awaited_once()
    assert q.message.edits and "accepted" in q.message.edits[-1][0]


# ---------------------------------------------------------------------------
# We don't need stale event loops between tests; pytest-asyncio handles it.
# A direct sanity check anyway:
# ---------------------------------------------------------------------------


def test_main_menu_keyboard_layout() -> None:
    kb = keyboards.main_menu()
    # 4 rows × 2 buttons.
    assert len(kb.keyboard) == 4
    for row in kb.keyboard:
        assert len(row) == 2


def test_scenario_choice_keyboard_has_three_choices_plus_cancel() -> None:
    kb = keyboards.scenario_choice()
    callback_data = [btn.callback_data for row in kb.inline_keyboard for btn in row]
    assert "no:scenario:activity_subscribe" in callback_data
    assert "no:scenario:activity_like" in callback_data
    assert "no:scenario:social_traffic" in callback_data
    assert "no:cancel" in callback_data


def test_source_platform_choice_has_all_six() -> None:
    kb = keyboards.source_platform_choice()
    cbs = [btn.callback_data for row in kb.inline_keyboard for btn in row]
    for p in ("vk", "x", "youtube", "telegram", "dzen", "pinterest"):
        assert f"no:platform:{p}" in cbs


# Tell pytest-asyncio to share the loop across async tests in this module so we
# can hold an FSMContext fixture without recreating the storage every test
# (the in-memory storage is per-instance).
@pytest.fixture
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()
