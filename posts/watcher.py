"""Post watcher: detects new posts in target social accounts (A3).

Simple polling-based detector. For each configured target social account URL,
the watcher keeps a memory of "seen" post identifiers. When a new identifier
appears, the watcher can trigger an `ACTIVITY_LIKE` order (subject to admin
confirmation by default, respecting A5 money-safety).

This MVP implementation uses a lightweight heuristic: for Telegram channels it
fetches the web preview page and extracts the last post path; for other
platforms it stores a hash of the page content. A production upgrade would use
each platform's native API (Telegram Bot API, VK API, etc.).

Config-driven knobs (see config.py / .env):
- `target_social_accounts` — list of URLs to watch.
- `posts_poll_interval_seconds` — how often to poll (default 300 = 5 min).
- `posts_auto_mode` — if True, auto-create draft orders; if False, only notify.
- `posts_likes_per_post` — quantity for the auto-created like order.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

import httpx

from config import Settings
from db.database import append_audit, connect

_WATCHED_STATE_PATH = Path("./.watcher_state.json")


def _load_state() -> dict[str, Any]:
    if _WATCHED_STATE_PATH.exists():
        return json.loads(_WATCHED_STATE_PATH.read_text(encoding="utf-8"))
    return {}


def _save_state(state: dict[str, Any]) -> None:
    _WATCHED_STATE_PATH.write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _last_post_id_telegram(html: str, account_url: str) -> str | None:
    """Heuristic: extract the last message path from a Telegram web preview."""
    matches = re.findall(r'href="([^"]+/\d+)"', html)
    if matches:

        def _msg_number(path: str) -> int:
            try:
                return int(path.rsplit("/", 1)[-1])
            except ValueError:
                return 0

        return max(matches, key=_msg_number)
    return None


def _last_post_id_generic(html: str, account_url: str) -> str | None:
    """Fallback: hash of the first 8 KB of the page to detect change."""
    return hashlib.sha256(html[:8192].encode("utf-8", errors="ignore")).hexdigest()[:16]


async def _fetch_snapshot(account_url: str, http_client: httpx.AsyncClient) -> str | None:
    try:
        resp = await http_client.get(account_url, timeout=15.0, follow_redirects=True)
        resp.raise_for_status()
        return resp.text
    except httpx.HTTPError:
        return None


def _extract_last_post_id(account_url: str, html: str) -> str | None:
    if "t.me/" in account_url or "telegram.me/" in account_url:
        return _last_post_id_telegram(html, account_url)
    return _last_post_id_generic(html, account_url)


async def check_accounts(
    settings: Settings, http_client: httpx.AsyncClient
) -> list[dict[str, str]]:
    """Poll all configured social accounts and return any newly detected posts."""
    state = _load_state()
    new_posts: list[dict[str, str]] = []

    for url in settings.target_social_accounts:
        html = await _fetch_snapshot(url, http_client)
        if html is None:
            continue
        current_id = _extract_last_post_id(url, html)
        if current_id is None:
            continue
        previous_id = state.get(url)
        if previous_id is not None and current_id != previous_id:
            new_posts.append(
                {
                    "account_url": url,
                    "previous_id": previous_id,
                    "current_id": current_id,
                    "post_url": f"{url}/{current_id.split('/')[-1]}" if "/" in current_id else url,
                }
            )
        state[url] = current_id

    _save_state(state)
    return new_posts


class PostWatcher:
    """Thin wrapper exposing a callable interface for the scheduler."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def run_once(self) -> list[dict[str, str]]:
        return await run_once(self._settings)


async def run_once(
    settings: Settings, *, notify_callback: Any | None = None
) -> list[dict[str, str]]:
    """Single pass of the watcher. Returns list of newly detected posts."""
    async with httpx.AsyncClient() as http_client:
        new_posts = await check_accounts(settings, http_client)

    if new_posts:
        async with connect(settings) as conn:
            for post in new_posts:
                await append_audit(
                    conn,
                    actor="post_watcher",
                    event="new_post_detected",
                    details=post,
                )
        if notify_callback is not None:
            for post in new_posts:
                await notify_callback(post)

    return new_posts
