"""Yandex Metrica verifier for the TRAFFIC metric.

Reads visit counts from Metrica Stat API v1 (`/stat/v1/data`), filtering by
UTM source. Auth: OAuth token in the `Authorization: OAuth <token>` header.
Counter ID = the numeric Metrica counter (NOT the OAuth client id) — find it
at https://metrika.yandex.ru/list/?

How the count is computed:
  - We ask Metrica for `ym:s:visits` grouped by `ym:s:UTMSource`, over the
    last `MetricaConfig.window_days` days.
  - The verifier returns the sum of visits whose UTM source matches the
    platform shipped in the task (vk → "vk", telegram → "telegram", etc.).
  - The agent's `check_delta` consumer compares `current - baseline` against
    the order's expected quantity.

Caveat: Metrica only sees traffic that hit a page tagged with the right UTM
parameters. The bot's job at order-creation time is to make sure the target
URL carries `?utm_source={platform}&utm_medium=smm` (handled at the agent
layer when it composes the order target — not here).

Docs: https://yandex.ru/dev/metrika/ru/stat/data
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import httpx

from models import SourcePlatform, TaskType
from verification.base import PlatformVerifier

_STAT_URL = "https://api-metrika.yandex.ru/stat/v1/data"

# UTM-source values we expect on the target URL for each SourcePlatform.
# These are the canonical short forms; the bot adds them at order creation.
_UTM_SOURCE_FOR_PLATFORM: dict[SourcePlatform, str] = {
    SourcePlatform.VK: "vk",
    SourcePlatform.X: "twitter",       # X is still "twitter" in most UTM conventions
    SourcePlatform.YOUTUBE: "youtube",
    SourcePlatform.TELEGRAM: "telegram",
    SourcePlatform.DZEN: "dzen",
    SourcePlatform.PINTEREST: "pinterest",
}


class YandexMetricaVerifier(PlatformVerifier):
    """Counts visits per UTM source via Metrica Stat API.

    Supports `TaskType.TRAFFIC` on every SourcePlatform — Metrica is the
    one verifier that's source-platform-agnostic (it only cares what the
    UTM tag says).
    """

    name = "yandex_metrica"

    def __init__(
        self,
        counter_id: str,
        oauth_token: str,
        http_client: httpx.AsyncClient | None = None,
        *,
        window_days: int = 7,
    ) -> None:
        if not counter_id:
            raise ValueError(
                "YandexMetricaVerifier requires counter_id (numeric Metrica counter)"
            )
        if not oauth_token:
            raise ValueError(
                "YandexMetricaVerifier requires oauth_token (see scripts/yandex_oauth.py)"
            )
        self._counter_id = str(counter_id).strip()
        self._token = oauth_token.strip()
        self._client = http_client
        self._window_days = max(1, int(window_days))

    def supports(self, platform: SourcePlatform, metric: TaskType) -> bool:
        return metric == TaskType.TRAFFIC and platform in _UTM_SOURCE_FOR_PLATFORM

    async def fetch_metric(self, url: str, metric: TaskType) -> float | None:
        if metric != TaskType.TRAFFIC:
            return None
        # Resolve the expected UTM source by sniffing the URL: caller is
        # responsible for keeping `utm_source=...` on the target. We accept
        # `?utm_source=vk` exactly as written.
        utm_source = _parse_utm_source(url)
        if utm_source is None:
            # Without a UTM tag we can't filter — Metrica would over-count.
            # Caller should add `?utm_source={platform}` to the order target.
            return None
        today = datetime.now(UTC).date()
        date1 = today - timedelta(days=self._window_days)
        return await self._fetch_visits(url, date1, today)

    async def fetch_metric_since(
        self,
        url: str,
        metric: TaskType,
        captured_at: str,
    ) -> float | None:
        """Return visits in a fixed window starting at the snapshot date.

        Metrica's Stat API is date-granular here, so this intentionally avoids
        sliding-window deltas but may still include same-day pre-snapshot visits.
        Unique per-order UTM campaign tags would be needed for exact timestamp
        isolation.
        """
        if metric != TaskType.TRAFFIC:
            return None
        try:
            start = datetime.fromisoformat(captured_at).date()
        except ValueError:
            start = datetime.now(UTC).date()
        return await self._fetch_visits(url, start, datetime.now(UTC).date())

    async def _fetch_visits(self, url: str, date1: date, date2: date) -> float | None:
        utm_source = _parse_utm_source(url)
        if utm_source is None:
            return None
        params = {
            "ids": self._counter_id,
            "metrics": "ym:s:visits",
            "dimensions": "ym:s:UTMSource",
            "filters": f"ym:s:UTMSource=='{utm_source}'",
            "date1": _fmt_date(date1),
            "date2": _fmt_date(date2),
            "limit": "10",
            "accuracy": "full",
        }
        headers = {"Authorization": f"OAuth {self._token}"}
        try:
            if self._client is not None:
                response = await self._client.get(
                    _STAT_URL, params=params, headers=headers, timeout=15.0
                )
            else:
                async with httpx.AsyncClient(timeout=15.0) as client:
                    response = await client.get(_STAT_URL, params=params, headers=headers)
            response.raise_for_status()
        except httpx.HTTPError:
            return None
        payload = response.json()
        data_rows = payload.get("data") or []
        total = 0.0
        for row in data_rows:
            metrics = row.get("metrics") or []
            if metrics:
                try:
                    total += float(metrics[0])
                except (TypeError, ValueError):
                    continue
        return total


def _fmt_date(d: date) -> str:
    return d.strftime("%Y-%m-%d")


def _parse_utm_source(url: str) -> str | None:
    """Pull the `utm_source` value out of a URL's query string.

    We accept canonical short forms — anything else is normalized to lowercase.
    """
    from urllib.parse import parse_qs, urlparse

    try:
        parsed = urlparse(url)
    except (ValueError, TypeError):
        return None
    qs = parse_qs(parsed.query)
    values = qs.get("utm_source") or []
    if not values:
        return None
    return values[0].strip().lower() or None


def utm_source_for(platform: SourcePlatform) -> str | None:
    """Canonical UTM source value the bot stamps on a target URL when
    placing a traffic order. Returns None for unmapped platforms."""
    return _UTM_SOURCE_FOR_PLATFORM.get(platform)
