"""APScheduler-based scheduler for periodic tasks.

Jobs:
- poll_active_orders   : every N minutes, per-exchange lock + jitter
- poll_new_posts       : every M minutes, A3
- weekly_report        : weekly, A9

MVP: thin wrapper that exposes `start()` / `shutdown()` and calls the
orchestrator / post watcher / reporting modules.
"""

from __future__ import annotations

from aiogram.exceptions import TelegramAPIError
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from config import Settings
from orchestrator import Orchestrator
from posts.watcher import PostWatcher


class Scheduler:
    """Owns the APScheduler instance and job registration."""

    def __init__(
        self,
        settings: Settings,
        orchestrator: Orchestrator,
        post_watcher: PostWatcher,
    ) -> None:
        self._settings = settings
        self._orchestrator = orchestrator
        self._post_watcher = post_watcher
        self._scheduler = AsyncIOScheduler()

    def start(self) -> None:
        self._scheduler.add_job(
            self._orchestrator.poll_all,
            trigger=IntervalTrigger(minutes=5),
            id="poll_active_orders",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
        self._scheduler.add_job(
            self._poll_posts_job,
            trigger=IntervalTrigger(minutes=15),
            id="poll_new_posts",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
        self._scheduler.start()

    def shutdown(self, wait: bool = True) -> None:
        self._scheduler.shutdown(wait=wait)

    async def _poll_posts_job(self) -> None:
        new_posts = await self._post_watcher.run_once()
        if new_posts and hasattr(self, "_bot") and self._bot is not None:
            for post in new_posts:
                for admin_id in self._settings.telegram_admin_ids:
                    try:
                        await self._bot.send_message(
                            chat_id=admin_id,
                            text=(
                                f"📢 Новый пост detected\n{post['post_url']}\n"
                                f"(prev {post['previous_id'][:16]} → cur {post['current_id'][:16]})"
                            ),
                        )
                    except TelegramAPIError:
                        pass

    async def run_poll_now(self) -> list[dict]:
        """Manual trigger (CLI / Telegram) — run one poll cycle."""
        return await self._orchestrator.poll_all()
