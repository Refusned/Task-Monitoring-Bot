"""Main entry point: Telegram bot + APScheduler.

- Starts aiogram polling for the Telegram bot.
- Schedules background jobs:
  * `poll_active_orders` (orchestrator lifecycle)
  * `poll_new_posts`     (posts/watcher.py)
  * `weekly_report`      (reporting/sheets.py)

Graceful shutdown on SIGINT/SIGTERM.
"""

from __future__ import annotations

import asyncio
import fcntl
import os
import signal
import sys
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path

import httpx
from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import BotCommand
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from bot.handlers import build_dispatcher
from cli import _build_adapters
from config import Settings, get_settings
from db.database import init_db
from orchestrator import Orchestrator
from posts.watcher import run_once as poll_posts_once
from reporting.sheets import SheetsReporter


@contextmanager
def _single_instance_lock(lock_path: Path = Path("bot.lock")):
    """Prevent multiple local long-polling processes for the same bot."""
    lock_file = lock_path.open("w", encoding="utf-8")
    try:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError(
                f"Bot is already running locally (lock file: {lock_path}). "
                "Stop the existing process before starting another polling instance."
            ) from exc
        lock_file.write(f"{os.getpid()}\n")
        lock_file.flush()
        yield
    finally:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        finally:
            lock_file.close()


async def _job_poll_posts(settings: Settings, bot: Bot | None) -> None:
    """APScheduler job: one pass of the post watcher."""
    if not settings.target_social_accounts:
        return
    new_posts = await poll_posts_once(settings)
    if bot is not None and new_posts:
        for post in new_posts:
            # Notify every admin about a new post
            for admin_id in settings.telegram_admin_ids:
                try:
                    await bot.send_message(
                        chat_id=admin_id,
                        text=(
                            f"📢 Новый пост detected\n{post['post_url']}\n"
                            f"(prev {post['previous_id'][:16]} → cur {post['current_id'][:16]})"
                        ),
                    )
                except TelegramAPIError:
                    pass


async def _job_weekly_report(settings: Settings) -> None:
    """APScheduler job: write weekly report rows to Google Sheets."""
    reporter = SheetsReporter(settings)
    try:
        await reporter.run_weekly_report()
    except Exception as exc:
        print(f"[weekly_report] failed: {exc}", file=sys.stderr)


async def _job_poll_orders(settings: Settings) -> None:
    """APScheduler job: one orchestrator pass over active/verifying orders."""
    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as http_client:
        adapters = _build_adapters(settings, dry_run=settings.dry_run, http_client=http_client)
        orchestrator = Orchestrator(settings, adapters)
        results = await orchestrator.poll_all()
    errors = [r for r in results if r.get("status") == "error"]
    if errors:
        print(f"[poll_orders] {len(errors)} errors: {errors[:3]}", file=sys.stderr)


async def _reconcile_orphans_at_startup(settings: Settings) -> None:
    """C1 startup reconciliation (HIGH-4 fix).

    Any `CREATING` row older than `min_age_seconds` (5 min default) is moved to
    FAILED with an audit entry. A row younger than that is left alone — the
    creator is most likely still mid-flight between `INSERT INTO orders` and
    `mark_order_active`. Per-adapter exchange-side cross-check is future scope.
    """
    async with httpx.AsyncClient(timeout=30.0) as http_client:
        adapters = _build_adapters(settings, dry_run=settings.dry_run, http_client=http_client)
        orchestrator = Orchestrator(settings, adapters)
        n = await orchestrator.reconcile_creating(actor="startup")
    if n:
        print(f"[startup] reconciled {n} orphan CREATING row(s) → FAILED")


@asynccontextmanager
async def lifespan(settings: Settings):
    """Setup DB + scheduler; teardown on exit."""
    await init_db(settings)
    await _reconcile_orphans_at_startup(settings)
    scheduler = AsyncIOScheduler()
    scheduler.start()
    yield scheduler
    scheduler.shutdown(wait=False)


async def main() -> None:
    settings = get_settings()
    if not settings.telegram_bot_token:
        print("TELEGRAM_BOT_TOKEN is not set — bot cannot start.", file=sys.stderr)
        sys.exit(1)

    with _single_instance_lock():
        bot = Bot(token=settings.telegram_bot_token)
        try:
            # Register the slash-command menu shown when the user types `/`.
            # Buttons in the main Reply keyboard are the primary UX; these are
            # backup shortcuts for power users.
            await bot.set_my_commands(
                [
                    BotCommand(command="start", description="Главное меню"),
                    BotCommand(command="menu", description="Показать меню"),
                    BotCommand(command="new_order", description="📦 Новый заказ"),
                    BotCommand(command="dashboard", description="📊 Сводка"),
                    BotCommand(command="orders", description="📋 Активные заказы"),
                    BotCommand(command="check", description="🔎 Запустить проверку"),
                    BotCommand(command="review", description="🧾 Сабмишены на ручное решение"),
                    BotCommand(command="report_preview", description="📄 Отчёт за неделю"),
                    BotCommand(command="health", description="🩺 Состояние интеграций"),
                    BotCommand(command="cancel", description="❌ Отменить действие"),
                ]
            )
            dp = build_dispatcher(settings)

            # Wire settings into dispatcher context so handlers can read them.
            dp["settings"] = settings

            # Note: do NOT store bot under dp["bot"] — aiogram 3 auto-injects
            # `bot=` into every handler signature from that key, causing
            # TypeError for handlers that don't declare it.
            # Use message.bot / query.bot inside handlers instead.

            # Graceful shutdown helpers.
            stop_event = asyncio.Event()

            def _on_signal(_sig) -> None:
                stop_event.set()

            loop = asyncio.get_running_loop()
            for sig in (signal.SIGINT, signal.SIGTERM):
                loop.add_signal_handler(sig, _on_signal, sig)

            async with lifespan(settings) as scheduler:
                # Schedule background jobs.
                scheduler.add_job(
                    _job_poll_orders,
                    "interval",
                    seconds=settings.order_poll_interval_seconds,
                    args=(settings,),
                    id="poll_active_orders",
                    replace_existing=True,
                    max_instances=1,
                    coalesce=True,
                )
                if settings.target_social_accounts:
                    scheduler.add_job(
                        _job_poll_posts,
                        "interval",
                        seconds=settings.posts_poll_interval_seconds,
                        args=(settings, bot),
                        id="poll_posts",
                        replace_existing=True,
                        max_instances=1,
                        coalesce=True,
                    )
                # Weekly report: every Monday 09:00 UTC.
                scheduler.add_job(
                    _job_weekly_report,
                    "cron",
                    day_of_week="mon",
                    hour=9,
                    minute=0,
                    args=(settings,),
                    id="weekly_report",
                    replace_existing=True,
                )

                print("Bot started. Press Ctrl+C to stop.")
                await _run_polling_until_stopped(dp, bot, stop_event)

            print("Shutdown complete.")
        finally:
            await bot.session.close()


async def _run_polling_until_stopped(dp, bot: Bot, stop_event: asyncio.Event) -> None:
    """Run aiogram polling until OS signal or polling task failure.

    `main()` owns signal handling. Passing `handle_signals=False` avoids aiogram
    replacing our handlers and leaving the shutdown event unset.
    """
    polling_task = asyncio.create_task(
        dp.start_polling(
            bot,
            handle_signals=False,
            allowed_updates=dp.resolve_used_update_types(),
        )
    )
    stop_task = asyncio.create_task(stop_event.wait())
    done, _pending = await asyncio.wait(
        {polling_task, stop_task},
        return_when=asyncio.FIRST_COMPLETED,
    )

    if polling_task in done:
        stop_task.cancel()
        try:
            await stop_task
        except asyncio.CancelledError:
            pass
        await polling_task
        return

    polling_task.cancel()
    try:
        await polling_task
    except asyncio.CancelledError:
        pass

    if not stop_task.done():
        stop_task.cancel()
        try:
            await stop_task
        except asyncio.CancelledError:
            pass


if __name__ == "__main__":
    asyncio.run(main())
