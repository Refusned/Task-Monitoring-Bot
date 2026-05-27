"""Tests for real public-counter metric providers."""

from __future__ import annotations

import httpx
import pytest

from models import Scenario
from verification.activity_metrics import (
    YouTubeMetricsProvider,
    extract_youtube_channel_lookup,
    extract_youtube_video_id,
)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        ("https://www.youtube.com/watch?v=dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        ("https://m.youtube.com/watch?v=dQw4w9WgXcQ&feature=share", "dQw4w9WgXcQ"),
        ("https://youtube.com/shorts/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        ("https://www.youtube.com/embed/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        ("https://youtu.be/dQw4w9WgXcQ?t=10", "dQw4w9WgXcQ"),
        ("https://example.com/watch?v=dQw4w9WgXcQ", None),
        ("https://youtube.com/watch?v=bad", None),
    ],
)
def test_extract_youtube_video_id(value: str, expected: str | None) -> None:
    assert extract_youtube_video_id(value) == expected


@pytest.mark.parametrize(
    ("value", "expected_kind", "expected_value"),
    [
        ("UCGOfWjBz7lK8A7hHapDSFQA", "id", "UCGOfWjBz7lK8A7hHapDSFQA"),
        ("https://youtube.com/channel/UCGOfWjBz7lK8A7hHapDSFQA", "id", "UCGOfWjBz7lK8A7hHapDSFQA"),
        ("@GoogleDevelopers", "forHandle", "GoogleDevelopers"),
        ("https://www.youtube.com/@GoogleDevelopers", "forHandle", "GoogleDevelopers"),
        ("https://youtube.com/user/GoogleDevelopers", "forUsername", "GoogleDevelopers"),
        ("https://youtube.com/c/GoogleDevelopers", None, None),
        ("https://example.com/@GoogleDevelopers", None, None),
    ],
)
def test_extract_youtube_channel_lookup(
    value: str,
    expected_kind: str | None,
    expected_value: str | None,
) -> None:
    lookup = extract_youtube_channel_lookup(value)
    if expected_kind is None:
        assert lookup is None
    else:
        assert lookup is not None
        assert lookup.kind == expected_kind
        assert lookup.value == expected_value


@pytest.mark.asyncio
async def test_youtube_metrics_provider_reads_like_count() -> None:
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(dict(request.url.params))
        return httpx.Response(
            200,
            json={"items": [{"statistics": {"likeCount": "123", "viewCount": "456"}}]},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        provider = YouTubeMetricsProvider("api-key", http_client=http_client)
        snapshot = await provider.measure(
            "https://youtube.com/watch?v=dQw4w9WgXcQ",
            Scenario.ACTIVITY_LIKE,
        )

    assert snapshot is not None
    assert snapshot.metric == "likeCount"
    assert snapshot.count == 123
    assert snapshot.source == "youtube_data_api"
    assert snapshot.raw_evidence["video_id"] == "dQw4w9WgXcQ"
    assert captured == {
        "part": "statistics",
        "id": "dQw4w9WgXcQ",
        "key": "api-key",
    }


@pytest.mark.asyncio
async def test_youtube_metrics_provider_reads_view_count() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"items": [{"statistics": {"likeCount": "123", "viewCount": "456"}}]},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        provider = YouTubeMetricsProvider("api-key", http_client=http_client)
        snapshot = await provider.measure("dQw4w9WgXcQ", Scenario.ACTIVITY_VIEW)

    assert snapshot is not None
    assert snapshot.metric == "viewCount"
    assert snapshot.count == 456


@pytest.mark.asyncio
async def test_youtube_metrics_provider_reads_subscriber_count() -> None:
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(dict(request.url.params))
        return httpx.Response(
            200,
            json={"items": [{"statistics": {"subscriberCount": "789"}}]},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        provider = YouTubeMetricsProvider("api-key", http_client=http_client)
        snapshot = await provider.measure(
            "https://youtube.com/@GoogleDevelopers",
            Scenario.ACTIVITY_SUBSCRIBE,
        )

    assert snapshot is not None
    assert snapshot.metric == "subscriberCount"
    assert snapshot.count == 789
    assert captured == {
        "part": "statistics",
        "forHandle": "GoogleDevelopers",
        "key": "api-key",
    }


@pytest.mark.asyncio
async def test_youtube_metrics_provider_ignores_non_youtube_targets() -> None:
    async with httpx.AsyncClient() as http_client:
        provider = YouTubeMetricsProvider("api-key", http_client=http_client)
        snapshot = await provider.measure("https://example.com/post", Scenario.ACTIVITY_VIEW)

    assert snapshot is None
