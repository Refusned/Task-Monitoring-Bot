from __future__ import annotations

import asyncio

import pytest

from cli import create_order_command
from config import Settings
from db.database import connect, init_db
from main import _run_polling_until_stopped, _single_instance_lock
from models import Scenario, SourcePlatform


def test_single_instance_lock_blocks_second_local_polling_process(tmp_path) -> None:
    lock_path = tmp_path / "bot.lock"

    with _single_instance_lock(lock_path):
        with pytest.raises(RuntimeError, match="already running"):
            with _single_instance_lock(lock_path):
                pass


class _FakeDispatcher:
    def __init__(self) -> None:
        self.kwargs = None

    def resolve_used_update_types(self) -> list[str]:
        return ["message", "callback_query"]

    async def start_polling(self, _bot, **kwargs) -> None:
        self.kwargs = kwargs
        await asyncio.Event().wait()


async def test_run_polling_uses_main_signal_handling() -> None:
    dispatcher = _FakeDispatcher()
    stop_event = asyncio.Event()

    task = asyncio.create_task(_run_polling_until_stopped(dispatcher, object(), stop_event))
    await asyncio.sleep(0)
    stop_event.set()
    await task

    assert dispatcher.kwargs["handle_signals"] is False
    assert dispatcher.kwargs["allowed_updates"] == ["message", "callback_query"]


async def test_create_order_is_blocked_in_dry_run(tmp_path) -> None:
    settings = Settings(dry_run=True, db_path=tmp_path / "main.db")
    await init_db(settings)

    args = type(
        "Args",
        (),
        {
            "dry_run": True,
            "exchange": "unu",
            "scenario": Scenario.SOCIAL_TRAFFIC.value,
            "target": "https://example.com",
            "quantity": 5,
            "source_platform": SourcePlatform.VK.value,
            "service_id": None,
            "max_cost": 2.0,
        },
    )()
    assert await create_order_command(settings, args) == 2

    async with connect(settings) as conn:
        cursor = await conn.execute("SELECT COUNT(*) AS c FROM orders")
        row = await cursor.fetchone()
    assert row["c"] == 0
