"""Real activity counters used by ActivityVerifier and the autopilot baseline step."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
from pydantic import BaseModel, Field

from models import Scenario

_YOUTUBE_VIDEOS_URL = "https://www.googleapis.com/youtube/v3/videos"
_YOUTUBE_CHANNELS_URL = "https://www.googleapis.com/youtube/v3/channels"
_YOUTUBE_CHANNEL_ID_RE = re.compile(r"UC[A-Za-z0-9_-]{22}")


class ActivityMetricSnapshot(BaseModel):
    """One measured public counter value."""

    metric: str
    count: int = Field(ge=0)
    source: str
    raw_evidence: dict[str, Any] = Field(default_factory=dict)


class ActivityMetricsProvider:
    """Interface for real social-platform counters."""

    async def measure(self, target: str, scenario: Scenario) -> ActivityMetricSnapshot | None:
        raise NotImplementedError


class YouTubeMetricsProvider(ActivityMetricsProvider):
    """Reads YouTube video and channel statistics through YouTube Data API."""

    def __init__(
        self,
        api_key: str,
        *,
        http_client: httpx.AsyncClient | None = None,
        timeout_seconds: float = 30.0,
    ) -> None:
        self._api_key = api_key
        self._client = http_client
        self._timeout_seconds = timeout_seconds

    async def measure(self, target: str, scenario: Scenario) -> ActivityMetricSnapshot | None:
        if scenario == Scenario.ACTIVITY_LIKE:
            metric = "likeCount"
        elif scenario == Scenario.ACTIVITY_VIEW:
            metric = "viewCount"
        elif scenario == Scenario.ACTIVITY_SUBSCRIBE:
            return await self._measure_channel_subscribers(target)
        else:
            return None

        video_id = extract_youtube_video_id(target)
        if video_id is None:
            return None
        if not self._api_key:
            raise ValueError("YOUTUBE_DATA_API_KEY is required for YouTube activity checks")

        params = {
            "part": "statistics",
            "id": video_id,
            "key": self._api_key,
        }
        if self._client is None:
            async with httpx.AsyncClient(timeout=self._timeout_seconds) as client:
                payload = await _get_json(client, _YOUTUBE_VIDEOS_URL, params)
        else:
            payload = await _get_json(self._client, _YOUTUBE_VIDEOS_URL, params)

        items = payload.get("items")
        if not isinstance(items, list) or not items:
            raise ValueError(f"YouTube video {video_id!r} was not found")
        statistics = items[0].get("statistics") or {}
        if metric not in statistics:
            raise ValueError(f"YouTube statistics for {video_id!r} does not include {metric}")
        count = int(statistics[metric])
        return ActivityMetricSnapshot(
            metric=metric,
            count=count,
            source="youtube_data_api",
            raw_evidence={
                "provider": "youtube",
                "video_id": video_id,
                "metric": metric,
            },
        )

    async def _measure_channel_subscribers(self, target: str) -> ActivityMetricSnapshot | None:
        lookup = extract_youtube_channel_lookup(target)
        if lookup is None:
            return None
        if not self._api_key:
            raise ValueError("YOUTUBE_DATA_API_KEY is required for YouTube activity checks")

        params = {
            "part": "statistics",
            "key": self._api_key,
            lookup.kind: lookup.value,
        }
        if self._client is None:
            async with httpx.AsyncClient(timeout=self._timeout_seconds) as client:
                payload = await _get_json(client, _YOUTUBE_CHANNELS_URL, params)
        else:
            payload = await _get_json(self._client, _YOUTUBE_CHANNELS_URL, params)

        items = payload.get("items")
        if not isinstance(items, list) or not items:
            raise ValueError(f"YouTube channel {target!r} was not found")
        statistics = items[0].get("statistics") or {}
        metric = "subscriberCount"
        if metric not in statistics:
            raise ValueError(f"YouTube statistics for {target!r} does not include {metric}")
        count = int(statistics[metric])
        return ActivityMetricSnapshot(
            metric=metric,
            count=count,
            source="youtube_data_api",
            raw_evidence={
                "provider": "youtube",
                "channel_lookup": lookup.kind,
                "channel_lookup_value": lookup.value,
                "metric": metric,
            },
        )


class CompositeActivityMetricsProvider(ActivityMetricsProvider):
    """Try providers in order until one can measure the target."""

    def __init__(self, providers: list[ActivityMetricsProvider]) -> None:
        self._providers = providers

    async def measure(self, target: str, scenario: Scenario) -> ActivityMetricSnapshot | None:
        for provider in self._providers:
            snapshot = await provider.measure(target, scenario)
            if snapshot is not None:
                return snapshot
        return None


def build_activity_metrics_provider(
    *,
    youtube_api_key: str = "",
    http_client: httpx.AsyncClient | None = None,
) -> ActivityMetricsProvider:
    """Build the configured public-counter provider chain."""
    providers: list[ActivityMetricsProvider] = []
    if youtube_api_key:
        providers.append(YouTubeMetricsProvider(youtube_api_key, http_client=http_client))
    return CompositeActivityMetricsProvider(providers)


async def _get_json(
    client: httpx.AsyncClient,
    url: str,
    params: dict[str, str],
) -> dict[str, Any]:
    response = await client.get(url, params=params)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError("YouTube returned a non-object response")
    return payload


def extract_youtube_video_id(url_or_id: str) -> str | None:
    """Extract a YouTube video id from common watch, shorts, embed, youtu.be URLs."""
    value = url_or_id.strip()
    if not value:
        return None
    if _is_youtube_video_id(value):
        return value

    parsed = urlparse(value)
    host = parsed.netloc.lower().removeprefix("www.")
    if host in {"youtube.com", "m.youtube.com", "music.youtube.com"}:
        if parsed.path == "/watch":
            ids = parse_qs(parsed.query).get("v")
            return ids[0] if ids and _is_youtube_video_id(ids[0]) else None
        parts = [part for part in parsed.path.split("/") if part]
        if (
            len(parts) >= 2
            and parts[0] in {"shorts", "embed", "live"}
            and _is_youtube_video_id(parts[1])
        ):
            return parts[1]
    if host == "youtu.be":
        parts = [part for part in parsed.path.split("/") if part]
        return parts[0] if parts and _is_youtube_video_id(parts[0]) else None
    return None


class YouTubeChannelLookup(BaseModel):
    """A channels.list lookup parameter inferred from a channel target."""

    kind: str
    value: str


def extract_youtube_channel_lookup(url_or_id: str) -> YouTubeChannelLookup | None:
    """Extract a channels.list lookup from channel id, handle, or legacy user URL."""
    value = url_or_id.strip()
    if not value:
        return None
    if _YOUTUBE_CHANNEL_ID_RE.fullmatch(value):
        return YouTubeChannelLookup(kind="id", value=value)
    if _is_youtube_handle(value):
        return YouTubeChannelLookup(kind="forHandle", value=value.removeprefix("@"))

    parsed = urlparse(value)
    host = parsed.netloc.lower().removeprefix("www.")
    if host not in {"youtube.com", "m.youtube.com"}:
        return None

    parts = [part for part in parsed.path.split("/") if part]
    if not parts:
        return None
    if len(parts) >= 2 and parts[0] == "channel" and _YOUTUBE_CHANNEL_ID_RE.fullmatch(parts[1]):
        return YouTubeChannelLookup(kind="id", value=parts[1])
    if len(parts) >= 2 and parts[0] == "user" and parts[1]:
        return YouTubeChannelLookup(kind="forUsername", value=parts[1])
    if _is_youtube_handle(parts[0]):
        return YouTubeChannelLookup(kind="forHandle", value=parts[0].removeprefix("@"))
    return None


def _is_youtube_video_id(value: str) -> bool:
    return re.fullmatch(r"[A-Za-z0-9_-]{11}", value) is not None


def _is_youtube_handle(value: str) -> bool:
    return re.fullmatch(r"@[A-Za-z0-9_.-]{3,30}", value) is not None
