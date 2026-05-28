"""YouTube Data API v3 verifier.

Reads video statistics (likeCount, viewCount, commentCount) via videos.list,
and channel subscriberCount via channels.list. Auth: API key in the `key=`
query param — no OAuth needed for public videos/channels. Free quota = 10,000
units/day; each list call costs 1 unit, so even a busy demo stays free.

Docs:
  https://developers.google.com/youtube/v3/docs/videos/list
  https://developers.google.com/youtube/v3/docs/channels/list
"""

from __future__ import annotations

import re

import httpx

from models import SourcePlatform, TaskType
from verification.base import PlatformVerifier

_VIDEOS_URL = "https://www.googleapis.com/youtube/v3/videos"
_CHANNELS_URL = "https://www.googleapis.com/youtube/v3/channels"
_SEARCH_URL = "https://www.googleapis.com/youtube/v3/search"

# Recognized URL shapes. The 11-char id pattern is YouTube's canonical id format.
_VIDEO_ID_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"youtube\.com/watch\?v=([A-Za-z0-9_-]{11})"),
    re.compile(r"youtu\.be/([A-Za-z0-9_-]{11})"),
    re.compile(r"youtube\.com/shorts/([A-Za-z0-9_-]{11})"),
    re.compile(r"youtube\.com/embed/([A-Za-z0-9_-]{11})"),
]

# Channel URL shapes. UC-id is YouTube's stable channel identifier (24 chars).
_CHANNEL_ID_PATTERN = re.compile(r"youtube\.com/channel/(UC[A-Za-z0-9_-]{22})")
_HANDLE_PATTERN = re.compile(r"youtube\.com/@([A-Za-z0-9._-]+)")
_LEGACY_USER_PATTERN = re.compile(r"youtube\.com/(?:user|c)/([A-Za-z0-9._-]+)")

# Map our agent-facing TaskType to the field name in YT statistics payload.
# (Video metrics — channels are handled separately via _fetch_channel_subs.)
_METRIC_FIELD: dict[TaskType, str] = {
    TaskType.LIKES: "likeCount",
    TaskType.VIEWS: "viewCount",
    TaskType.COMMENTS: "commentCount",
}


def extract_video_id(url: str) -> str | None:
    """Pull the 11-char video id out of any common YouTube URL form."""
    for pat in _VIDEO_ID_PATTERNS:
        m = pat.search(url)
        if m:
            return m.group(1)
    return None


def extract_channel_ref(url: str) -> tuple[str, str] | None:
    """Return (kind, value) for YouTube channel URLs.

    kind ∈ {"id", "handle", "username"}:
      - "id" → 24-char UC… ready for channels.list?id=
      - "handle" → @-handle resolved via channels.list?forHandle=
      - "username" → legacy /user/ or /c/ resolved via search.list fallback
    """
    if m := _CHANNEL_ID_PATTERN.search(url):
        return ("id", m.group(1))
    if m := _HANDLE_PATTERN.search(url):
        return ("handle", m.group(1))
    if m := _LEGACY_USER_PATTERN.search(url):
        return ("username", m.group(1))
    return None


class YouTubeVerifier(PlatformVerifier):
    """Real verifier — Data API v3, key auth. Videos + channels."""

    name = "youtube_data_api_v3"

    def __init__(self, api_key: str, http_client: httpx.AsyncClient | None = None) -> None:
        if not api_key:
            raise ValueError("YouTubeVerifier requires api_key (set YOUTUBE_API_KEY in .env)")
        self._api_key = api_key
        self._client = http_client

    def supports(self, platform: SourcePlatform, metric: TaskType) -> bool:
        if platform != SourcePlatform.YOUTUBE:
            return False
        return metric in _METRIC_FIELD or metric == TaskType.SUBSCRIBES

    async def fetch_metric(self, url: str, metric: TaskType) -> float | None:
        if metric == TaskType.SUBSCRIBES:
            return await self._fetch_channel_subs(url)
        field = _METRIC_FIELD.get(metric)
        if field is None:
            return None
        video_id = extract_video_id(url)
        if not video_id:
            return None
        params = {"id": video_id, "part": "statistics", "key": self._api_key}
        payload = await self._get_json(_VIDEOS_URL, params)
        if payload is None:
            return None
        items = payload.get("items") or []
        if not items:
            return None
        stats = items[0].get("statistics") or {}
        return _coerce_count(stats.get(field))

    async def _fetch_channel_subs(self, url: str) -> float | None:
        ref = extract_channel_ref(url)
        if ref is None:
            return None
        kind, value = ref
        if kind == "id":
            params = {"id": value, "part": "statistics", "key": self._api_key}
        elif kind == "handle":
            # forHandle requires the leading "@" per Data API v3 docs.
            params = {"forHandle": f"@{value}", "part": "statistics", "key": self._api_key}
        else:  # username (legacy)
            params = {"forUsername": value, "part": "statistics", "key": self._api_key}
        payload = await self._get_json(_CHANNELS_URL, params)
        if payload is None:
            return None
        items = payload.get("items") or []
        if not items and kind == "username":
            # /c/ vanity URLs aren't always resolvable via forUsername; fall back
            # to search.list for the channel id (costs 100 quota units, used rarely).
            payload = await self._get_json(
                _SEARCH_URL,
                {
                    "q": value,
                    "type": "channel",
                    "part": "snippet",
                    "maxResults": 1,
                    "key": self._api_key,
                },
            )
            if payload is None:
                return None
            search_items = payload.get("items") or []
            if not search_items:
                return None
            channel_id = (search_items[0].get("id") or {}).get("channelId")
            if not channel_id:
                return None
            payload = await self._get_json(
                _CHANNELS_URL,
                {"id": channel_id, "part": "statistics", "key": self._api_key},
            )
            if payload is None:
                return None
            items = payload.get("items") or []
        if not items:
            return None
        stats = items[0].get("statistics") or {}
        # hiddenSubscriberCount=true → count is None per YouTube policy.
        if stats.get("hiddenSubscriberCount"):
            return None
        return _coerce_count(stats.get("subscriberCount"))

    async def _get_json(self, url: str, params: dict) -> dict | None:
        try:
            if self._client is not None:
                response = await self._client.get(url, params=params, timeout=15.0)
            else:
                async with httpx.AsyncClient(timeout=15.0) as client:
                    response = await client.get(url, params=params)
            response.raise_for_status()
        except httpx.HTTPError:
            return None
        return response.json()


def _coerce_count(raw: object) -> float | None:
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None
