"""PlatformVerifier ABC + dispatcher for the agent's external metric checks.

Each verifier knows how to read ONE metric on ONE platform from the platform's
own API (or a public-page scrape, post-MVP). The orchestrator picks a verifier
via `select_verifier(platform, metric)` and gets back `None` when no real
verifier is wired — the dashboard surfaces `NEEDS_HUMAN_REVIEW` in that case,
never a silent pass.

In v4 MVP only `YouTubeVerifier` is real. The remaining platforms (VK, TG,
Instagram, TikTok, X, Pinterest, Dzen) are architectural slots — adding a
verifier is one new file + one line in `build_verifiers_from_settings`.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

import httpx

from models import SourcePlatform, TaskType


class PlatformVerifier(ABC):
    """Reads one metric on one platform. Returns the current absolute value."""

    name: str

    @abstractmethod
    def supports(self, platform: SourcePlatform, metric: TaskType) -> bool:
        """True if this verifier can read (platform, metric)."""

    @abstractmethod
    async def fetch_metric(self, url: str, metric: TaskType) -> float | None:
        """Return the current value of `metric` for `url`. None on failure
        (network, parse, unsupported url shape). NEVER raises for expected
        misses — let the caller treat None as `NEEDS_HUMAN_REVIEW`."""


def build_verifiers_from_settings(
    http_client: httpx.AsyncClient | None = None,
) -> list[PlatformVerifier]:
    """Construct the verifier registry from current settings.

    Order matters: first verifier that `supports(...)` wins in `select_verifier`.
    """
    from config import get_settings
    from verification.metrica import YandexMetricaVerifier
    from verification.youtube import YouTubeVerifier

    settings = get_settings()
    out: list[PlatformVerifier] = []
    if settings.youtube_api_key:
        out.append(YouTubeVerifier(api_key=settings.youtube_api_key, http_client=http_client))
    if settings.metrica_counter_id and settings.metrica_oauth_token:
        out.append(
            YandexMetricaVerifier(
                counter_id=settings.metrica_counter_id,
                oauth_token=settings.metrica_oauth_token,
                http_client=http_client,
                window_days=settings.metrica_verification_window_days,
            )
        )
    if (
        settings.telegram_mtproto_api_id
        and settings.telegram_mtproto_api_hash
        and settings.telegram_mtproto_session_string
    ):
        from verification.telegram_mtproto import TelegramMTProtoVerifier

        out.append(
            TelegramMTProtoVerifier(
                api_id=settings.telegram_mtproto_api_id,
                api_hash=settings.telegram_mtproto_api_hash,
                session_string=settings.telegram_mtproto_session_string,
            )
        )
    return out


def select_verifier(
    verifiers: list[PlatformVerifier],
    platform: SourcePlatform,
    metric: TaskType,
) -> PlatformVerifier | None:
    """First-match dispatch; returns None when nothing supports (platform, metric)."""
    for v in verifiers:
        if v.supports(platform, metric):
            return v
    return None
