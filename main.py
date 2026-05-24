"""Main entry point: Telegram bot + APScheduler.

- Starts aiogram polling for the Telegram bot.
- Schedules background jobs:
  * `poll_active_orders` (placeholder for Day 3 orchestrator)
  * `poll_new_posts`     (posts/watcher.py)
  * `weekly_report`      (reporting/sheets.py)

Graceful shutdown on SIGINT/SIGTERM.
"""

from __future__ import annotations

import asyncio
import signal
import sys
from contextlib import asynccontextmanager

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from bot.handlers import build_dispatcher
from config import Settings, get_settings
from db.database import init_db
from posts.watcher import run_once as poll_posts_once
from reporting.sheets import SheetsReporter


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


@asynccontextmanager
async def lifespan(settings: Settings):
    """Setup DB + scheduler; teardown on exit."""
    await init_db(settings)
    scheduler = AsyncIOScheduler()
    scheduler.start()
    yield scheduler
    scheduler.shutdown(wait=False)


async def main() -> None:
    settings = get_settings()
    if not settings.telegram_bot_token:
        print("TELEGRAM_BOT_TOKEN is not set — bot cannot start.", file=sys.stderr)
        sys.exit(1)

    bot = Bot(token=settings.telegram_bot_token)
    dp = build_dispatcher(settings)

    # Wire settings into dispatcher context so handlers can read them
    dp["settings"] = settings
    dp["bot"] = bot

    # Graceful shutdown helpers
    stop_event = asyncio.Event()

    def _on_signal(_sig) -> None:
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _on_signal, sig)

    async with lifespan(settings) as scheduler:
        # Schedule background jobs
        if settings.target_social_accounts:
            scheduler.add_job(
                _job_poll_posts,
                "interval",
                minutes=5,
                args=(settings, bot),
                id="poll_posts",
                replace_existing=True,
            )
        # Weekly report: every Monday 09:00 UTC
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

        # Run bot polling in a background task
        polling_task = asyncio.create_task(dp.start_polling(bot))

        print("Bot started. Press Ctrl+C to stop.")
        await stop_event.wait()

        polling_task.cancel()
        try:
            await polling_task
        except asyncio.CancelledError:
            pass

    await bot.session.close()
    print("Shutdown complete.")


if __name__ == "__main__":
    asyncio.run(main())
