"""Shared runtime state for the FastAPI app.

Built once at FastAPI lifespan startup, torn down at shutdown:
- `AppState.http_client`: a single httpx.AsyncClient shared by all adapters and
  the YouTube verifier (connection pooling, lower latency).
- `AppState.adapters`: registry keyed by exchange name. Missing API keys outside
  DRY_RUN cause that adapter to be skipped — the tool layer returns it as
  "exchange not configured" rather than crashing the whole app.
- `AppState.verifiers`: PlatformVerifier instances (only YouTube real in MVP).
- `AppState.event_bus`: in-memory publish-subscribe for the WebSocket dashboard,
  *additive* over the durable `agent_events` SQLite table — on reload the
  dashboard replays the last N rows from the table before subscribing live.
- `AppState.balance_cache`: best-effort balance cache backed by
  `exchange_balance_cache` table with configurable TTL.
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from adapters.advego import AdvegoAdapter
from adapters.base import ExchangeAdapter
from adapters.ipgold import IpgoldAdapter
from adapters.prskill import PrSkillAdapter
from adapters.smmcode import SmmcodeAdapter
from adapters.unu import UnuAdapter
from config import Settings, get_settings
from db.database import connect, init_db
from models import ExchangeBalance
from verification.base import PlatformVerifier, build_verifiers_from_settings


@dataclass
class AppState:
    settings: Settings
    http_client: httpx.AsyncClient
    adapters: dict[str, ExchangeAdapter] = field(default_factory=dict)
    verifiers: list[PlatformVerifier] = field(default_factory=list)
    event_bus: EventBus = field(default=None)  # type: ignore[assignment]
    sheets_writer: object | None = None  # reporting.sheets.SheetsWriter or None


class EventBus:
    """In-memory pub/sub for the dashboard WebSocket.

    Each subscriber gets an asyncio.Queue. `publish` is fire-and-forget — if a
    subscriber's queue is full (slow client) the event is dropped for that
    subscriber only; durable history lives in agent_events table.
    """

    def __init__(self, max_queue: int = 100) -> None:
        self._subscribers: set[asyncio.Queue[dict]] = set()
        self._max_queue = max_queue
        self._lock = asyncio.Lock()

    async def subscribe(self) -> asyncio.Queue[dict]:
        q: asyncio.Queue[dict] = asyncio.Queue(maxsize=self._max_queue)
        async with self._lock:
            self._subscribers.add(q)
        return q

    async def unsubscribe(self, q: asyncio.Queue[dict]) -> None:
        async with self._lock:
            self._subscribers.discard(q)

    async def publish(self, event: dict) -> None:
        # Snapshot under lock to avoid mutation-during-iter.
        async with self._lock:
            subs = list(self._subscribers)
        for q in subs:
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                # Slow consumer — drop oldest, push newest. Better than blocking.
                try:
                    q.get_nowait()
                    q.put_nowait(event)
                except (asyncio.QueueEmpty, asyncio.QueueFull):
                    pass


_SECRET_FIELD_NAMES = (
    "dashboard_token",
    "agent_tools_token",
    "metrica_oauth_token",
    "ollama_api_key",
    "telegram_bot_token",
    "smmcode_api_key",
    "prskill_api_key",
    "unu_api_key",
    "advego_api_token",
    "ipgold_api_key",
    "yandex_oauth_client_secret",
)

_TOKEN_ASSIGNMENT_RE = re.compile(
    r"(?i)\b([A-Z0-9_]*(?:TOKEN|SECRET|API_KEY)[A-Z0-9_]*)\s*[:=]\s*"
    r"([`'\"]?)([A-Za-z0-9._~+/=-]{12,})\2"
)


def redact_secrets(settings: Settings, value: Any) -> Any:
    """Return `value` with known runtime secrets removed.

    This is deliberately applied before writing durable `agent_events` rows and
    before returning OpenClaw replies to the dashboard. It handles exact secret
    values from settings and generic TOKEN/SECRET/API_KEY assignments.
    """
    secrets_to_redact: list[tuple[str, str]] = []
    for name in _SECRET_FIELD_NAMES:
        secret = str(getattr(settings, name, "") or "")
        if len(secret) >= 8:
            secrets_to_redact.append((name.upper(), secret))

    def redact_text(text: str) -> str:
        out = text
        for label, secret in secrets_to_redact:
            out = out.replace(secret, f"[REDACTED_{label}]")
        return _TOKEN_ASSIGNMENT_RE.sub(lambda m: f"{m.group(1)}: [REDACTED]", out)

    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        return {k: redact_secrets(settings, v) for k, v in value.items()}
    if isinstance(value, list):
        return [redact_secrets(settings, item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_secrets(settings, item) for item in value)
    return value


def _safe_construct(ctor, *args, **kwargs) -> ExchangeAdapter | None:
    try:
        return ctor(*args, **kwargs)
    except (ValueError, TypeError):
        return None


def build_adapters(
    settings: Settings,
    http_client: httpx.AsyncClient,
) -> dict[str, ExchangeAdapter]:
    """Construct adapters from settings. Adapters without credentials are
    omitted (outside DRY_RUN). In DRY_RUN we accept a placeholder so the
    smart-selection demo can still call get_quote on every adapter.

    Note: missing-credential adapters are skipped silently; the tool layer
    surfaces "exchange not configured" in get_balances responses for visibility.
    """
    out: dict[str, ExchangeAdapter] = {}
    placeholder = "DRY_RUN_PLACEHOLDER"

    def key(real: str) -> str:
        if real:
            return real
        if settings.dry_run:
            return placeholder
        return ""

    # smmcode
    a = _safe_construct(SmmcodeAdapter, key(settings.smmcode_api_key), http_client)
    if a:
        out["smmcode"] = a

    # prskill
    a = _safe_construct(PrSkillAdapter, key(settings.prskill_api_key), http_client)
    if a:
        out["prskill"] = a

    # unu
    a = _safe_construct(UnuAdapter, key(settings.unu_api_key), http_client)
    if a:
        out["unu"] = a

    # advego — needs default_campaign_id from settings (else create_order fails).
    a = _safe_construct(
        AdvegoAdapter,
        key(settings.advego_api_token),
        http_client,
        default_campaign_id=settings.advego_default_campaign_id,
    )
    if a:
        out["advego"] = a

    # ipgold — stub accepts empty key
    a = _safe_construct(IpgoldAdapter, settings.ipgold_api_key, http_client)
    if a:
        out["ipgold"] = a

    return out


async def build_app_state() -> AppState:
    """Construct registries + http client; ensure DB schema. Returns the state
    blob mounted on the FastAPI app's `state` attribute."""
    settings = get_settings()
    await init_db(settings)
    http_client = httpx.AsyncClient(timeout=30.0)
    adapters = build_adapters(settings, http_client)
    verifiers = build_verifiers_from_settings(http_client)
    sheets_writer = _build_sheets_writer(settings)
    return AppState(
        settings=settings,
        http_client=http_client,
        adapters=adapters,
        verifiers=verifiers,
        event_bus=EventBus(),
        sheets_writer=sheets_writer,
    )


def _build_sheets_writer(settings: Settings):
    """Construct SheetsWriter if both credentials path AND spreadsheet id are
    configured. Bad creds / missing file is logged but NEVER crashes startup —
    the weekly job and /api/sheets/* endpoints surface the issue cleanly."""
    if not settings.google_sheets_credentials_file or not settings.google_sheets_spreadsheet_id:
        return None
    try:
        from reporting.sheets import SheetsWriter

        return SheetsWriter(
            credentials_path=settings.google_sheets_credentials_file,
            spreadsheet_id=settings.google_sheets_spreadsheet_id,
        )
    except Exception as exc:
        print(f"[sheets] disabled: {exc!r}")
        return None


# ---------- agent_events: durable feed for the dashboard ----------


async def emit_event(
    settings: Settings,
    kind: str,
    payload: dict,
    order_uuid: str | None = None,
) -> dict:
    """Persist an agent_event row and return it ready-to-publish dict.
    The caller is responsible for AppState.event_bus.publish(...) if needed —
    keeping that side-effect outside makes this helper trivially callable from
    scheduler code that doesn't hold an AppState reference."""
    now = datetime.now(UTC).isoformat(timespec="seconds")
    safe_payload = redact_secrets(settings, payload)
    event_payload_json = json.dumps(safe_payload, ensure_ascii=False, default=str)
    async with connect(settings) as conn:
        cursor = await conn.execute(
            """
            INSERT INTO agent_events (occurred_at, kind, payload_json, order_uuid)
            VALUES (?, ?, ?, ?)
            """,
            (now, kind, event_payload_json, order_uuid),
        )
        await conn.commit()
        event_id = cursor.lastrowid
    return {
        "event_id": event_id,
        "occurred_at": now,
        "kind": kind,
        "payload": safe_payload,
        "order_uuid": order_uuid,
    }


async def recent_events(settings: Settings, limit: int = 200) -> list[dict]:
    async with connect(settings) as conn:
        cursor = await conn.execute(
            """
            SELECT event_id, occurred_at, kind, payload_json, order_uuid
            FROM agent_events ORDER BY event_id DESC LIMIT ?
            """,
            (limit,),
        )
        rows = await cursor.fetchall()
    out: list[dict] = []
    for row in rows:
        try:
            payload = json.loads(row["payload_json"])
        except (json.JSONDecodeError, TypeError):
            payload = {}
        out.append(
            {
                "event_id": row["event_id"],
                "occurred_at": row["occurred_at"],
                "kind": row["kind"],
                "payload": payload,
                "order_uuid": row["order_uuid"],
            }
        )
    # Caller wants oldest-first for chronological feed rendering.
    return list(reversed(out))


# ---------- balance cache ----------


async def cached_balance(
    settings: Settings,
    adapter: ExchangeAdapter,
    force_refresh: bool = False,
) -> ExchangeBalance:
    """Read balance from cache if young; otherwise call adapter.get_balance()
    and write through. On adapter failure with a stale cache row, return the
    stale value flagged `stale=True` so the LLM/UI can decide what to do.
    """
    ttl = timedelta(seconds=settings.balance_cache_ttl_seconds)
    now = datetime.now(UTC)

    async with connect(settings) as conn:
        cursor = await conn.execute(
            "SELECT amount, currency, fetched_at FROM exchange_balance_cache WHERE exchange = ?",
            (adapter.name,),
        )
        row = await cursor.fetchone()

    cached_amount: float | None = None
    cached_currency = "RUB"
    cached_fetched_at: datetime | None = None
    if row is not None:
        cached_amount = float(row["amount"])
        cached_currency = row["currency"]
        try:
            cached_fetched_at = datetime.fromisoformat(row["fetched_at"])
        except ValueError:
            cached_fetched_at = None

    fresh = (
        cached_amount is not None
        and cached_fetched_at is not None
        and now - cached_fetched_at < ttl
    )
    if fresh and not force_refresh:
        assert cached_amount is not None and cached_fetched_at is not None
        return ExchangeBalance(
            exchange=adapter.name,
            amount=cached_amount,
            currency=cached_currency,
            fetched_at=cached_fetched_at,
            stale=False,
        )

    # Refresh
    try:
        raw_amount = await adapter.get_balance()
    except Exception:
        if cached_amount is not None and cached_fetched_at is not None:
            return ExchangeBalance(
                exchange=adapter.name,
                amount=cached_amount,
                currency=cached_currency,
                fetched_at=cached_fetched_at,
                stale=True,
            )
        return ExchangeBalance(
            exchange=adapter.name,
            amount=0.0,
            currency="RUB",
            fetched_at=now,
            stale=True,
        )

    # Adapter returning None = exchange's public API has no balance method.
    # Surface as `no_api` (not "stale") so the dashboard shows an honest
    # plate instead of a misleading "устарел" warning.
    if raw_amount is None:
        return ExchangeBalance(
            exchange=adapter.name,
            amount=0.0,
            currency="RUB",
            fetched_at=now,
            stale=False,
            no_api=True,
        )

    amount = float(raw_amount)

    iso_now = now.isoformat(timespec="seconds")
    async with connect(settings) as conn:
        await conn.execute(
            """
            INSERT INTO exchange_balance_cache (exchange, amount, currency, fetched_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(exchange) DO UPDATE SET
                amount = excluded.amount,
                currency = excluded.currency,
                fetched_at = excluded.fetched_at
            """,
            (adapter.name, amount, "RUB", iso_now),
        )
        await conn.commit()
    return ExchangeBalance(
        exchange=adapter.name,
        amount=amount,
        currency="RUB",
        fetched_at=now,
        stale=False,
    )


# ---------- snapshot store ----------


async def save_snapshot(
    settings: Settings,
    snapshot_id: str,
    platform: str,
    target_url: str,
    metric: str,
    baseline_value: float,
    raw: dict[str, Any],
) -> None:
    async with connect(settings) as conn:
        await conn.execute(
            """
            INSERT INTO metric_snapshots
            (snapshot_id, platform, target_url, metric, baseline_value, captured_at, raw_json)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                snapshot_id,
                platform,
                target_url,
                metric,
                baseline_value,
                datetime.now(UTC).isoformat(timespec="seconds"),
                json.dumps(raw, ensure_ascii=False, default=str),
            ),
        )
        await conn.commit()


async def claim_snapshot_for_order(
    settings: Settings,
    snapshot_id: str,
    order_uuid: str,
    *,
    platform: str,
    target_url: str,
    metric: str,
) -> bool:
    """Single-use snapshot claim for snapshot-first order placement."""
    async with connect(settings) as conn:
        cursor = await conn.execute(
            """
            UPDATE metric_snapshots
            SET order_uuid = ?
            WHERE snapshot_id = ?
              AND order_uuid IS NULL
              AND platform = ?
              AND target_url = ?
              AND metric = ?
            """,
            (order_uuid, snapshot_id, platform, target_url, metric),
        )
        await conn.commit()
    return cursor.rowcount == 1


async def link_snapshot_to_order(settings: Settings, snapshot_id: str, order_uuid: str) -> bool:
    async with connect(settings) as conn:
        cursor = await conn.execute(
            """
            UPDATE metric_snapshots SET order_uuid = ?
            WHERE snapshot_id = ? AND order_uuid IS NULL
            """,
            (order_uuid, snapshot_id),
        )
        await conn.commit()
    return cursor.rowcount == 1


async def get_snapshot(settings: Settings, snapshot_id: str) -> dict | None:
    async with connect(settings) as conn:
        cursor = await conn.execute(
            """
            SELECT snapshot_id, platform, target_url, metric, baseline_value,
                   captured_at, raw_json, order_uuid
            FROM metric_snapshots WHERE snapshot_id = ?
            """,
            (snapshot_id,),
        )
        row = await cursor.fetchone()
    if row is None:
        return None
    try:
        raw = json.loads(row["raw_json"]) if row["raw_json"] else {}
    except (json.JSONDecodeError, TypeError):
        raw = {}
    return {
        "snapshot_id": row["snapshot_id"],
        "platform": row["platform"],
        "target_url": row["target_url"],
        "metric": row["metric"],
        "baseline_value": float(row["baseline_value"]),
        "captured_at": row["captured_at"],
        "raw": raw,
        "order_uuid": row["order_uuid"],
    }


async def get_snapshot_for_order(settings: Settings, order_uuid: str) -> dict | None:
    async with connect(settings) as conn:
        cursor = await conn.execute(
            """
            SELECT snapshot_id, platform, target_url, metric, baseline_value, captured_at, raw_json
            FROM metric_snapshots WHERE order_uuid = ? ORDER BY captured_at DESC LIMIT 1
            """,
            (order_uuid,),
        )
        row = await cursor.fetchone()
    if row is None:
        return None
    try:
        raw = json.loads(row["raw_json"]) if row["raw_json"] else {}
    except (json.JSONDecodeError, TypeError):
        raw = {}
    return {
        "snapshot_id": row["snapshot_id"],
        "platform": row["platform"],
        "target_url": row["target_url"],
        "metric": row["metric"],
        "baseline_value": float(row["baseline_value"]),
        "captured_at": row["captured_at"],
        "raw": raw,
    }


# ---------- topup_requests ----------


async def create_topup_request(
    settings: Settings,
    topup_uuid: str,
    exchange: str,
    requested_amount: float,
    currency: str,
    topup_url: str,
    user_chat_id: int | None,
    note: str,
) -> None:
    async with connect(settings) as conn:
        await conn.execute(
            """
            INSERT INTO topup_requests
            (topup_uuid, exchange, requested_amount, currency, topup_url,
             status, created_at, user_chat_id, note)
            VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, ?)
            """,
            (
                topup_uuid,
                exchange,
                requested_amount,
                currency,
                topup_url,
                datetime.now(UTC).isoformat(timespec="seconds"),
                user_chat_id,
                note,
            ),
        )
        await conn.commit()


async def list_pending_topups(settings: Settings) -> list[dict]:
    async with connect(settings) as conn:
        cursor = await conn.execute(
            """
            SELECT topup_uuid, exchange, requested_amount, currency, topup_url,
                   status, created_at, resolved_at, user_chat_id, note
            FROM topup_requests
            WHERE status = 'pending'
            ORDER BY created_at DESC
            """,
        )
        rows = await cursor.fetchall()
    return [
        {
            "topup_uuid": row["topup_uuid"],
            "exchange": row["exchange"],
            "requested_amount": float(row["requested_amount"]),
            "currency": row["currency"],
            "topup_url": row["topup_url"],
            "status": row["status"],
            "created_at": row["created_at"],
            "resolved_at": row["resolved_at"],
            "user_chat_id": row["user_chat_id"],
            "note": row["note"],
        }
        for row in rows
    ]


async def resolve_topup_request(settings: Settings, topup_uuid: str, status: str) -> None:
    async with connect(settings) as conn:
        await conn.execute(
            """
            UPDATE topup_requests SET status = ?, resolved_at = ?
            WHERE topup_uuid = ? AND status = 'pending'
            """,
            (status, datetime.now(UTC).isoformat(timespec="seconds"), topup_uuid),
        )
        await conn.commit()
