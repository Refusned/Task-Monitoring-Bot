"""Inline + reply keyboards for the Telegram bot.

The bot UX is button-driven: a persistent Reply keyboard surfaces the top-level
menu under the input field, and inline keyboards appear inside messages for
contextual actions (scenario choice, exchange choice, order details, accept /
rework).

Callback-data conventions:
- `menu:<action>`            — top-level menu shortcuts when needed inside messages
- `no:scenario:<value>`      — "new order" FSM step: scenario chosen
- `no:exchange:<value>`      — exchange chosen
- `no:platform:<value>`      — source platform chosen (SOCIAL_TRAFFIC only)
- `no:confirm:<uuid>`        — confirm a draft order
- `no:cancel`                — cancel the current FSM
- `accept:<sub_uuid>`        — accept a submission (admin)
- `reject:<sub_uuid>`        — reject a submission (admin)
- `order:open:<uuid>`        — show order detail
- `order:refresh:<uuid>`     — re-poll one order
- `order:back`               — return to orders list
"""

from __future__ import annotations

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)

from models import Scenario, SourcePlatform

# Labels for the persistent Reply keyboard.
BTN_NEW_ORDER = "📦 Новый заказ"
BTN_BALANCE = "💰 Баланс"
BTN_DASHBOARD = "📊 Сводка"
BTN_ORDERS = "📋 Заказы"
BTN_CHECK = "🔎 Проверить"
BTN_REVIEW = "🧾 На проверку"
BTN_REPORT = "📄 Отчёт"
BTN_HEALTH = "🩺 Здоровье"
BTN_CANCEL = "❌ Отмена"

# Display names for real exchanges (mirrors the orchestrator routing).
PANEL_EXCHANGES = ("smmcode", "prskill")
TASK_EXCHANGES = ("unu", "advego", "ipgold")


def main_menu() -> ReplyKeyboardMarkup:
    """Persistent menu always visible under the input field.

    Layout — money-relevant actions (Новый заказ, Баланс) are on the top row
    where they're easiest to reach with the thumb.
    """
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_NEW_ORDER), KeyboardButton(text=BTN_BALANCE)],
            [KeyboardButton(text=BTN_DASHBOARD), KeyboardButton(text=BTN_ORDERS)],
            [KeyboardButton(text=BTN_CHECK), KeyboardButton(text=BTN_REVIEW)],
            [KeyboardButton(text=BTN_REPORT), KeyboardButton(text=BTN_HEALTH)],
            [KeyboardButton(text=BTN_CANCEL)],
        ],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Выберите действие или введите команду",
    )


def remove_keyboard() -> ReplyKeyboardRemove:
    return ReplyKeyboardRemove()


# ---------------------------------------------------------------------------
# New-order FSM keyboards
# ---------------------------------------------------------------------------


def scenario_choice() -> InlineKeyboardMarkup:
    """Step 1: choose the scenario (activity vs traffic)."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🔔 Подписки",
                    callback_data=f"no:scenario:{Scenario.ACTIVITY_SUBSCRIBE.value}",
                ),
                InlineKeyboardButton(
                    text="❤️ Лайки",
                    callback_data=f"no:scenario:{Scenario.ACTIVITY_LIKE.value}",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="🌐 Трафик из соцсетей",
                    callback_data=f"no:scenario:{Scenario.SOCIAL_TRAFFIC.value}",
                )
            ],
            [InlineKeyboardButton(text=BTN_CANCEL, callback_data="no:cancel")],
        ]
    )


def exchange_choice(scenario: Scenario) -> InlineKeyboardMarkup:
    """Step 2: choose the exchange. Panel vs task depends on scenario.

    - ACTIVITY_SUBSCRIBE / ACTIVITY_LIKE → both panel and task are valid.
    - SOCIAL_TRAFFIC → typically a task-exchange action, but we surface both
      and let cost / capability checks gate placement.
    """
    rows: list[list[InlineKeyboardButton]] = []
    panel_row = [
        InlineKeyboardButton(text=name, callback_data=f"no:exchange:{name}")
        for name in PANEL_EXCHANGES
    ]
    task_row = [
        InlineKeyboardButton(text=name, callback_data=f"no:exchange:{name}")
        for name in TASK_EXCHANGES
    ]
    rows.append(panel_row)
    rows.append(task_row[:2])
    rows.append(task_row[2:])
    rows.append([InlineKeyboardButton(text=BTN_CANCEL, callback_data="no:cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def source_platform_choice() -> InlineKeyboardMarkup:
    """Step 3 (SOCIAL_TRAFFIC only): choose the platform driving traffic."""
    labels = {
        SourcePlatform.VK: "ВКонтакте",
        SourcePlatform.X: "X (Twitter)",
        SourcePlatform.YOUTUBE: "YouTube",
        SourcePlatform.TELEGRAM: "Telegram",
        SourcePlatform.DZEN: "Дзен",
        SourcePlatform.PINTEREST: "Pinterest",
    }
    keys = list(labels.keys())
    rows = [
        [
            InlineKeyboardButton(
                text=labels[keys[i]], callback_data=f"no:platform:{keys[i].value}"
            ),
            InlineKeyboardButton(
                text=labels[keys[i + 1]],
                callback_data=f"no:platform:{keys[i + 1].value}",
            ),
        ]
        for i in range(0, len(keys), 2)
    ]
    rows.append([InlineKeyboardButton(text=BTN_CANCEL, callback_data="no:cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def confirm_order(draft_uuid: str) -> InlineKeyboardMarkup:
    """Final step: confirm or cancel a drafted order."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Подтвердить и создать",
                    callback_data=f"no:confirm:{draft_uuid}",
                ),
                InlineKeyboardButton(text=BTN_CANCEL, callback_data="no:cancel"),
            ]
        ]
    )


def cancel_only() -> InlineKeyboardMarkup:
    """Single Cancel button — used during free-text steps (target, quantity)."""
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=BTN_CANCEL, callback_data="no:cancel")]]
    )


def service_choice(options: list) -> InlineKeyboardMarkup:
    """Step 3: pick a service from the exchange's catalogue.

    `options` is a list of `adapters.base.ServiceOption` (typed loosely here to
    keep this module free of an adapter dependency). Each becomes a row with a
    label like `Подписчики со всего мира · ₽0.18/шт · ≥20`. A final row offers
    `Ввести ID вручную` (manual entry) and `❌ Отмена` so the user is never
    stuck without an exit.
    """
    rows: list[list[InlineKeyboardButton]] = []
    for opt in options[:10]:
        rows.append(
            [
                InlineKeyboardButton(
                    text=opt.button_label[:64],
                    callback_data=f"no:service:{opt.service_id}",
                )
            ]
        )
    rows.append(
        [InlineKeyboardButton(text="✏️ Ввести ID вручную", callback_data="no:service_manual")]
    )
    rows.append([InlineKeyboardButton(text=BTN_CANCEL, callback_data="no:cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ---------------------------------------------------------------------------
# Submission review (admin decision)
# ---------------------------------------------------------------------------


def accept_rework(submission_uuid: str) -> InlineKeyboardMarkup:
    """Two buttons: Accept (pays) / Rework (returns)."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Принять", callback_data=f"accept:{submission_uuid}"),
                InlineKeyboardButton(
                    text="❌ Вернуть на доработку",
                    callback_data=f"reject:{submission_uuid}",
                ),
            ]
        ]
    )


# ---------------------------------------------------------------------------
# Orders list + detail
# ---------------------------------------------------------------------------


def orders_list(orders: list[dict]) -> InlineKeyboardMarkup:
    """Tap a row to open the order's detail card.

    `orders` is a list of dicts with `client_order_uuid`, `status`, `exchange`,
    `quantity`, `target` keys.
    """
    rows: list[list[InlineKeyboardButton]] = []
    for o in orders[:20]:
        short = o["client_order_uuid"][:8]
        label = f"{short} · {o['status']} · {o['exchange']} · {o['quantity']}"
        rows.append(
            [InlineKeyboardButton(text=label, callback_data=f"order:open:{o['client_order_uuid']}")]
        )
    if not rows:
        rows.append([InlineKeyboardButton(text="(нет активных заказов)", callback_data="noop")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def order_detail(order_uuid: str) -> InlineKeyboardMarkup:
    """Detail view: refresh status, go back to list."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="↻ Обновить", callback_data=f"order:refresh:{order_uuid}"
                ),
                InlineKeyboardButton(text="◀ К списку", callback_data="order:back"),
            ]
        ]
    )
