"""TelegramMTProtoVerifier tests with a fake Telethon client.

No real Telegram traffic. We assert URL parsing, capability dispatch, and
metric fetch for both subscribes / views, plus failure paths.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from models import SourcePlatform, TaskType
from verification.telegram_mtproto import (
    TelegramMTProtoVerifier,
    parse_telegram_url,
)


# --- URL parser -----------------------------------------------------------


def test_parse_channel_url() -> None:
    assert parse_telegram_url("https://t.me/durov") == ("durov", None)
    assert parse_telegram_url("t.me/durov") == ("durov", None)
    assert parse_telegram_url("https://t.me/durov/") == ("durov", None)


def test_parse_post_url() -> None:
    assert parse_telegram_url("https://t.me/durov/123") == ("durov", 123)
    assert parse_telegram_url("t.me/durov/42") == ("durov", 42)


def test_parse_rejects_invite_and_private() -> None:
    assert parse_telegram_url("https://t.me/+abc123def") is None
    assert parse_telegram_url("https://t.me/c/1234567890/55") is None


def test_parse_rejects_garbage() -> None:
    assert parse_telegram_url("") is None
    assert parse_telegram_url("https://example.com/channel") is None
    assert parse_telegram_url("not a url") is None
    assert parse_telegram_url(None) is None  # type: ignore[arg-type]


def test_parse_rejects_short_username() -> None:
    # Telegram usernames are min 5 chars
    assert parse_telegram_url("https://t.me/abc") is None


# --- Verifier dispatch ----------------------------------------------------


def _make_verifier(client) -> TelegramMTProtoVerifier:
    return TelegramMTProtoVerifier(
        api_id=12345,
        api_hash="hashhash",
        session_string="session",
        client=client,
    )


def test_supports_only_telegram_subs_and_views() -> None:
    v = _make_verifier(client=MagicMock())
    assert v.supports(SourcePlatform.TELEGRAM, TaskType.SUBSCRIBES)
    assert v.supports(SourcePlatform.TELEGRAM, TaskType.VIEWS)
    assert not v.supports(SourcePlatform.TELEGRAM, TaskType.LIKES)
    assert not v.supports(SourcePlatform.YOUTUBE, TaskType.SUBSCRIBES)


def test_constructor_rejects_missing_credentials() -> None:
    with pytest.raises(ValueError, match="requires api_id"):
        TelegramMTProtoVerifier(api_id=0, api_hash="x", session_string="y")
    with pytest.raises(ValueError, match="requires api_id"):
        TelegramMTProtoVerifier(api_id=1, api_hash="", session_string="y")
    with pytest.raises(ValueError, match="requires api_id"):
        TelegramMTProtoVerifier(api_id=1, api_hash="x", session_string="")


# --- subscribes ----------------------------------------------------------


async def test_fetch_subscribers_happy_path() -> None:
    """get_entity + GetFullChannelRequest(...) → returns participants_count."""
    full = SimpleNamespace(full_chat=SimpleNamespace(participants_count=4242))

    class Client:
        def __init__(self):
            self.is_connected = lambda: True
            self.get_entity = AsyncMock(return_value=SimpleNamespace(id=1))

        async def __call__(self, request):  # Telethon-style RPC call
            return full

    v = _make_verifier(client=Client())

    async def fake_ensure():
        return v._client

    v._ensure_client = fake_ensure  # type: ignore[method-assign]

    result = await v.fetch_metric("https://t.me/durov", TaskType.SUBSCRIBES)
    assert result == 4242.0


async def test_fetch_subscribers_rpc_error_returns_none() -> None:
    class FailingClient:
        def __init__(self):
            self.is_connected = lambda: True
            self.get_entity = AsyncMock(side_effect=RuntimeError("ChannelInvalid"))

        async def __call__(self, request):
            raise RuntimeError("should not reach")

    v = _make_verifier(client=FailingClient())

    async def fake_ensure():
        return v._client

    v._ensure_client = fake_ensure  # type: ignore[method-assign]

    result = await v.fetch_metric("https://t.me/nonexistent_channel_xyz", TaskType.SUBSCRIBES)
    assert result is None


# --- views --------------------------------------------------------------


async def test_fetch_views_happy_path() -> None:
    class Client:
        def __init__(self):
            self.is_connected = lambda: True
            self.get_messages = AsyncMock(
                return_value=SimpleNamespace(views=15234, id=123)
            )

        async def __call__(self, request):
            raise RuntimeError("not used for views")

    v = _make_verifier(client=Client())

    async def fake_ensure():
        return v._client

    v._ensure_client = fake_ensure  # type: ignore[method-assign]

    result = await v.fetch_metric("https://t.me/durov/123", TaskType.VIEWS)
    assert result == 15234.0


async def test_fetch_views_post_id_missing_returns_none() -> None:
    """Views requested but URL has no /<post_id> → None (caller can't verify)."""
    class Client:
        def __init__(self):
            self.is_connected = lambda: True
            self.get_messages = AsyncMock()  # should not be called

        async def __call__(self, request):
            raise RuntimeError("not used")

    v = _make_verifier(client=Client())

    async def fake_ensure():
        return v._client

    v._ensure_client = fake_ensure  # type: ignore[method-assign]

    result = await v.fetch_metric("https://t.me/durov", TaskType.VIEWS)
    assert result is None


async def test_fetch_views_when_message_has_no_views_returns_none() -> None:
    """Channel posts have views; PM-style messages don't. Treat as None."""
    class Client:
        def __init__(self):
            self.is_connected = lambda: True
            self.get_messages = AsyncMock(
                return_value=SimpleNamespace(views=None, id=42)
            )

        async def __call__(self, request):
            raise RuntimeError()

    v = _make_verifier(client=Client())

    async def fake_ensure():
        return v._client

    v._ensure_client = fake_ensure  # type: ignore[method-assign]

    result = await v.fetch_metric("https://t.me/durov/42", TaskType.VIEWS)
    assert result is None


# --- fallthrough ---------------------------------------------------------


async def test_unsupported_metric_returns_none() -> None:
    v = _make_verifier(client=MagicMock())
    result = await v.fetch_metric("https://t.me/durov", TaskType.LIKES)
    assert result is None


async def test_invalid_url_returns_none_without_rpc() -> None:
    """Make sure we DON'T call the client when the URL can't be parsed."""
    client = MagicMock()
    client.get_entity = AsyncMock(side_effect=RuntimeError("should not be called"))
    v = _make_verifier(client=client)

    result = await v.fetch_metric("https://example.com/", TaskType.SUBSCRIBES)
    assert result is None
    client.get_entity.assert_not_called()
