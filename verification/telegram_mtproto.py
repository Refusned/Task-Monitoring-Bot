"""Telegram verifier via MTProto (Telethon).

Reads channel subscriber counts and post view counts from a user-level
Telegram account. Auth: `api_id` + `api_hash` registered at
https://my.telegram.org/apps, plus a `StringSession` produced by
`scripts/telegram_mtproto_setup.py` (one-time interactive flow).

Why user-level (MTProto), not Bot API:
- Bot API can only count subscribers in channels where the bot is a member,
  and CANNOT read message views at all. SMM verification needs both for
  arbitrary channels — only the user-level API exposes them.

Lifecycle:
- `TelegramClient` keeps a persistent TCP connection. Build once at app
  startup, share across requests, close at shutdown.
- All RPC is rate-limited (1 req/sec by default below). Telethon also
  handles FloodWaitError transparently if the server demands a longer
  delay, so we don't burn the account by accident.

Supported metrics:
- `(TELEGRAM, SUBSCRIBES)` — `GetFullChannelRequest(channel).full_chat.participants_count`
- `(TELEGRAM, VIEWS)`      — `client.get_messages(channel, ids=msg_id).views`

URL shapes accepted:
- `https://t.me/channelname`
- `https://t.me/channelname/123`         (post)
- `t.me/channelname`                     (no scheme)
- `https://t.me/+invite_hash`            (private — not supported, we'd need
                                          to join first)
- `https://t.me/c/1234567890/123`        (private channel by numeric id —
                                          not supported in MVP)
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import TYPE_CHECKING

from models import SourcePlatform, TaskType
from verification.base import PlatformVerifier

if TYPE_CHECKING:  # pragma: no cover
    from telethon import TelegramClient

_LOG = logging.getLogger("telegram_mtproto")

# Public channels: https://t.me/<username>[/<post_id>]
_PUBLIC_URL_PATTERN = re.compile(
    r"^(?:https?://)?t\.me/(?P<username>[A-Za-z][A-Za-z0-9_]{3,31})(?:/(?P<post_id>\d+))?/?$"
)
_INVITE_HASH_PATTERN = re.compile(r"^(?:https?://)?t\.me/\+[A-Za-z0-9_-]+/?$")
_PRIVATE_ID_PATTERN = re.compile(r"^(?:https?://)?t\.me/c/\d+(?:/\d+)?/?$")

# Conservative rate limit: 1 RPC per second on the user account. Telethon
# also auto-handles FloodWaitError separately — this is just additional
# headroom against ad-hoc bursts.
_MIN_INTERVAL_SECONDS = 1.0


class TelegramMTProtoVerifier(PlatformVerifier):
    """Counts Telegram channel subscribers / post views via Telethon."""

    name = "telegram_mtproto"

    def __init__(
        self,
        api_id: int,
        api_hash: str,
        session_string: str,
        *,
        client: TelegramClient | None = None,
    ) -> None:
        if not api_id or not api_hash or not session_string:
            raise ValueError(
                "TelegramMTProtoVerifier requires api_id + api_hash + session_string "
                "(see scripts/telegram_mtproto_setup.py)"
            )
        self._api_id = int(api_id)
        self._api_hash = str(api_hash)
        self._session_string = str(session_string)
        self._client = client  # allow injection for tests
        self._client_lock = asyncio.Lock()
        self._rate_lock = asyncio.Lock()
        self._last_rpc_at = 0.0

    def supports(self, platform: SourcePlatform, metric: TaskType) -> bool:
        return platform == SourcePlatform.TELEGRAM and metric in (
            TaskType.SUBSCRIBES,
            TaskType.VIEWS,
        )

    async def fetch_metric(self, url: str, metric: TaskType) -> float | None:
        parsed = parse_telegram_url(url)
        if parsed is None:
            _LOG.debug("URL not recognized as public t.me link: %s", url)
            return None
        username, post_id = parsed
        if metric == TaskType.SUBSCRIBES:
            return await self._fetch_subscribers(username)
        if metric == TaskType.VIEWS:
            if post_id is None:
                _LOG.debug("Views requested but URL has no post id: %s", url)
                return None
            return await self._fetch_post_views(username, post_id)
        return None

    async def _fetch_subscribers(self, username: str) -> float | None:
        client = await self._ensure_client()
        async with self._rate_limit():
            try:
                from telethon.tl.functions.channels import GetFullChannelRequest

                entity = await client.get_entity(username)
                full = await client(GetFullChannelRequest(entity))
            except Exception as exc:
                _LOG.warning("Telegram get_full_channel failed for %r: %r", username, exc)
                return None
        try:
            return float(full.full_chat.participants_count)
        except (AttributeError, TypeError, ValueError):
            return None

    async def _fetch_post_views(self, username: str, post_id: int) -> float | None:
        client = await self._ensure_client()
        async with self._rate_limit():
            try:
                msg = await client.get_messages(username, ids=post_id)
            except Exception as exc:
                _LOG.warning("Telegram get_messages failed for %r/%d: %r", username, post_id, exc)
                return None
        if msg is None:
            return None
        views = getattr(msg, "views", None)
        if views is None:
            return None
        try:
            return float(views)
        except (TypeError, ValueError):
            return None

    async def _ensure_client(self) -> TelegramClient:
        """Lazy-connect on first use. Idempotent if already connected."""
        if self._client is None:
            async with self._client_lock:
                if self._client is None:
                    from telethon import TelegramClient
                    from telethon.sessions import StringSession

                    self._client = TelegramClient(
                        StringSession(self._session_string),
                        self._api_id,
                        self._api_hash,
                    )
        if not self._client.is_connected():
            await self._client.connect()
            if not await self._client.is_user_authorized():
                raise RuntimeError(
                    "Telegram session not authorized — re-run "
                    "scripts/telegram_mtproto_setup.py to refresh TELEGRAM_MTPROTO_SESSION_STRING"
                )
        return self._client

    async def close(self) -> None:
        """Shutdown hook — called from app lifespan teardown."""
        if self._client is not None and self._client.is_connected():
            await self._client.disconnect()

    def _rate_limit(self):
        verifier = self

        class _Ctx:
            async def __aenter__(self):
                async with verifier._rate_lock:
                    loop = asyncio.get_running_loop()
                    now = loop.time()
                    wait = _MIN_INTERVAL_SECONDS - (now - verifier._last_rpc_at)
                    if wait > 0:
                        await asyncio.sleep(wait)
                    verifier._last_rpc_at = loop.time()

            async def __aexit__(self, *exc):
                return False

        return _Ctx()


def parse_telegram_url(url: str) -> tuple[str, int | None] | None:
    """Extract `(username, post_id_or_None)` from a public t.me URL.

    Returns None for invite-link / private-id / unrecognized shapes — those
    aren't verifiable without joining the channel first (MVP scope).
    """
    if not isinstance(url, str):
        return None
    stripped = url.strip()
    if not stripped:
        return None
    if _INVITE_HASH_PATTERN.match(stripped) or _PRIVATE_ID_PATTERN.match(stripped):
        return None
    m = _PUBLIC_URL_PATTERN.match(stripped)
    if not m:
        return None
    username = m.group("username")
    post_id_str = m.group("post_id")
    post_id = int(post_id_str) if post_id_str else None
    return username, post_id
