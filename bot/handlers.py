"""Telegram bot handlers (aiogram 3) — interactive (button-driven) UX.

Design:
- A persistent Reply keyboard surfaces the main menu under the input field
  (Новый заказ, Сводка, Заказы, Проверить, На проверку, Отчёт, Здоровье,
  Отмена). One tap = one action.
- Inline keyboards drive every FSM choice (scenario, exchange, source platform)
  and every contextual action (open order, refresh, accept / rework).
- Slash commands remain as aliases for power users — every button has a
  matching `/command` that does the same thing.
- `/cancel`, the ❌ button, and `no:cancel` callback all clear FSM state.

Money safety: `_persist_and_create` only fires after the user explicitly taps
`✅ Подтвердить и создать` on the confirmation card.
"""

from __future__ import annotations

from typing import Any

import httpx
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from bot import keyboards
from bot.keyboards import (
    BTN_CANCEL,
    BTN_CHECK,
    BTN_DASHBOARD,
    BTN_HEALTH,
    BTN_NEW_ORDER,
    BTN_ORDERS,
    BTN_REPORT,
    BTN_REVIEW,
    PANEL_EXCHANGES,
    TASK_EXCHANGES,
)
from cli import _build_adapters, _persist_and_create, build_adapter
from config import Settings
from db.database import connect, init_db
from models import (
    OrderSpec,
    Scenario,
    SourcePlatform,
    new_client_order_uuid,
)
from orchestrator import Orchestrator
from reporting.sheets import preview_weekly_rows

# ---------------------------------------------------------------------------
# Router + admin guard
# ---------------------------------------------------------------------------

router = Router()

ALL_EXCHANGES = tuple(PANEL_EXCHANGES) + tuple(TASK_EXCHANGES)


def _user_is_admin(user_id: int, settings: Settings) -> bool:
    return user_id in settings.telegram_admin_ids


def _message_is_admin(message: Message, settings: Settings) -> bool:
    return message.from_user is not None and _user_is_admin(message.from_user.id, settings)


def _query_is_admin(query: CallbackQuery, settings: Settings) -> bool:
    return _user_is_admin(query.from_user.id, settings)


def _require_admin_msg(handler):
    """Decorator: reject non-admin messages with a polite notice."""

    async def wrapper(message: Message, state: FSMContext, settings: Settings, **kwargs: Any):
        if not _message_is_admin(message, settings):
            await message.answer("⛔ Доступ только для администраторов.")
            return
        return await handler(message, state, settings, **kwargs)

    return wrapper


# ---------------------------------------------------------------------------
# FSM
# ---------------------------------------------------------------------------


class NewOrderFSM(StatesGroup):
    scenario = State()
    exchange = State()
    service = State()  # NEW — pick service_id from exchange catalogue
    service_manual = State()  # NEW — fallback: user types service_id by hand
    target = State()
    quantity = State()
    source_platform = State()  # only for SOCIAL_TRAFFIC
    confirm = State()


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------


async def on_startup(bot: Bot, settings: Settings) -> None:
    await init_db(settings)
    if not settings.telegram_bot_token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")
    if not settings.telegram_admin_ids:
        raise RuntimeError("TELEGRAM_ADMIN_IDS is empty — no admin can use the bot")


# ---------------------------------------------------------------------------
# /start — show greeting + persistent menu
# ---------------------------------------------------------------------------


WELCOME_TEXT = (
    "👋 *Бот мониторинга бирж*\n\n"
    "Создаю и веду заказы на 5 биржах накрутки и микрозадач, проверяю\n"
    "результат и помогаю с приёмом или возвратом сабмишенов.\n\n"
    "Главное меню — внизу под полем ввода. Нажимайте кнопки —\n"
    "ничего печатать не нужно."
)


@router.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext, **kwargs: Any) -> None:
    await state.clear()
    await message.answer(
        WELCOME_TEXT,
        reply_markup=keyboards.main_menu(),
        parse_mode="Markdown",
    )


@router.message(Command("menu"))
async def cmd_menu(message: Message, state: FSMContext, **kwargs: Any) -> None:
    await state.clear()
    await message.answer("Главное меню:", reply_markup=keyboards.main_menu())


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext, **kwargs: Any) -> None:
    current = await state.get_state()
    await state.clear()
    if current:
        await message.answer("✅ Действие отменено.", reply_markup=keyboards.main_menu())
    else:
        await message.answer("Нечего отменять.", reply_markup=keyboards.main_menu())


# ---------------------------------------------------------------------------
# Reply-button shortcuts — convert the persistent-menu tap into the same
# action as the slash command would do. Each handler is admin-gated.
# ---------------------------------------------------------------------------


@router.message(F.text == BTN_CANCEL)
async def reply_cancel(message: Message, state: FSMContext, **kwargs: Any) -> None:
    await cmd_cancel(message, state, **kwargs)


@router.message(F.text == BTN_NEW_ORDER)
@_require_admin_msg
async def reply_new_order(
    message: Message,
    state: FSMContext,
    settings: Settings,
    **kwargs: Any,
) -> None:
    await _start_new_order(message, state)


@router.message(F.text == BTN_DASHBOARD)
@_require_admin_msg
async def reply_dashboard(
    message: Message,
    state: FSMContext,
    settings: Settings,
    **kwargs: Any,
) -> None:
    # Tapping a top-level menu button cancels whatever FSM the user was in —
    # otherwise the next text reply could be misread as a step input.
    await state.clear()
    await _show_dashboard(message, settings)


@router.message(F.text == BTN_ORDERS)
@_require_admin_msg
async def reply_orders(
    message: Message,
    state: FSMContext,
    settings: Settings,
    **kwargs: Any,
) -> None:
    await state.clear()
    await _show_orders(message, settings)


@router.message(F.text == BTN_CHECK)
@_require_admin_msg
async def reply_check(
    message: Message,
    state: FSMContext,
    settings: Settings,
    **kwargs: Any,
) -> None:
    await state.clear()
    await _run_check(message, settings)


@router.message(F.text == BTN_REVIEW)
@_require_admin_msg
async def reply_review(
    message: Message,
    state: FSMContext,
    settings: Settings,
    **kwargs: Any,
) -> None:
    await state.clear()
    await _show_review(message, settings)


@router.message(F.text == BTN_REPORT)
@_require_admin_msg
async def reply_report(
    message: Message,
    state: FSMContext,
    settings: Settings,
    **kwargs: Any,
) -> None:
    await state.clear()
    await _show_report_preview(message, settings)


@router.message(F.text == BTN_HEALTH)
@_require_admin_msg
async def reply_health(
    message: Message,
    state: FSMContext,
    settings: Settings,
    **kwargs: Any,
) -> None:
    await state.clear()
    await _show_health(message, settings)


# ---------------------------------------------------------------------------
# /new_order — slash alias for the same flow
# ---------------------------------------------------------------------------


async def _start_new_order(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(
        "📦 *Новый заказ*\n\nШаг 1/6 — выберите *сценарий*:",
        reply_markup=keyboards.scenario_choice(),
        parse_mode="Markdown",
    )
    await state.set_state(NewOrderFSM.scenario)


@router.message(Command("new_order"))
@_require_admin_msg
async def cmd_new_order(
    message: Message,
    state: FSMContext,
    settings: Settings,
    **kwargs: Any,
) -> None:
    await _start_new_order(message, state)


# ---------------------------------------------------------------------------
# FSM step 1: scenario (inline-button driven)
# ---------------------------------------------------------------------------


@router.callback_query(F.data.startswith("no:scenario:"), NewOrderFSM.scenario)
async def cb_scenario(
    query: CallbackQuery,
    state: FSMContext,
    settings: Settings,
    **kwargs: Any,
) -> None:
    if not _query_is_admin(query, settings):
        await query.answer("⛔ Только админы.", show_alert=True)
        return
    assert query.data is not None
    value = query.data.split(":", 2)[2]
    try:
        scenario = Scenario(value)
    except ValueError:
        await query.answer("Неверный сценарий", show_alert=True)
        return
    await state.update_data(scenario=scenario)

    msg = query.message
    if msg is not None and hasattr(msg, "edit_text"):
        await msg.edit_text(
            f"📦 *Новый заказ*\n\nСценарий: *{scenario.value}*\n\nШаг 2/6 — выберите *биржу*:",
            reply_markup=keyboards.exchange_choice(scenario),
            parse_mode="Markdown",
        )
    await state.set_state(NewOrderFSM.exchange)
    await query.answer()


# ---------------------------------------------------------------------------
# FSM step 2: exchange
# ---------------------------------------------------------------------------


@router.callback_query(F.data.startswith("no:exchange:"), NewOrderFSM.exchange)
async def cb_exchange(
    query: CallbackQuery,
    state: FSMContext,
    settings: Settings,
    **kwargs: Any,
) -> None:
    if not _query_is_admin(query, settings):
        await query.answer("⛔ Только админы.", show_alert=True)
        return
    assert query.data is not None
    exchange = query.data.split(":", 2)[2]
    if exchange not in ALL_EXCHANGES:
        await query.answer("Неизвестная биржа", show_alert=True)
        return
    await state.update_data(exchange=exchange)

    data = await state.get_data()
    scenario: Scenario = data["scenario"]

    # Step 3 — pick a service from the exchange's catalogue. We try to fetch
    # the catalogue live; if the exchange has no usable list (ipgold stub,
    # API down) the user gets the manual-entry fallback only.
    msg = query.message
    if msg is None or not hasattr(msg, "edit_text"):
        await query.answer("Сообщение недоступно.", show_alert=True)
        return

    options: list[Any] = []
    fetch_error: str | None = None
    try:
        async with httpx.AsyncClient(timeout=20.0) as http_client:
            adapter = build_adapter(settings, exchange, http_client, dry_run=settings.dry_run)
            options = await adapter.list_services_for_scenario(scenario)
    except Exception as exc:
        fetch_error = f"{type(exc).__name__}: {exc}"

    header = (
        f"📦 *Новый заказ*\n\n"
        f"Сценарий: *{scenario.value}*\nБиржа: *{exchange}*\n\n"
        f"Шаг 3/6 — выберите *услугу*:"
    )
    if options:
        text = header
    elif fetch_error:
        text = (
            header
            + f"\n\n⚠️ Не удалось загрузить каталог автоматически:\n`{fetch_error[:120]}`"
            + "\n\nНажмите *«Ввести ID вручную»* — посмотрите ID услуги в личном кабинете биржи."
        )
    else:
        text = (
            header
            + "\n\n⚠️ У этой биржи нет автоподбора услуг (либо каталог пуст, либо "
            + "интеграция в режиме capability-stub).\n\n"
            + "Нажмите *«Ввести ID вручную»* — посмотрите ID услуги в личном кабинете биржи."
        )

    await msg.edit_text(
        text,
        reply_markup=keyboards.service_choice(options),
        parse_mode="Markdown",
    )
    await state.set_state(NewOrderFSM.service)
    await query.answer()


# ---------------------------------------------------------------------------
# FSM step 3: service (inline buttons from catalogue) + manual fallback
# ---------------------------------------------------------------------------


@router.callback_query(F.data.startswith("no:service:"), NewOrderFSM.service)
async def cb_service(
    query: CallbackQuery,
    state: FSMContext,
    settings: Settings,
    **kwargs: Any,
) -> None:
    if not _query_is_admin(query, settings):
        await query.answer("⛔ Только админы.", show_alert=True)
        return
    assert query.data is not None
    service_id = query.data.split(":", 2)[2]
    await state.update_data(service_id=service_id)
    msg = query.message
    if msg is not None and hasattr(msg, "edit_text"):
        await _render_target_prompt(msg, state, settings, service_id_label=service_id)
    await state.set_state(NewOrderFSM.target)
    await query.answer()


@router.callback_query(F.data == "no:service_manual", NewOrderFSM.service)
async def cb_service_manual(
    query: CallbackQuery,
    state: FSMContext,
    settings: Settings,
    **kwargs: Any,
) -> None:
    """User opts to type the service_id by hand (catalogue too big / not listed)."""
    if not _query_is_admin(query, settings):
        await query.answer("⛔ Только админы.", show_alert=True)
        return
    msg = query.message
    if msg is None or not hasattr(msg, "edit_text"):
        await query.answer("Сообщение недоступно.", show_alert=True)
        return
    await msg.edit_text(
        "📦 *Новый заказ*\n\n"
        "Введите *ID услуги* (число из каталога биржи).\n"
        "Узнать ID можно в личном кабинете биржи или через её документацию.",
        reply_markup=keyboards.cancel_only(),
        parse_mode="Markdown",
    )
    await state.set_state(NewOrderFSM.service_manual)
    await query.answer()


@router.message(NewOrderFSM.service_manual)
@_require_admin_msg
async def fsm_service_manual(
    message: Message,
    state: FSMContext,
    settings: Settings,
    **kwargs: Any,
) -> None:
    text = (message.text or "").strip()
    if text == BTN_CANCEL:
        await cmd_cancel(message, state, **kwargs)
        return
    if not text:
        await message.answer(
            "ID услуги не может быть пустым.", reply_markup=keyboards.cancel_only()
        )
        return
    # Accept anything non-empty — each adapter validates against its catalogue
    # at create-order time and raises ValueError if the id isn't real.
    await state.update_data(service_id=text)
    await message.answer(
        f"Услуга: `{text}`",
        parse_mode="Markdown",
    )
    await _render_target_prompt(message, state, settings, service_id_label=text)
    await state.set_state(NewOrderFSM.target)


async def _render_target_prompt(
    msg: Any,
    state: FSMContext,
    settings: Settings,
    *,
    service_id_label: str,
) -> None:
    """Render the «введите URL» step. Works for both Message.answer and
    CallbackQuery.message.edit_text — duck-typed.
    """
    data = await state.get_data()
    scenario: Scenario = data["scenario"]
    exchange = data["exchange"]
    prompt = (
        "Введите *целевой URL сайта* (Метрика считает переходы)."
        if scenario == Scenario.SOCIAL_TRAFFIC
        else "Введите *целевой аккаунт* или ссылку на пост."
    )
    text = (
        f"📦 *Новый заказ*\n\n"
        f"Сценарий: *{scenario.value}*\nБиржа: *{exchange}* · Услуга: `{service_id_label}`\n\n"
        f"Шаг 4/6 — {prompt}"
    )
    if hasattr(msg, "edit_text"):
        await msg.edit_text(text, reply_markup=keyboards.cancel_only(), parse_mode="Markdown")
    else:
        await msg.answer(text, reply_markup=keyboards.cancel_only(), parse_mode="Markdown")


# ---------------------------------------------------------------------------
# FSM step 4: target (free-text)
# ---------------------------------------------------------------------------


@router.message(NewOrderFSM.target)
@_require_admin_msg
async def fsm_target(
    message: Message,
    state: FSMContext,
    settings: Settings,
    **kwargs: Any,
) -> None:
    text = (message.text or "").strip()
    if not text or text == BTN_CANCEL:
        # Reply-keyboard cancel inside FSM.
        if text == BTN_CANCEL:
            await cmd_cancel(message, state, **kwargs)
            return
        await message.answer("URL/аккаунт не может быть пустым.")
        return
    await state.update_data(target=text)
    await message.answer(
        "📦 *Новый заказ*\n\nШаг 5/6 — введите *количество* (число > 0):",
        reply_markup=keyboards.cancel_only(),
        parse_mode="Markdown",
    )
    await state.set_state(NewOrderFSM.quantity)


# ---------------------------------------------------------------------------
# FSM step 4: quantity (free-text → confirm card)
# ---------------------------------------------------------------------------


@router.message(NewOrderFSM.quantity)
@_require_admin_msg
async def fsm_quantity(
    message: Message,
    state: FSMContext,
    settings: Settings,
    **kwargs: Any,
) -> None:
    text = (message.text or "").strip()
    if text == BTN_CANCEL:
        await cmd_cancel(message, state, **kwargs)
        return
    try:
        qty = int(text)
    except ValueError:
        await message.answer("Введите целое число.", reply_markup=keyboards.cancel_only())
        return
    if qty <= 0:
        await message.answer("Количество должно быть > 0.", reply_markup=keyboards.cancel_only())
        return
    await state.update_data(quantity=qty)

    data = await state.get_data()
    if data["scenario"] == Scenario.SOCIAL_TRAFFIC:
        await message.answer(
            "📦 *Новый заказ*\n\nВыберите *платформу-источник трафика*:",
            reply_markup=keyboards.source_platform_choice(),
            parse_mode="Markdown",
        )
        await state.set_state(NewOrderFSM.source_platform)
    else:
        await _ask_confirm(message, state)


# ---------------------------------------------------------------------------
# FSM step 4b (SOCIAL_TRAFFIC): source platform
# ---------------------------------------------------------------------------


@router.callback_query(F.data.startswith("no:platform:"), NewOrderFSM.source_platform)
async def cb_source_platform(
    query: CallbackQuery,
    state: FSMContext,
    settings: Settings,
    **kwargs: Any,
) -> None:
    if not _query_is_admin(query, settings):
        await query.answer("⛔ Только админы.", show_alert=True)
        return
    assert query.data is not None
    value = query.data.split(":", 2)[2]
    try:
        platform = SourcePlatform(value)
    except ValueError:
        await query.answer("Неверная платформа", show_alert=True)
        return
    await state.update_data(source_platform=platform)
    msg = query.message
    if msg is not None and hasattr(msg, "edit_text"):
        await _ask_confirm_via_edit(msg, state)
    await query.answer()


# ---------------------------------------------------------------------------
# FSM final: confirmation card
# ---------------------------------------------------------------------------


def _draft_summary(data: dict[str, Any], draft_uuid: str) -> str:
    scenario: Scenario = data["scenario"]
    lines = [
        "📦 *Новый заказ — подтверждение*",
        f"Сценарий: *{scenario.value}*",
        f"Биржа: *{data['exchange']}*",
    ]
    if (sid := data.get("service_id")) is not None:
        lines.append(f"Услуга (ID): `{sid}`")
    lines.append(f"Цель: `{data['target']}`")
    lines.append(f"Количество: *{data['quantity']}*")
    if (sp := data.get("source_platform")) is not None:
        lines.append(f"Платформа: *{sp.value}*")
    lines.append(f"\n🆔 Черновик: `{draft_uuid[:8]}…`")
    lines.append("\nНажмите *✅ Подтвердить* — заказ уйдёт на биржу.")
    return "\n".join(lines)


async def _ask_confirm(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    draft_uuid = new_client_order_uuid()
    await state.update_data(draft_uuid=draft_uuid)
    await message.answer(
        _draft_summary(data, draft_uuid),
        reply_markup=keyboards.confirm_order(draft_uuid),
        parse_mode="Markdown",
    )
    await state.set_state(NewOrderFSM.confirm)


async def _ask_confirm_via_edit(message, state: FSMContext) -> None:
    """Same as _ask_confirm but edits the previous inline message (preserves
    the conversation thread cleanly when the user came from a button)."""
    data = await state.get_data()
    draft_uuid = new_client_order_uuid()
    await state.update_data(draft_uuid=draft_uuid)
    await message.edit_text(
        _draft_summary(data, draft_uuid),
        reply_markup=keyboards.confirm_order(draft_uuid),
        parse_mode="Markdown",
    )
    await state.set_state(NewOrderFSM.confirm)


async def _create_order_from_state(
    data: dict[str, Any],
    settings: Settings,
    actor: str,
) -> tuple[str, str, float]:
    if settings.dry_run:
        raise RuntimeError(
            "DRY_RUN=true: создание заказов отключено после удаления симуляторов. "
            "Переключите DRY_RUN=false и заполните реальные реквизиты бирж."
        )
    spec = OrderSpec(
        scenario=data["scenario"],
        exchange=data["exchange"],
        target=data["target"],
        quantity=data["quantity"],
        service_id=data.get("service_id"),
        source_platform=data.get("source_platform"),
        max_cost=settings.per_order_spend_limit,
    )
    async with httpx.AsyncClient() as http_client:
        adapter = build_adapter(settings, spec.exchange, http_client, dry_run=settings.dry_run)
        return await _persist_and_create(settings, adapter, spec, actor=actor)


@router.callback_query(F.data.startswith("no:confirm:"), NewOrderFSM.confirm)
async def cb_confirm_order(
    query: CallbackQuery,
    state: FSMContext,
    settings: Settings,
    **kwargs: Any,
) -> None:
    if not _query_is_admin(query, settings):
        await query.answer("⛔ Только админы.", show_alert=True)
        return
    assert query.data is not None
    draft_uuid = query.data.split(":", 2)[2]
    data = await state.get_data()
    if data.get("draft_uuid") != draft_uuid:
        await query.answer("Устаревший черновик.", show_alert=True)
        return

    msg = query.message
    if msg is None or not hasattr(msg, "edit_text"):
        await query.answer("Сообщение недоступно.", show_alert=True)
        return

    try:
        client_uuid, external_id, cost = await _create_order_from_state(
            data, settings, actor=f"tg:{query.from_user.id}"
        )
    except Exception as exc:
        await msg.edit_text(f"❌ Ошибка создания: `{exc}`", parse_mode="Markdown")
        await state.clear()
        await query.answer("Ошибка", show_alert=True)
        return

    await msg.edit_text(
        f"✅ *Заказ создан*\n"
        f"UUID: `{client_uuid}`\n"
        f"Биржа ID: `{external_id}`\n"
        f"Стоимость: *{cost:.2f}*",
        parse_mode="Markdown",
    )
    await state.clear()
    await query.answer("Готово")


@router.callback_query(F.data == "no:cancel")
async def cb_new_order_cancel(query: CallbackQuery, state: FSMContext, **kwargs: Any) -> None:
    await state.clear()
    msg = query.message
    if msg is not None and hasattr(msg, "edit_text"):
        await msg.edit_text("❌ Создание заказа отменено.")
    await query.answer()


# ---------------------------------------------------------------------------
# Submission review callbacks (accept / rework)
# ---------------------------------------------------------------------------


async def _decide_submission(
    settings: Settings,
    submission_uuid: str,
    *,
    actor: str,
    accept: bool,
    reason: str | None = None,
) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as http_client:
        adapters = _build_adapters(
            settings,
            dry_run=settings.dry_run,
            http_client=http_client,
        )
        orch = Orchestrator(settings, adapters)
        if accept:
            return await orch.admin_accept_submission(submission_uuid, actor=actor)
        return await orch.admin_reject_submission(
            submission_uuid,
            actor=actor,
            reason=reason or "Возвращено администратором на доработку",
        )


@router.callback_query(F.data.startswith("accept:"))
async def cb_accept(query: CallbackQuery, settings: Settings, **kwargs: Any) -> None:
    if not _query_is_admin(query, settings):
        await query.answer("⛔ Только админы.", show_alert=True)
        return
    assert query.data is not None
    sub_uuid = query.data.split(":", 1)[1]
    decision = await _decide_submission(
        settings, sub_uuid, actor=f"tg:{query.from_user.id}", accept=True
    )
    msg = query.message
    if msg is None or not hasattr(msg, "edit_text"):
        await query.answer("Сообщение недоступно.", show_alert=True)
        return
    await msg.edit_text(
        f"✅ Решение: *{decision.get('decision', decision.get('status'))}*",
        parse_mode="Markdown",
    )
    await query.answer("Готово")


@router.callback_query(F.data.startswith("reject:"))
async def cb_reject(query: CallbackQuery, settings: Settings, **kwargs: Any) -> None:
    if not _query_is_admin(query, settings):
        await query.answer("⛔ Только админы.", show_alert=True)
        return
    assert query.data is not None
    sub_uuid = query.data.split(":", 1)[1]
    decision = await _decide_submission(
        settings, sub_uuid, actor=f"tg:{query.from_user.id}", accept=False
    )
    msg = query.message
    if msg is None or not hasattr(msg, "edit_text"):
        await query.answer("Сообщение недоступно.", show_alert=True)
        return
    await msg.edit_text(
        f"❌ Решение: *{decision.get('decision', decision.get('status'))}*",
        parse_mode="Markdown",
    )
    await query.answer("Готово")


# ---------------------------------------------------------------------------
# Dashboard / Orders / Order detail / Check / Review / Report / Health
# ---------------------------------------------------------------------------


async def _show_dashboard(message: Message, settings: Settings) -> None:
    async with connect(settings) as conn:
        cursor = await conn.execute("SELECT status, COUNT(*) AS c FROM orders GROUP BY status")
        order_rows = await cursor.fetchall()
        cursor = await conn.execute("SELECT status, COUNT(*) AS c FROM submissions GROUP BY status")
        sub_rows = await cursor.fetchall()
        cursor = await conn.execute(
            "SELECT COALESCE(SUM(cost_actual), 0) AS total FROM orders "
            "WHERE date(created_at) = date('now')"
        )
        spend = await cursor.fetchone()

    order_text = ", ".join(f"{r['status']}: {r['c']}" for r in order_rows) or "нет"
    sub_text = ", ".join(f"{r['status']}: {r['c']}" for r in sub_rows) or "нет"
    await message.answer(
        "📊 *Сводка*\n"
        f"Режим: *{'DRY_RUN' if settings.dry_run else 'LIVE'}*\n"
        f"Заказы: {order_text}\n"
        f"Сабмишены: {sub_text}\n"
        f"Расход сегодня: *{float(spend['total'] if spend else 0):.2f}*\n"
        f"Лимит/день: {settings.daily_spend_limit:.2f}",
        parse_mode="Markdown",
    )


@router.message(Command("dashboard"))
@_require_admin_msg
async def cmd_dashboard(
    message: Message,
    _state: FSMContext,
    settings: Settings,
    **kwargs: Any,
) -> None:
    await _show_dashboard(message, settings)


async def _show_orders(message: Message, settings: Settings) -> None:
    async with connect(settings) as conn:
        cursor = await conn.execute(
            "SELECT client_order_uuid, status, exchange, scenario, target, quantity "
            "FROM orders WHERE status IN ('creating','active','verifying') "
            "ORDER BY created_at DESC LIMIT 20"
        )
        rows = await cursor.fetchall()
    if not rows:
        await message.answer(
            "Нет активных заказов.\nСоздать первый — нажмите 📦 *Новый заказ*.",
            parse_mode="Markdown",
        )
        return
    orders = [dict(r) for r in rows]
    summary = "📋 *Активные заказы* — нажмите для деталей:"
    await message.answer(summary, reply_markup=keyboards.orders_list(orders), parse_mode="Markdown")


@router.message(Command("orders"))
@_require_admin_msg
async def cmd_orders(
    message: Message,
    _state: FSMContext,
    settings: Settings,
    **kwargs: Any,
) -> None:
    await _show_orders(message, settings)


@router.callback_query(F.data.startswith("order:open:"))
async def cb_order_open(
    query: CallbackQuery,
    settings: Settings,
    **kwargs: Any,
) -> None:
    if not _query_is_admin(query, settings):
        await query.answer("⛔ Только админы.", show_alert=True)
        return
    assert query.data is not None
    order_uuid = query.data.split(":", 2)[2]
    text = await _format_order_detail(settings, order_uuid)
    msg = query.message
    if msg is None or not hasattr(msg, "edit_text"):
        await query.answer("Сообщение недоступно.", show_alert=True)
        return
    await msg.edit_text(
        text, reply_markup=keyboards.order_detail(order_uuid), parse_mode="Markdown"
    )
    await query.answer()


@router.callback_query(F.data.startswith("order:refresh:"))
async def cb_order_refresh(
    query: CallbackQuery,
    settings: Settings,
    **kwargs: Any,
) -> None:
    if not _query_is_admin(query, settings):
        await query.answer("⛔ Только админы.", show_alert=True)
        return
    assert query.data is not None
    order_uuid = query.data.split(":", 2)[2]
    # One-shot poll of this single order via the orchestrator.
    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as http_client:
        adapters = _build_adapters(settings, dry_run=settings.dry_run, http_client=http_client)
        orch = Orchestrator(settings, adapters)
        await orch.verify_single_order(order_uuid)
    text = await _format_order_detail(settings, order_uuid)
    msg = query.message
    if msg is None or not hasattr(msg, "edit_text"):
        await query.answer("Сообщение недоступно.", show_alert=True)
        return
    await msg.edit_text(
        text, reply_markup=keyboards.order_detail(order_uuid), parse_mode="Markdown"
    )
    await query.answer("Обновлено")


@router.callback_query(F.data == "order:back")
async def cb_order_back(
    query: CallbackQuery,
    settings: Settings,
    **kwargs: Any,
) -> None:
    if not _query_is_admin(query, settings):
        await query.answer("⛔ Только админы.", show_alert=True)
        return
    async with connect(settings) as conn:
        cursor = await conn.execute(
            "SELECT client_order_uuid, status, exchange, scenario, target, quantity "
            "FROM orders WHERE status IN ('creating','active','verifying') "
            "ORDER BY created_at DESC LIMIT 20"
        )
        rows = await cursor.fetchall()
    orders = [dict(r) for r in rows]
    msg = query.message
    if msg is None or not hasattr(msg, "edit_text"):
        await query.answer("Сообщение недоступно.", show_alert=True)
        return
    if not orders:
        await msg.edit_text("Нет активных заказов.")
    else:
        await msg.edit_text(
            "📋 *Активные заказы* — нажмите для деталей:",
            reply_markup=keyboards.orders_list(orders),
            parse_mode="Markdown",
        )
    await query.answer()


async def _format_order_detail(settings: Settings, order_uuid: str) -> str:
    async with connect(settings) as conn:
        cursor = await conn.execute(
            "SELECT * FROM orders WHERE client_order_uuid = ?", (order_uuid,)
        )
        order = await cursor.fetchone()
        if order is None:
            return "❌ Заказ не найден."
        cursor = await conn.execute(
            "SELECT status, COUNT(*) AS c FROM submissions WHERE order_uuid = ? GROUP BY status",
            (order_uuid,),
        )
        sub_rows = await cursor.fetchall()
    sub_summary = ", ".join(f"{r['status']}: {r['c']}" for r in sub_rows) or "нет"
    lines = [
        f"📋 *Заказ* `{order['client_order_uuid']}`",
        f"Статус: *{order['status']}*",
        f"Биржа: `{order['exchange']}` · Сценарий: `{order['scenario']}`",
        f"Цель: `{order['target']}`",
        f"Количество: *{order['quantity']}* · Стоимость: *{order['cost_actual']}*",
    ]
    if order["external_order_id"]:
        lines.append(f"Биржа ID: `{order['external_order_id']}`")
    if order["source_platform"]:
        lines.append(f"Платформа: `{order['source_platform']}`")
    lines.append(f"Сабмишены: {sub_summary}")
    return "\n".join(lines)


@router.message(Command("order"))
@_require_admin_msg
async def cmd_order(
    message: Message,
    _state: FSMContext,
    settings: Settings,
    **kwargs: Any,
) -> None:
    parts = (message.text or "").split()
    if len(parts) < 2:
        await message.answer(
            "Использование: /order <uuid>. Или нажмите 📋 *Заказы*.", parse_mode="Markdown"
        )
        return
    uuid_query = parts[1]
    async with connect(settings) as conn:
        cursor = await conn.execute(
            "SELECT client_order_uuid FROM orders "
            "WHERE client_order_uuid = ? OR client_order_uuid LIKE ?",
            (uuid_query, f"{uuid_query}%"),
        )
        row = await cursor.fetchone()
    if row is None:
        await message.answer("Заказ не найден.")
        return
    text = await _format_order_detail(settings, row["client_order_uuid"])
    await message.answer(
        text,
        reply_markup=keyboards.order_detail(row["client_order_uuid"]),
        parse_mode="Markdown",
    )


async def _run_check(message: Message, settings: Settings) -> None:
    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as http_client:
        adapters = _build_adapters(settings, dry_run=settings.dry_run, http_client=http_client)
        results = await Orchestrator(settings, adapters).poll_all()
    if not results:
        await message.answer("Нет активных заказов для проверки.")
        return
    lines = ["🔎 *Проверка завершена:*"]
    for result in results[:20]:
        lines.append(
            f"`{result['order_uuid'][:8]}` · {result.get('status')} · {result.get('exchange', '')}"
        )
    await message.answer("\n".join(lines), parse_mode="Markdown")


@router.message(Command("check"))
@_require_admin_msg
async def cmd_check(
    message: Message,
    _state: FSMContext,
    settings: Settings,
    **kwargs: Any,
) -> None:
    await _run_check(message, settings)


async def _show_review(message: Message, settings: Settings) -> None:
    async with connect(settings) as conn:
        cursor = await conn.execute(
            "SELECT s.submission_uuid, s.external_submission_id, s.executor_hint, "
            "o.exchange, o.scenario, o.target, o.quantity "
            "FROM submissions s JOIN orders o ON o.client_order_uuid = s.order_uuid "
            "WHERE s.status = 'awaiting_admin' "
            "ORDER BY s.created_at ASC LIMIT 10"
        )
        rows = await cursor.fetchall()
    if not rows:
        await message.answer("Нет сабмишенов на ручное решение.")
        return
    for row in rows:
        await message.answer(
            "🧾 *На проверку*\n"
            f"Submission: `{row['submission_uuid']}`\n"
            f"External: `{row['external_submission_id']}`\n"
            f"Биржа: *{row['exchange']}* | {row['scenario']}\n"
            f"Количество: *{row['quantity']}*\n"
            f"Исполнитель: {row['executor_hint'] or '—'}\n"
            f"Цель: `{row['target']}`",
            reply_markup=keyboards.accept_rework(row["submission_uuid"]),
            parse_mode="Markdown",
        )


@router.message(Command("review"))
@_require_admin_msg
async def cmd_review(
    message: Message,
    _state: FSMContext,
    settings: Settings,
    **kwargs: Any,
) -> None:
    await _show_review(message, settings)


async def _show_report_preview(message: Message, settings: Settings) -> None:
    rows = await preview_weekly_rows(settings)
    if not rows:
        await message.answer("За текущую неделю пока нет строк отчёта.")
        return
    lines = ["📄 *Отчёт за неделю:*"]
    for row in rows[:20]:
        lines.append(
            f"{row['source_platform']} · {row['exchange']} · "
            f"{row['ordered_count']} заказано · {row['actual_count']} факт · "
            f"{row['cost']:.2f} · {row['status']}"
        )
    await message.answer("\n".join(lines), parse_mode="Markdown")


@router.message(Command("report_preview"))
@_require_admin_msg
async def cmd_report_preview(
    message: Message,
    _state: FSMContext,
    settings: Settings,
    **kwargs: Any,
) -> None:
    await _show_report_preview(message, settings)


async def _show_health(message: Message, settings: Settings) -> None:
    checks = [
        ("Telegram admins", bool(settings.telegram_admin_ids)),
        ("Metrica", bool(settings.metrica_counter_id and settings.metrica_oauth_token)),
        ("Google Sheets", bool(settings.google_sheets_spreadsheet_id)),
        ("smmcode", bool(settings.smmcode_api_key) or settings.dry_run),
        ("prskill", bool(settings.prskill_api_key) or settings.dry_run),
        ("unu", bool(settings.unu_api_key) or settings.dry_run),
        ("advego", bool(settings.advego_api_token) or settings.dry_run),
        ("ipgold", bool(settings.ipgold_api_key) or settings.dry_run),
    ]
    lines = ["🩺 *Здоровье интеграций*"]
    for name, ok in checks:
        lines.append(f"{'✅' if ok else '⚠️'} {name}")
    lines.append(f"\nРежим: *{'DRY_RUN' if settings.dry_run else 'LIVE'}*")
    await message.answer("\n".join(lines), parse_mode="Markdown")


@router.message(Command("health"))
@_require_admin_msg
async def cmd_health(
    message: Message,
    _state: FSMContext,
    settings: Settings,
    **kwargs: Any,
) -> None:
    await _show_health(message, settings)


# ---------------------------------------------------------------------------
# Slash-command aliases for accept/reject (keep for power users)
# ---------------------------------------------------------------------------


@router.message(Command("accept"))
@_require_admin_msg
async def cmd_accept_submission(
    message: Message,
    _state: FSMContext,
    settings: Settings,
    **kwargs: Any,
) -> None:
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) != 2:
        await message.answer(
            "Формат: /accept <submission_uuid>. Удобнее — кнопкой ✅ в карточке (🧾 На проверку)."
        )
        return
    actor = f"tg:{message.from_user.id}" if message.from_user else "tg"
    decision = await _decide_submission(settings, parts[1].strip(), actor=actor, accept=True)
    await message.answer(
        f"✅ Решение: *{decision.get('decision', decision.get('status'))}*",
        parse_mode="Markdown",
    )


@router.message(Command("reject"))
@_require_admin_msg
async def cmd_reject_submission(
    message: Message,
    _state: FSMContext,
    settings: Settings,
    **kwargs: Any,
) -> None:
    parts = (message.text or "").split(maxsplit=2)
    if len(parts) < 2:
        await message.answer(
            "Формат: /reject <submission_uuid> [reason]. Удобнее — кнопкой ❌ в карточке."
        )
        return
    actor = f"tg:{message.from_user.id}" if message.from_user else "tg"
    decision = await _decide_submission(
        settings,
        parts[1].strip(),
        actor=actor,
        accept=False,
        reason=parts[2].strip() if len(parts) == 3 else None,
    )
    await message.answer(
        f"❌ Решение: *{decision.get('decision', decision.get('status'))}*",
        parse_mode="Markdown",
    )


# ---------------------------------------------------------------------------
# Catch-all callback for ignored taps (e.g. "(нет активных заказов)")
# ---------------------------------------------------------------------------


@router.callback_query(F.data == "noop")
async def cb_noop(query: CallbackQuery, **kwargs: Any) -> None:
    await query.answer()


# ---------------------------------------------------------------------------
# Stale callback fallback: a tap on an old inline button (no FSM state, or
# wrong state) used to be silently ignored by aiogram. Now we surface a clear
# "session expired" toast so the user understands why nothing happened.
# ---------------------------------------------------------------------------


@router.callback_query(F.data.startswith("no:"))
async def cb_new_order_stale(query: CallbackQuery, **kwargs: Any) -> None:
    """Last-resort handler for any `no:*` callback that didn't match a state-
    filtered handler above. Means the inline message is from a previous
    session that has since been cleared.
    """
    await query.answer(
        "⏳ Эта карточка устарела — нажмите 📦 Новый заказ ещё раз.",
        show_alert=True,
    )


# ---------------------------------------------------------------------------
# Dispatcher builder
# ---------------------------------------------------------------------------


def build_dispatcher(settings: Settings) -> Dispatcher:
    dp = Dispatcher()
    dp["settings"] = settings
    dp.include_router(router)
    dp.startup.register(on_startup)
    return dp
