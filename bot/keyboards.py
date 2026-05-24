"""Inline keyboards for Telegram bot.

Provides compact buttons for:
- admin accept / rework decisions on a submission,
- confirming a new order,
- simple navigation (cancel).
"""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def accept_rework(submission_uuid: str) -> InlineKeyboardMarkup:
    """Two buttons: Accept (pays) / Rework (returns)."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Принять", callback_data=f"accept:{submission_uuid}"),
                InlineKeyboardButton(
                    text="❌ Вернуть на доработку", callback_data=f"reject:{submission_uuid}"
                ),
            ]
        ]
    )


def confirm_order(client_order_uuid: str) -> InlineKeyboardMarkup:
    """Confirm / Cancel for a draft order before money is at risk."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Подтвердить", callback_data=f"confirm_order:{client_order_uuid}"
                ),
                InlineKeyboardButton(
                    text="❌ Отмена", callback_data=f"cancel_order:{client_order_uuid}"
                ),
            ]
        ]
    )


def simple_cancel(action_prefix: str, payload: str) -> InlineKeyboardMarkup:
    """Single Cancel button for multi-step flows."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="❌ Отмена", callback_data=f"cancel:{action_prefix}:{payload}"
                )
            ]
        ]
    )
