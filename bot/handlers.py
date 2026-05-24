"""Telegram bot handlers (aiogram 3).

- Whitelist of admins via `settings.telegram_admin_ids`.
- `/new_order` — minimal flow: choose scenario → exchange → target → quantity → confirm.
- `/orders` — list active orders.
- `/order <uuid>` — show one order with inline actions.
- Callbacks for confirm/cancel and admin accept/rework.

Money safety: confirmation is required before `_persist_and_create`; the order row
is written in `CREATING` state only after the user presses the inline confirm button.
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
from cli import _persist_and_create, build_adapter
from config import Settings
from db.database import (
    append_audit,
    connect,
    init_db,
)
from models import (
    OrderSpec,
    Scenario,
    SourcePlatform,
    new_client_order_uuid,
)

# ---------------------------------------------------------------------------
# Router + filters
# ---------------------------------------------------------------------------

router = Router()

PANEL_EXCHANGES = {"smmcode", "prskill", "fake_panel"}
TASK_EXCHANGES = {"unu", "advego", "ipgold", "fake_task_exchange"}
ALL_EXCHANGES = sorted(PANEL_EXCHANGES | TASK_EXCHANGES)


def _is_admin(message: Message, settings: Settings) -> bool:
    return message.from_user is not None and message.from_user.id in settings.telegram_admin_ids


def _require_admin(handler):
    async def wrapper(message: Message, state: FSMContext, settings: Settings, **kwargs: Any):
        if not _is_admin(message, settings):
            await message.answer("⛔ Доступ только для администраторов.")
            return
        return await handler(message, state, settings, **kwargs)

    return wrapper


# ---------------------------------------------------------------------------
# FSM for /new_order
# ---------------------------------------------------------------------------


class NewOrderFSM(StatesGroup):
    scenario = State()
    exchange = State()
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
# Commands
# ---------------------------------------------------------------------------


@router.message(Command("start"))
async def cmd_start(message: Message) -> None:
    await message.answer(
        "Бот мониторинга заказов бирж.\n"
        "Команды:\n"
        "/new_order — создать заказ\n"
        "/orders — список активных заказов\n"
        "/order <uuid> — детали заказа"
    )


@router.message(Command("new_order"))
@_require_admin
async def cmd_new_order(message: Message, state: FSMContext, settings: Settings) -> None:
    kb = keyboards.simple_cancel("new_order", "scenario")
    await message.answer(
        "Выберите сценарий:\n1 — Подписки / лайки (activity)\n2 — Переходы на сайт (traffic)",
        reply_markup=kb,
    )
    await state.set_state(NewOrderFSM.scenario)


@router.message(NewOrderFSM.scenario)
@_require_admin
async def fsm_scenario(message: Message, state: FSMContext, settings: Settings) -> None:
    text = (message.text or "").strip()
    mapping = {"1": Scenario.ACTIVITY_LIKE, "2": Scenario.SOCIAL_TRAFFIC}
    if text not in mapping:
        await message.answer("Введите 1 или 2.")
        return
    await state.update_data(scenario=mapping[text])
    await message.answer(
        f"Сценарий: {mapping[text].value}\nВыберите биржу: {', '.join(ALL_EXCHANGES)}"
    )
    await state.set_state(NewOrderFSM.exchange)


@router.message(NewOrderFSM.exchange)
@_require_admin
async def fsm_exchange(message: Message, state: FSMContext, settings: Settings) -> None:
    text = (message.text or "").strip().lower()
    if text not in ALL_EXCHANGES:
        await message.answer(f"Неизвестная биржа. Доступны: {', '.join(ALL_EXCHANGES)}")
        return
    await state.update_data(exchange=text)

    data = await state.get_data()
    scenario = data["scenario"]

    if scenario == Scenario.SOCIAL_TRAFFIC:
        platforms_text = ", ".join(p.value for p in SourcePlatform)
        await message.answer(
            f"Биржа: {text}\nПлатформы: {platforms_text}\nВведите целевой URL сайта"
        )
    else:
        await message.answer(f"Биржа: {text}\nВведите целевой аккаунт / пост")
    await state.set_state(NewOrderFSM.target)


@router.message(NewOrderFSM.target)
@_require_admin
async def fsm_target(message: Message, state: FSMContext, settings: Settings) -> None:
    text = (message.text or "").strip()
    if not text:
        await message.answer("URL/аккаунт не может быть пустым.")
        return
    await state.update_data(target=text)
    await message.answer("Введите количество (число > 0):")
    await state.set_state(NewOrderFSM.quantity)


@router.message(NewOrderFSM.quantity)
@_require_admin
async def fsm_quantity(message: Message, state: FSMContext, settings: Settings) -> None:
    text = (message.text or "").strip()
    try:
        qty = int(text)
    except ValueError:
        await message.answer("Введите целое число.")
        return
    if qty <= 0:
        await message.answer("Количество должно быть > 0.")
        return
    await state.update_data(quantity=qty)

    data = await state.get_data()
    if data["scenario"] == Scenario.SOCIAL_TRAFFIC:
        platforms = ", ".join(p.value for p in SourcePlatform)
        await message.answer(f"Введите платформу-источник: {platforms}")
        await state.set_state(NewOrderFSM.source_platform)
    else:
        await _ask_confirm(message, state, settings)


@router.message(NewOrderFSM.source_platform)
@_require_admin
async def fsm_source_platform(message: Message, state: FSMContext, settings: Settings) -> None:
    text = (message.text or "").strip().lower()
    try:
        platform = SourcePlatform(text)
    except ValueError:
        await message.answer(
            f"Неверная платформа. Доступны: {', '.join(p.value for p in SourcePlatform)}"
        )
        return
    await state.update_data(source_platform=platform)
    await _ask_confirm(message, state, settings)


async def _ask_confirm(message: Message, state: FSMContext, settings: Settings) -> None:
    data = await state.get_data()
    scenario = data["scenario"]
    exchange = data["exchange"]
    target = data["target"]
    quantity = data["quantity"]
    source_platform = data.get("source_platform")

    # Generate a draft UUID for the confirmation keyboard
    draft_uuid = new_client_order_uuid()
    await state.update_data(draft_uuid=draft_uuid)

    lines = [
        "📋 Новый заказ (черновик)",
        f"Сценарий: {scenario.value}",
        f"Биржа: {exchange}",
        f"Цель: {target}",
        f"Количество: {quantity}",
    ]
    if source_platform:
        lines.append(f"Платформа: {source_platform.value}")
    lines.append("\nНажмите Подтвердить для создания.")

    await message.answer(
        "\n".join(lines),
        reply_markup=keyboards.confirm_order(draft_uuid),
    )
    await state.set_state(NewOrderFSM.confirm)


# ---------------------------------------------------------------------------
# Callbacks
# ---------------------------------------------------------------------------


@router.callback_query(F.data.startswith("confirm_order:"))
async def cb_confirm_order(query: CallbackQuery, state: FSMContext, settings: Settings) -> None:
    if query.data is None:
        return
    if query.from_user.id not in settings.telegram_admin_ids:
        await query.answer("⛔ Только админы.", show_alert=True)
        return

    draft_uuid = query.data.split(":", 1)[1]
    data = await state.get_data()
    if data.get("draft_uuid") != draft_uuid:
        await query.answer("Устаревший черновик.", show_alert=True)
        return

    scenario = data["scenario"]
    exchange = data["exchange"]
    target = data["target"]
    quantity = data["quantity"]
    source_platform = data.get("source_platform")

    spec = OrderSpec(
        scenario=scenario,
        exchange=exchange,
        target=target,
        quantity=quantity,
        source_platform=source_platform,
        max_cost=settings.per_order_spend_limit,
    )

    # Use a single HTTP client for the adapter lifetime of this call
    async with httpx.AsyncClient() as http_client:
        adapter = build_adapter(settings, exchange, http_client, dry_run=settings.dry_run)
        try:
            client_uuid, external_id, cost = await _persist_and_create(
                settings, adapter, spec, actor=f"tg:{query.from_user.id}"
            )
        except Exception as exc:
            msg = query.message

            if msg is None or not hasattr(msg, "edit_text"):
                await query.answer("Сообщение недоступно.", show_alert=True)

                return

            await msg.edit_text(f"❌ Ошибка создания: {exc}")
            await state.clear()
            return

    msg = query.message

    if msg is None or not hasattr(msg, "edit_text"):
        await query.answer("Сообщение недоступно.", show_alert=True)

        return

    await msg.edit_text(
        f"✅ Заказ создан\nUUID: `{client_uuid}`\nБиржа ID: `{external_id}`\nСтоимость: {cost:.2f}"
    )
    await state.clear()


@router.callback_query(F.data.startswith("cancel_order:"))
async def cb_cancel_order(query: CallbackQuery, state: FSMContext) -> None:
    if query.data is None:
        return
    msg = query.message

    if msg is None or not hasattr(msg, "edit_text"):
        await query.answer("Сообщение недоступно.", show_alert=True)

        return

    await msg.edit_text("❌ Создание отменено.")
    await state.clear()


@router.callback_query(F.data.startswith("accept:"))
async def cb_accept(query: CallbackQuery, settings: Settings) -> None:
    if query.data is None:
        return
    if query.from_user.id not in settings.telegram_admin_ids:
        await query.answer("⛔ Только админы.", show_alert=True)
        return
    sub_uuid = query.data.split(":", 1)[1]
    # Placeholder: orchestrator wires real accept on Day 3
    msg = query.message

    if msg is None or not hasattr(msg, "edit_text"):
        await query.answer("Сообщение недоступно.", show_alert=True)

        return

    await msg.edit_text(f"✅ Принято (submission {sub_uuid[:8]}…).")
    async with connect(settings) as conn:
        await append_audit(
            conn,
            actor=f"tg:{query.from_user.id}",
            event="admin_accept",
            submission_uuid=sub_uuid,
        )


@router.callback_query(F.data.startswith("reject:"))
async def cb_reject(query: CallbackQuery, settings: Settings) -> None:
    if query.data is None:
        return
    if query.from_user.id not in settings.telegram_admin_ids:
        await query.answer("⛔ Только админы.", show_alert=True)
        return
    sub_uuid = query.data.split(":", 1)[1]
    msg = query.message

    if msg is None or not hasattr(msg, "edit_text"):
        await query.answer("Сообщение недоступно.", show_alert=True)

        return

    await msg.edit_text(f"❌ Возвращено на доработку (submission {sub_uuid[:8]}…).")
    async with connect(settings) as conn:
        await append_audit(
            conn,
            actor=f"tg:{query.from_user.id}",
            event="admin_reject",
            submission_uuid=sub_uuid,
        )


# ---------------------------------------------------------------------------
# /orders  /order
# ---------------------------------------------------------------------------


@router.message(Command("orders"))
@_require_admin
async def cmd_orders(message: Message, settings: Settings) -> None:
    async with connect(settings) as conn:
        cursor = await conn.execute(
            "SELECT client_order_uuid, status, exchange, scenario, target, quantity "
            "FROM orders WHERE status IN ('creating','active','verifying') "
            "ORDER BY created_at DESC LIMIT 20"
        )
        rows = await cursor.fetchall()
    if not rows:
        await message.answer("Нет активных заказов.")
        return
    lines = ["📦 Активные заказы:"]
    for r in rows:
        lines.append(
            f"`{r['client_order_uuid'][:8]}` | {r['status']} | {r['exchange']} | "
            f"{r['scenario']} | {r['quantity']} | {r['target'][:40]}"
        )
    await message.answer("\n".join(lines))


@router.message(Command("order"))
@_require_admin
async def cmd_order(message: Message, settings: Settings) -> None:
    parts = (message.text or "").split()
    if len(parts) < 2:
        await message.answer("Использование: /order <uuid>")
        return
    uuid_query = parts[1]
    async with connect(settings) as conn:
        # Exact or prefix match
        cursor = await conn.execute(
            "SELECT * FROM orders WHERE client_order_uuid = ? OR client_order_uuid LIKE ?",
            (uuid_query, f"{uuid_query}%"),
        )
        row = await cursor.fetchone()
    if row is None:
        await message.answer("Заказ не найден.")
        return
    lines = [
        f"📋 Заказ `{row['client_order_uuid']}`",
        f"Статус: {row['status']}",
        f"Биржа: {row['exchange']} | Сценарий: {row['scenario']}",
        f"Цель: {row['target']}",
        f"Количество: {row['quantity']} | Стоимость: {row['cost_actual']}",
    ]
    await message.answer("\n".join(lines))


# ---------------------------------------------------------------------------
# Dispatcher builder (used by main.py)
# ---------------------------------------------------------------------------


def build_dispatcher(settings: Settings) -> Dispatcher:
    dp = Dispatcher()
    dp["settings"] = settings
    dp.include_router(router)
    dp.startup.register(on_startup)
    return dp
