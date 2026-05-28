"""FastAPI app — tool endpoints for OpenClaw + dashboard + WebSocket.

Routes:
  POST /api/tools/get_quote
  POST /api/tools/get_balances
  POST /api/tools/get_topup_info
  POST /api/tools/capture_snapshot
  POST /api/tools/place_order
  POST /api/tools/check_order_status
  POST /api/tools/check_delta
  POST /api/tools/report
  GET  /dashboard
  GET  /api/state/balances
  GET  /api/state/orders
  GET  /api/state/events
  GET  /api/state/topups
  GET  /api/state/agent_log
  WS   /ws/live

Auth: /api/tools/* uses a dedicated agent Bearer token. Dashboard pages, state
endpoints, chat, and WebSocket use the dashboard cookie or dashboard Bearer
token; the LLM never receives that dashboard secret.

Run: `uvicorn app.main:app --host 127.0.0.1 --port 8000` (or via `python cli.py start`).
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import shlex
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qs

import httpx
from fastapi import (
    Cookie,
    Depends,
    FastAPI,
    HTTPException,
    Query,
    Request,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from adapters.base import Capability
from app.state import (
    AppState,
    build_app_state,
    cached_balance,
    claim_snapshot_for_order,
    create_topup_request,
    emit_event,
    get_snapshot,
    get_snapshot_for_order,
    list_pending_topups,
    recent_events,
    redact_secrets,
    save_snapshot,
)
from db.database import connect
from models import (
    OrderSpec,
    OrderStatus,
    Quote,
    SourcePlatform,
    TaskType,
    TopupInfo,
    VerificationVerdict,
    new_client_order_uuid,
    new_snapshot_id,
    task_type_to_scenario,
)
from orchestrator import persist_and_create_order
from verification.base import select_verifier

_TEMPLATES_DIR = Path(__file__).parent / "templates"
_STATIC_DIR = Path(__file__).parent / "static"


# ===== Lifespan: build state, optionally start scheduler =====


@asynccontextmanager
async def lifespan(app: FastAPI):
    state = await build_app_state()
    app.state.app_state = state
    # Auto-generate dashboard token if missing — print once to console.
    if not state.settings.dashboard_token:
        token = secrets.token_urlsafe(24)
        # Cannot mutate Settings (frozen at import), so attach to runtime state
        # and use that lookup. The user can also set DASHBOARD_TOKEN in .env.
        state.settings.__dict__["dashboard_token"] = token
        print(f"\n[dashboard] Generated session token: {token}")
        print(
            f"[dashboard] Open http://{state.settings.app_host}:"
            f"{state.settings.app_port}/dashboard and paste the token\n"
        )
    if (
        not state.settings.agent_tools_token
        or state.settings.agent_tools_token == state.settings.dashboard_token
    ):
        state.settings.__dict__["agent_tools_token"] = secrets.token_urlsafe(32)
        print("[agent] Generated runtime-only AGENT_TOOLS_TOKEN for OpenClaw tools")
    # Start scheduler (lives inside this process so it shares AppState).
    try:
        from scheduler.jobs import start_scheduler

        scheduler = start_scheduler(state)
        app.state.scheduler = scheduler
    except Exception as e:
        print(f"[scheduler] Failed to start: {e!r}")
        app.state.scheduler = None
    try:
        yield
    finally:
        sched = getattr(app.state, "scheduler", None)
        if sched is not None:
            try:
                sched.shutdown(wait=False)
            except Exception:
                pass
        # Close any verifier that owns long-lived connections (e.g. Telethon MTProto).
        for verifier in state.verifiers:
            close = getattr(verifier, "close", None)
            if close is not None:
                try:
                    await close()
                except Exception:
                    pass
        await state.http_client.aclose()


app = FastAPI(
    title="SMM Aggregator MCP-equivalent (HTTP tools for OpenClaw)",
    lifespan=lifespan,
)


if _STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))


# ===== Auth =====


def _get_state(request: Request) -> AppState:
    return request.app.state.app_state


async def require_tool_auth(request: Request) -> AppState:
    """Bearer auth for /api/tools/* — OpenClaw sends AGENT_TOOLS_TOKEN."""
    state: AppState = request.app.state.app_state
    expected = state.settings.agent_tools_token
    if not expected:
        raise HTTPException(status_code=503, detail="agent_tools_token not set on server")
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    if not secrets.compare_digest(auth[len("Bearer ") :], expected):
        raise HTTPException(status_code=403, detail="bad token")
    return state


async def require_dashboard_auth(
    request: Request,
    auth_token: str | None = Cookie(default=None),
) -> AppState:
    """Cookie or Authorization auth for dashboard/state endpoints."""
    state: AppState = request.app.state.app_state
    expected = state.settings.dashboard_token
    if not expected:
        raise HTTPException(status_code=503, detail="dashboard_token not set on server")
    auth = request.headers.get("Authorization", "")
    bearer = auth[len("Bearer ") :] if auth.startswith("Bearer ") else None
    candidate = bearer or auth_token
    if not candidate or not secrets.compare_digest(candidate, expected):
        raise HTTPException(status_code=403, detail="bad or missing token")
    return state


_TOOL_AUTH = Depends(require_tool_auth)
_DASHBOARD_AUTH = Depends(require_dashboard_auth)


# ===== Tool request/response schemas =====


class GetQuoteRequest(BaseModel):
    metric: TaskType
    platform: SourcePlatform
    quantity: int = Field(gt=0)
    target_url: str = Field(min_length=1)


class GetQuoteResponse(BaseModel):
    quotes: list[Quote]
    recommended_exchange: str | None
    lowest_price_exchange: str | None
    # Backward-compatible alias; historically this was actually the recommended
    # quote after confidence sorting.
    cheapest_exchange: str | None


class GetBalancesRequest(BaseModel):
    force_refresh: bool = False


class GetBalancesResponse(BaseModel):
    balances: list[dict]  # serialized ExchangeBalance + extras
    total: float
    currency: str = "RUB"


class GetTopupInfoRequest(BaseModel):
    exchange: str = Field(min_length=1)
    requested_amount: float | None = Field(default=None, ge=0)
    user_chat_id: int | None = None


class GetTopupInfoResponse(BaseModel):
    topup: TopupInfo
    topup_uuid: str  # persisted pending_topup id; scheduler watches it


class CaptureSnapshotRequest(BaseModel):
    platform: SourcePlatform
    target_url: str = Field(min_length=1)
    metric: TaskType


class CaptureSnapshotResponse(BaseModel):
    snapshot_id: str
    platform: SourcePlatform
    target_url: str
    metric: TaskType
    baseline_value: float
    captured_at: str
    verifier_used: str  # which verifier read this; "none" if no verifier supports
    # False means baseline is a placeholder and verification needs a human.
    verifier_available: bool = True
    verification_mode: str = "automated"


class PlaceOrderRequest(BaseModel):
    exchange: str = Field(min_length=1)
    metric: TaskType
    platform: SourcePlatform
    quantity: int = Field(gt=0)
    target_url: str = Field(min_length=1)
    max_cost: float = Field(gt=0)
    snapshot_id: str = Field(min_length=1)  # required: enforces baseline-first
    service_id: str | None = None
    user_chat_id: int | None = None
    allow_manual_verification: bool = False


class PlaceOrderResponse(BaseModel):
    order_uuid: str
    external_order_id: str
    exchange: str
    cost_actual: float
    status: str


class CheckOrderStatusRequest(BaseModel):
    order_uuid: str = Field(min_length=1)


class CheckOrderStatusResponse(BaseModel):
    order_uuid: str
    status: str  # canonical DB OrderStatus (creating/active/verifying/completed/failed/cancelled)
    exchange: str
    external_order_id: str | None
    cost_actual: float | None
    age_seconds: int
    raw_exchange_status: str | None
    exchange_status: str | None = None  # latest adapter readout: in_progress/completed/failed


class CheckDeltaRequest(BaseModel):
    order_uuid: str = Field(min_length=1)


class CheckDeltaResponse(BaseModel):
    order_uuid: str
    verdict: VerificationVerdict
    baseline_value: float
    current_value: float | None
    measured_delta: float | None
    expected_increase: float
    reason: str
    verifier_used: str


class ReportRequest(BaseModel):
    order_uuid: str | None = None
    summary_md: str = Field(min_length=1)
    user_chat_id: int | None = None  # OpenClaw can pass through


class ReportResponse(BaseModel):
    ack: bool
    event_id: int | None


class AgentChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    session_id: str = Field(
        default="dashboard",
        min_length=1,
        max_length=80,
        pattern=r"^[A-Za-z0-9_.:-]+$",
    )
    timeout_seconds: int | None = Field(default=None, ge=5, le=600)


class AgentChatResponse(BaseModel):
    reply: str
    session_id: str
    run_id: str | None = None
    status: str
    model: str | None = None
    duration_ms: int | None = None
    usage_total: int | None = None
    fallback_used: bool | None = None


async def _fetch_metric_for_snapshot(verifier, snap: dict, metric: TaskType) -> float | None:
    raw = snap.get("raw") if isinstance(snap.get("raw"), dict) else {}
    if (
        raw.get("counting_mode") == "fixed_window_from_snapshot"
        and hasattr(verifier, "fetch_metric_since")
    ):
        return await verifier.fetch_metric_since(snap["target_url"], metric, snap["captured_at"])
    return await verifier.fetch_metric(snap["target_url"], metric)


# ===== Tool endpoints =====


async def _collect_quotes(state: AppState, req: GetQuoteRequest) -> GetQuoteResponse:
    """Query every adapter for a quote. Shared by agent tools and dashboard ops.

    Filters out adapters that lack CREATE_ORDER (e.g. ipgold stub): there's no
    point ranking a quote we cannot fulfill. Sorts by (-confidence, total_price)
    so a real-catalog quote (0.9) wins over a mock quote (0.4) even if mock is
    nominally cheaper — placing on a mock-only adapter is a guaranteed failure
    at create_order (service_id absent in real catalogue).
    """
    names = list(state.adapters.keys())
    coros = [
        adapter.get_quote(req.metric, req.platform, req.quantity)
        for adapter in state.adapters.values()
    ]
    raw_results = await asyncio.gather(*coros, return_exceptions=True)
    quotes: list[Quote] = []
    for name, r in zip(names, raw_results, strict=False):
        if isinstance(r, Exception):
            await emit_event(
                state.settings,
                kind="quote_error",
                payload={"exchange": name, "error": repr(r)},
            )
            continue
        if r is None:
            continue
        adapter = state.adapters.get(name)
        if adapter is None or Capability.CREATE_ORDER not in adapter.capabilities():
            continue
        quotes.append(r)
    quotes.sort(key=lambda q: (-q.confidence, q.total_price))
    cheapest = min(quotes, key=lambda q: q.total_price).exchange if quotes else None
    recommended = quotes[0].exchange if quotes else None
    return GetQuoteResponse(
        quotes=quotes,
        recommended_exchange=recommended,
        lowest_price_exchange=cheapest,
        cheapest_exchange=recommended,
    )


def _json_from_openclaw_stdout(stdout: str) -> dict:
    """OpenClaw normally prints pure JSON with --json; this tolerates banners."""
    try:
        return json.loads(stdout)
    except json.JSONDecodeError:
        start = stdout.find("{")
        end = stdout.rfind("}")
        if start < 0 or end <= start:
            raise
        return json.loads(stdout[start : end + 1])


def _agent_response_from_openclaw(data: dict, session_id: str) -> AgentChatResponse:
    if isinstance(data.get("result"), dict):
        result = data["result"]
        status = str(data.get("status") or "unknown")
        run_id = data.get("runId")
    else:
        result = data
        status = str(data.get("status") or "ok")
        run_id = data.get("runId")
    meta = result.get("meta") if isinstance(result.get("meta"), dict) else {}
    payloads = result.get("payloads") if isinstance(result.get("payloads"), list) else []
    texts = [
        str(payload.get("text"))
        for payload in payloads
        if isinstance(payload, dict) and payload.get("text")
    ]
    reply = "\n\n".join(texts).strip()
    if not reply:
        reply = str(
            meta.get("finalAssistantVisibleText")
            or meta.get("finalAssistantRawText")
            or ""
        ).strip()
    if not reply:
        reply = str(data.get("summary") or "OpenClaw returned no visible text.")

    agent_meta = meta.get("agentMeta") if isinstance(meta.get("agentMeta"), dict) else {}
    usage = agent_meta.get("usage") if isinstance(agent_meta.get("usage"), dict) else {}
    trace = meta.get("executionTrace") if isinstance(meta.get("executionTrace"), dict) else {}
    return AgentChatResponse(
        reply=reply,
        session_id=session_id,
        run_id=run_id,
        status=status,
        model=agent_meta.get("model"),
        duration_ms=meta.get("durationMs"),
        usage_total=usage.get("total"),
        fallback_used=trace.get("fallbackUsed"),
    )


async def _run_openclaw_agent(
    state: AppState,
    message: str,
    session_id: str,
    timeout_seconds: int,
) -> AgentChatResponse:
    """Run agent with retries. If `openclaw_restart_command` is not configured,
    retrying same broken state is pointless — surface the original error fast."""
    attempts = max(1, state.settings.openclaw_agent_retries + 1)
    can_restart = bool(state.settings.openclaw_restart_command.strip())
    last_error = "OpenClaw agent failed"

    for attempt in range(1, attempts + 1):
        if state.settings.openclaw_preflight_enabled:
            ready = await _wait_for_openclaw_gateway(state, timeout_seconds=3)
            if not ready and can_restart:
                await _restart_openclaw_gateway(state, reason="preflight_failed")
                await _wait_for_openclaw_gateway(state, timeout_seconds=25)

        try:
            response = await _run_openclaw_agent_once(
                state,
                message,
                session_id,
                timeout_seconds,
            )
        except HTTPException as exc:
            last_error = str(exc.detail)
            if attempt < attempts and _openclaw_error_is_retryable(last_error) and can_restart:
                await _restart_openclaw_gateway(state, reason="agent_error")
                continue
            raise

        if response.reply.strip() and response.reply != "OpenClaw returned no visible text.":
            return response
        last_error = response.reply
        if attempt < attempts and can_restart:
            await _restart_openclaw_gateway(state, reason="empty_agent_reply")
            continue
        return response

    raise HTTPException(502, last_error)


async def _run_openclaw_agent_once(
    state: AppState,
    message: str,
    session_id: str,
    timeout_seconds: int,
) -> AgentChatResponse:
    binary = state.settings.openclaw_binary or "openclaw"
    workspace = Path(state.settings.openclaw_workspace)
    if not workspace.is_absolute():
        workspace = Path.cwd() / workspace
    cwd = workspace if workspace.exists() else Path.cwd()
    env = os.environ.copy()
    env["APP_BASE_URL"] = f"http://127.0.0.1:{state.settings.app_port}"
    env["AGENT_TOOLS_TOKEN"] = state.settings.agent_tools_token
    if state.settings.ollama_api_key:
        env["OLLAMA_API_KEY"] = state.settings.ollama_api_key
    if state.settings.ollama_base_url:
        env["OLLAMA_BASE_URL"] = state.settings.ollama_base_url
    if state.settings.ollama_model:
        env["OLLAMA_MODEL"] = state.settings.ollama_model
    if state.settings.ollama_fallback_model:
        env["OLLAMA_FALLBACK_MODEL"] = state.settings.ollama_fallback_model
    env.pop("DASHBOARD_TOKEN", None)

    command = [binary]
    if state.settings.openclaw_profile.strip():
        command.extend(["--profile", state.settings.openclaw_profile.strip()])
    command.extend([
        "agent",
        "--session-id",
        session_id,
        "--message",
        message,
        "--json",
        "--timeout",
        str(timeout_seconds),
    ])

    try:
        proc = await asyncio.create_subprocess_exec(
            *command,
            cwd=str(cwd),
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise HTTPException(503, f"OpenClaw binary not found: {binary}") from exc
    try:
        stdout_b, stderr_b = await asyncio.wait_for(
            proc.communicate(),
            timeout=timeout_seconds + 20,
        )
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise HTTPException(504, "OpenClaw agent timed out") from None

    stdout = stdout_b.decode(errors="replace")
    stderr = stderr_b.decode(errors="replace")
    if proc.returncode != 0:
        detail = str(
            redact_secrets(state.settings, (stderr or stdout or "OpenClaw agent failed").strip())
        )[-2000:]
        raise HTTPException(502, detail)
    try:
        data = _json_from_openclaw_stdout(stdout)
    except json.JSONDecodeError as exc:
        raise HTTPException(502, f"OpenClaw returned invalid JSON: {exc}") from exc
    response = _agent_response_from_openclaw(data, session_id)
    return response.model_copy(
        update={"reply": redact_secrets(state.settings, response.reply)}
    )


def _openclaw_error_is_retryable(detail: str) -> bool:
    retryable_fragments = (
        "GatewayTransportError",
        "ECONNREFUSED",
        "gateway closed",
        "OpenClaw returned invalid JSON",
        "timed out",
        "no visible text",
    )
    return any(fragment in detail for fragment in retryable_fragments)


async def _openclaw_gateway_ready(state: AppState) -> bool:
    try:
        response = await state.http_client.get(
            state.settings.openclaw_gateway_http_url,
            timeout=3.0,
        )
    except Exception:
        return False
    return response.status_code < 500


async def _wait_for_openclaw_gateway(state: AppState, timeout_seconds: int) -> bool:
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    while True:
        if await _openclaw_gateway_ready(state):
            return True
        if asyncio.get_running_loop().time() >= deadline:
            return False
        await asyncio.sleep(1)


async def _restart_openclaw_gateway(state: AppState, reason: str) -> None:
    command = state.settings.openclaw_restart_command.strip()
    if not command:
        ev = await emit_event(
            state.settings,
            kind="openclaw_restart_skipped",
            payload={"reason": reason, "detail": "OPENCLAW_RESTART_COMMAND is empty"},
        )
        await state.event_bus.publish(ev)
        return

    ev = await emit_event(
        state.settings,
        kind="openclaw_restart_requested",
        payload={"reason": reason, "command": command},
    )
    await state.event_bus.publish(ev)
    try:
        proc = await asyncio.create_subprocess_exec(
            *shlex.split(command),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=30)
    except Exception as exc:
        err_ev = await emit_event(
            state.settings,
            kind="openclaw_restart_failed",
            payload={"reason": reason, "error": repr(exc)},
        )
        await state.event_bus.publish(err_ev)
        return

    if proc.returncode != 0:
        err_ev = await emit_event(
            state.settings,
            kind="openclaw_restart_failed",
            payload={
                "reason": reason,
                "returncode": proc.returncode,
                "stdout": stdout_b.decode(errors="replace")[-1000:],
                "stderr": stderr_b.decode(errors="replace")[-1000:],
            },
        )
        await state.event_bus.publish(err_ev)


@app.post("/api/tools/get_quote", response_model=GetQuoteResponse)
async def tool_get_quote(
    req: GetQuoteRequest,
    state: AppState = _TOOL_AUTH,
):
    """Parallel-query every adapter for a quote; return sorted ascending by total price."""
    quote_response = await _collect_quotes(state, req)
    cheapest = quote_response.cheapest_exchange

    ev = await emit_event(
        state.settings,
        kind="tool_call",
        payload={
            "tool": "get_quote",
            "args": req.model_dump(mode="json"),
            "result": {
                "quotes": [q.model_dump(mode="json") for q in quote_response.quotes],
                "recommended": quote_response.recommended_exchange,
                "lowest_price": quote_response.lowest_price_exchange,
                "cheapest": cheapest,
            },
        },
    )
    await state.event_bus.publish(ev)
    return quote_response


@app.post("/api/tools/get_balances", response_model=GetBalancesResponse)
async def tool_get_balances(
    req: GetBalancesRequest,
    state: AppState = _TOOL_AUTH,
):
    """Return per-exchange balances. force_refresh bypasses the cache for all."""
    names = list(state.adapters.keys())
    coros = [
        cached_balance(state.settings, adapter, force_refresh=req.force_refresh)
        for adapter in state.adapters.values()
    ]
    results = await asyncio.gather(*coros, return_exceptions=True)
    balances: list[dict] = []
    total = 0.0
    for name, r in zip(names, results, strict=False):
        if isinstance(r, Exception):
            balances.append(
                {
                    "exchange": name,
                    "amount": 0.0,
                    "currency": "RUB",
                    "fetched_at": datetime.now(UTC).isoformat(timespec="seconds"),
                    "stale": True,
                    "error": repr(r),
                }
            )
            continue
        balances.append(
            {
                "exchange": r.exchange,
                "amount": r.amount,
                "currency": r.currency,
                "fetched_at": r.fetched_at.isoformat(timespec="seconds"),
                "stale": r.stale,
                "no_api": r.no_api,
            }
        )
        if not r.stale and not r.no_api:
            total += r.amount

    ev = await emit_event(
        state.settings,
        kind="tool_call",
        payload={
            "tool": "get_balances",
            "args": req.model_dump(mode="json"),
            "result": {"balances": balances, "total": total},
        },
    )
    await state.event_bus.publish(ev)
    return GetBalancesResponse(balances=balances, total=total, currency="RUB")


@app.post("/api/tools/get_topup_info", response_model=GetTopupInfoResponse)
async def tool_get_topup_info(
    req: GetTopupInfoRequest,
    state: AppState = _TOOL_AUTH,
):
    """Persist a topup_request row + return TopupInfo to the agent."""
    adapter = state.adapters.get(req.exchange)
    if adapter is None:
        raise HTTPException(404, f"exchange {req.exchange!r} not configured on this server")
    info = await adapter.get_topup_info()
    if info is None:
        raise HTTPException(501, f"{req.exchange} adapter does not implement get_topup_info")
    # Money safety: requested_amount below min_amount is meaningless — the exchange
    # would reject the deposit, and the scheduler's recheck_balance_after_topup
    # would otherwise falsely mark the request resolved on any tiny stale balance.
    if req.requested_amount is not None and req.requested_amount < info.min_amount:
        raise HTTPException(
            422,
            f"requested_amount {req.requested_amount} is below {req.exchange} "
            f"min_amount {info.min_amount} {info.currency}",
        )
    requested = req.requested_amount or info.min_amount
    import uuid as _uuid

    topup_uuid = f"topup_{_uuid.uuid4().hex[:12]}"
    await create_topup_request(
        state.settings,
        topup_uuid=topup_uuid,
        exchange=info.exchange,
        requested_amount=requested,
        currency=info.currency,
        topup_url=info.topup_url,
        user_chat_id=req.user_chat_id,
        note=info.notes,
    )
    ev = await emit_event(
        state.settings,
        kind="tool_call",
        payload={
            "tool": "get_topup_info",
            "args": req.model_dump(mode="json"),
            "result": {"topup_uuid": topup_uuid, "info": info.model_dump(mode="json")},
        },
    )
    await state.event_bus.publish(ev)
    return GetTopupInfoResponse(topup=info, topup_uuid=topup_uuid)


@app.post("/api/tools/capture_snapshot", response_model=CaptureSnapshotResponse)
async def tool_capture_snapshot(
    req: CaptureSnapshotRequest,
    state: AppState = _TOOL_AUTH,
):
    """Read current metric value via the matching verifier; persist snapshot.

    If no verifier is wired for (platform, metric), still persist a placeholder
    snapshot (baseline=0.0, verifier="none") so that `place_order` can proceed —
    the resulting `verifications` row will end up `NEEDS_HUMAN_REVIEW`, which
    the dashboard surfaces for manual decision. This unblocks the VK/TG/X/etc.
    flow on smmcode (real catalogue) without weakening money-safety: the order
    is still gated by snapshot-first + spend limits, just verified by a human.
    """
    verifier = select_verifier(state.verifiers, req.platform, req.metric)
    verifier_available = verifier is not None
    captured_at = datetime.now(UTC).isoformat(timespec="seconds")
    verification_mode = "automated" if verifier_available else "manual_only"
    raw = {
        "verifier": verifier.name if verifier else "none",
        "verifier_available": verifier_available,
        "verification_mode": verification_mode,
    }
    if verifier is not None:
        if verifier.name == "yandex_metrica" and hasattr(verifier, "fetch_metric_since"):
            probe_value = await verifier.fetch_metric(req.target_url, req.metric)
            if probe_value is None:
                raise HTTPException(
                    502,
                    f"{verifier.name} failed to read {req.metric.value} for {req.target_url!r}",
                )
            value = 0.0
            raw.update(
                {
                    "counting_mode": "fixed_window_from_snapshot",
                    "probe_window_value": probe_value,
                }
            )
        else:
            value = await verifier.fetch_metric(req.target_url, req.metric)
            if value is None:
                raise HTTPException(
                    502,
                    f"{verifier.name} failed to read {req.metric.value} for {req.target_url!r}",
                )
        if value is None:
            raise HTTPException(
                502,
                f"{verifier.name} failed to read {req.metric.value} for {req.target_url!r}",
            )
        verifier_name = verifier.name
    else:
        value = 0.0
        verifier_name = "none"
        # Surface a clear signal on the live feed so the operator sees we placed
        # an order whose delivery they'll need to confirm by eye.
        await emit_event(
            state.settings,
            kind="unverifiable_snapshot",
            payload={
                "platform": req.platform.value,
                "metric": req.metric.value,
                "target_url": req.target_url,
                "note": "no verifier wired — verification will need human review",
            },
        )
    snap_id = new_snapshot_id()
    await save_snapshot(
        state.settings,
        snapshot_id=snap_id,
        platform=req.platform.value,
        target_url=req.target_url,
        metric=req.metric.value,
        baseline_value=value,
        raw=raw,
    )
    ev = await emit_event(
        state.settings,
        kind="tool_call",
        payload={
            "tool": "capture_snapshot",
            "args": req.model_dump(mode="json"),
            "result": {
                "snapshot_id": snap_id,
                "baseline_value": value,
                "verifier": verifier_name,
                "verifier_available": verifier_available,
                "verification_mode": verification_mode,
            },
        },
    )
    await state.event_bus.publish(ev)
    return CaptureSnapshotResponse(
        snapshot_id=snap_id,
        platform=req.platform,
        target_url=req.target_url,
        metric=req.metric,
        baseline_value=value,
        captured_at=captured_at,
        verifier_used=verifier_name,
        verifier_available=verifier_available,
        verification_mode=verification_mode,
    )


@app.post("/api/tools/place_order", response_model=PlaceOrderResponse)
async def tool_place_order(
    req: PlaceOrderRequest,
    state: AppState = _TOOL_AUTH,
):
    """Idempotent placement via the existing orchestrator (C1).
    Refuses if `snapshot_id` is not on disk (forces baseline-first protocol)."""
    snap = await get_snapshot(state.settings, req.snapshot_id)
    if snap is None:
        raise HTTPException(
            422,
            f"snapshot_id {req.snapshot_id!r} not found — call capture_snapshot first",
        )
    if snap.get("order_uuid"):
        raise HTTPException(422, f"snapshot_id {req.snapshot_id!r} was already used")
    mismatches = {
        "platform": (snap.get("platform"), req.platform.value),
        "metric": (snap.get("metric"), req.metric.value),
        "target_url": (snap.get("target_url"), req.target_url),
    }
    bad = {key: pair for key, pair in mismatches.items() if pair[0] != pair[1]}
    if bad:
        raise HTTPException(422, f"snapshot does not match requested order: {bad}")
    snap_raw = snap.get("raw") if isinstance(snap.get("raw"), dict) else {}
    if (
        snap_raw.get("verifier_available") is False
        and not req.allow_manual_verification
    ):
        raise HTTPException(
            422,
            "snapshot has no automated verifier; pass allow_manual_verification=true "
            "only after an operator accepts manual verification for this order",
        )
    adapter = state.adapters.get(req.exchange)
    if adapter is None:
        raise HTTPException(404, f"exchange {req.exchange!r} not configured")
    caps = adapter.capabilities()
    if Capability.CREATE_ORDER not in caps:
        raise HTTPException(
            422,
            f"{req.exchange} does not support CREATE_ORDER (capability missing)",
        )
    # Bridge: TaskType → Scenario for the legacy OrderSpec.
    scenario = task_type_to_scenario(req.metric)
    try:
        spec = OrderSpec(
            scenario=scenario,
            exchange=req.exchange,
            target=req.target_url,
            quantity=req.quantity,
            service_id=req.service_id or snap.get("service_id"),
            source_platform=req.platform,
            max_cost=req.max_cost,
        )
    except Exception as exc:
        raise HTTPException(422, f"spec validation failed: {exc!r}") from exc

    order_uuid = new_client_order_uuid()
    if not await claim_snapshot_for_order(
        state.settings,
        req.snapshot_id,
        order_uuid,
        platform=req.platform.value,
        target_url=req.target_url,
        metric=req.metric.value,
    ):
        raise HTTPException(409, "snapshot was already claimed or no longer matches the order")

    try:
        order_uuid, external_id, cost = await persist_and_create_order(
            state.settings,
            adapter,
            spec,
            actor=f"agent:user_chat={req.user_chat_id or 'unknown'}",
            client_uuid=order_uuid,
        )
    except (ValueError, RuntimeError) as exc:
        failure_payload = {
            "exchange": req.exchange,
            "error": repr(exc),
            "args": req.model_dump(mode="json"),
        }
        await emit_event(state.settings, kind="place_order_failed", payload=failure_payload)
        # Day 4: alert admins immediately — failed placements often mean the
        # whole exchange is down and they need to switch.
        try:
            from scheduler.jobs import push_failure_alert

            await push_failure_alert(state, "place_order_failed", failure_payload)
        except Exception:
            pass  # alert is best-effort
        raise HTTPException(502, f"place_order failed: {exc}") from exc

    ev = await emit_event(
        state.settings,
        kind="tool_call",
        payload={
            "tool": "place_order",
            "args": req.model_dump(mode="json"),
            "result": {
                "order_uuid": order_uuid,
                "external_order_id": external_id,
                "cost_actual": cost,
                "snapshot_id": req.snapshot_id,
            },
            "user_chat_id": req.user_chat_id,
        },
        order_uuid=order_uuid,
    )
    await state.event_bus.publish(ev)
    return PlaceOrderResponse(
        order_uuid=order_uuid,
        external_order_id=external_id,
        exchange=req.exchange,
        cost_actual=cost,
        status=OrderStatus.ACTIVE.value,
    )


@app.post("/api/tools/check_order_status", response_model=CheckOrderStatusResponse)
async def tool_check_order_status(
    req: CheckOrderStatusRequest,
    state: AppState = _TOOL_AUTH,
):
    """Re-poll the exchange for the order status (live, not cached)."""
    async with connect(state.settings) as conn:
        cursor = await conn.execute(
            """
            SELECT exchange, external_order_id, status, cost_actual, raw_exchange_status,
                   created_at
            FROM orders WHERE client_order_uuid = ?
            """,
            (req.order_uuid,),
        )
        row = await cursor.fetchone()
    if row is None:
        raise HTTPException(404, f"order {req.order_uuid!r} not found")
    exchange = row["exchange"]
    external_id = row["external_order_id"]
    db_status = row["status"]  # canonical OrderStatus from local SQLite
    cost_actual = row["cost_actual"]
    raw = row["raw_exchange_status"]
    age = (datetime.now(UTC) - datetime.fromisoformat(row["created_at"])).total_seconds()

    # Refresh status if possible (only when external_id is known and adapter supports).
    # Adapter taxonomy ('in_progress'/'completed'/'failed') is reported separately
    # as `exchange_status`; the canonical `status` always reflects what's in our
    # DB (creating/active/verifying/completed/failed/cancelled). Scheduler owns
    # the DB transition — this endpoint never writes.
    exchange_status: str | None = None
    if external_id and exchange in state.adapters:
        adapter = state.adapters[exchange]
        if Capability.GET_ORDER_STATUS in adapter.capabilities():
            try:
                exchange_status = await adapter.get_order_status(external_id)
            except Exception as exc:
                exchange_status = None
                raw = (raw or "") + f"\n[poll error] {exc!r}"

    ev = await emit_event(
        state.settings,
        kind="tool_call",
        payload={
            "tool": "check_order_status",
            "args": req.model_dump(mode="json"),
            "result": {
                "status": db_status,
                "exchange_status": exchange_status,
                "external_id": external_id,
            },
        },
        order_uuid=req.order_uuid,
    )
    await state.event_bus.publish(ev)
    return CheckOrderStatusResponse(
        order_uuid=req.order_uuid,
        status=db_status,
        exchange=exchange,
        external_order_id=external_id,
        cost_actual=cost_actual,
        age_seconds=int(age),
        raw_exchange_status=raw,
        exchange_status=exchange_status,
    )


@app.post("/api/tools/check_delta", response_model=CheckDeltaResponse)
async def tool_check_delta(
    req: CheckDeltaRequest,
    state: AppState = _TOOL_AUTH,
):
    """Read current metric value, compute delta vs baseline. Persists a
    `verifications` row + returns the verdict."""
    snap = await get_snapshot_for_order(state.settings, req.order_uuid)
    if snap is None:
        raise HTTPException(404, f"no snapshot for order {req.order_uuid!r}")
    async with connect(state.settings) as conn:
        cursor = await conn.execute(
            "SELECT quantity, spec_json FROM orders WHERE client_order_uuid = ?",
            (req.order_uuid,),
        )
        row = await cursor.fetchone()
    if row is None:
        raise HTTPException(404, f"order {req.order_uuid!r} not found")
    expected = float(row["quantity"])
    try:
        platform_str = snap["platform"]
        metric_str = snap["metric"]
        platform = SourcePlatform(platform_str)
        metric = TaskType(metric_str)
    except ValueError as exc:
        raise HTTPException(500, f"corrupt snapshot enum: {exc}") from exc
    verifier = select_verifier(state.verifiers, platform, metric)
    verifier_name = verifier.name if verifier else "none"
    baseline_value = float(snap["baseline_value"])
    if verifier is None:
        verdict = VerificationVerdict.NEEDS_HUMAN_REVIEW
        reason = "no verifier wired for this platform/metric"
        current = None
        delta = None
    else:
        current = await _fetch_metric_for_snapshot(verifier, snap, metric)
        if current is None:
            verdict = VerificationVerdict.NEEDS_HUMAN_REVIEW
            reason = "verifier returned no value (creator-hidden, deleted, or transient error)"
            delta = None
        else:
            delta = current - baseline_value
            # Upper bound rules out organic-growth false-positives on popular URLs
            # (a 10-like order on Rick Roll naturally sees +180 likes/hour and would
            # otherwise auto_pass without delivery). 3x expected is the trip-wire.
            organic_ceiling = expected * 3.0
            if expected * 0.8 <= delta <= organic_ceiling:
                verdict = VerificationVerdict.AUTO_PASS
                reason = f"delta {delta:.0f} within [80%, 300%] of expected {expected:.0f}"
            elif delta > organic_ceiling:
                verdict = VerificationVerdict.NEEDS_HUMAN_REVIEW
                reason = (
                    f"delta {delta:.0f} > 300% of expected {expected:.0f} — "
                    "likely organic noise, admin to confirm"
                )
            elif delta < expected * 0.2:
                verdict = VerificationVerdict.FAIL
                reason = f"delta {delta:.0f} < 20% of expected {expected:.0f}"
            else:
                verdict = VerificationVerdict.NEEDS_HUMAN_REVIEW
                reason = f"delta {delta:.0f} between 20%–80% of expected {expected:.0f}"

    # Persist verification row.
    import uuid as _uuid

    verification_uuid = f"verif_{_uuid.uuid4().hex[:12]}"
    async with connect(state.settings) as conn:
        await conn.execute(
            """
            INSERT INTO verifications
            (verification_uuid, order_uuid, verdict, measured, expected, reason,
             raw_evidence_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                verification_uuid,
                req.order_uuid,
                verdict.value,
                current,
                expected,
                reason,
                json.dumps(
                    {
                        "baseline": baseline_value,
                        "current": current,
                        "verifier": verifier_name,
                        "snapshot_id": snap["snapshot_id"],
                    },
                    ensure_ascii=False,
                    default=str,
                ),
                datetime.now(UTC).isoformat(timespec="seconds"),
            ),
        )
        await conn.commit()

    ev = await emit_event(
        state.settings,
        kind="tool_call",
        payload={
            "tool": "check_delta",
            "args": req.model_dump(mode="json"),
            "result": {
                "verdict": verdict.value,
                "baseline": baseline_value,
                "current": current,
                "delta": delta,
                "expected": expected,
                "reason": reason,
            },
        },
        order_uuid=req.order_uuid,
    )
    await state.event_bus.publish(ev)
    return CheckDeltaResponse(
        order_uuid=req.order_uuid,
        verdict=verdict,
        baseline_value=baseline_value,
        current_value=current,
        measured_delta=delta,
        expected_increase=expected,
        reason=reason,
        verifier_used=verifier_name,
    )


@app.post("/api/tools/report", response_model=ReportResponse)
async def tool_report(
    req: ReportRequest,
    state: AppState = _TOOL_AUTH,
):
    """Record a user-visible report event. OpenClaw's Telegram channel will
    independently put the summary in the chat (it's the LLM's own response);
    the scheduler-side path may call this directly to surface verification
    results that arrive after the LLM session ends.
    """
    ev = await emit_event(
        state.settings,
        kind="report",
        payload={
            "summary_md": req.summary_md,
            "user_chat_id": req.user_chat_id,
        },
        order_uuid=req.order_uuid,
    )
    await state.event_bus.publish(ev)
    # If user_chat_id + telegram_bot_token, push a Telegram message right here
    # (scheduler does the same for autonomous reports).
    if req.user_chat_id and state.settings.telegram_bot_token:
        try:
            await _push_telegram(
                state.http_client,
                state.settings.telegram_bot_token,
                req.user_chat_id,
                req.summary_md,
            )
        except Exception as exc:
            await emit_event(
                state.settings,
                kind="telegram_push_failed",
                payload={"error": repr(exc), "chat_id": req.user_chat_id},
            )
    return ReportResponse(ack=True, event_id=ev["event_id"])


async def _push_telegram(client, bot_token: str, chat_id: int, text: str) -> None:
    """Direct sendMessage call. Markdown rendering on by default; bot must be
    a regular bot account (OpenClaw might use its own bot, ours is separate).
    """
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}
    response = await client.post(url, json=payload, timeout=15.0)
    response.raise_for_status()


# ===== Dashboard pages =====


@app.get("/", response_class=HTMLResponse)
async def root_redirect():
    return RedirectResponse("/dashboard", status_code=307)


@app.get("/healthz")
async def healthz(request: Request):
    state: AppState = request.app.state.app_state
    db_ok = True
    try:
        async with connect(state.settings) as conn:
            await conn.execute("SELECT 1")
    except Exception:
        db_ok = False
    openclaw_ok = await _openclaw_gateway_ready(state)
    scheduler_ok = getattr(request.app.state, "scheduler", None) is not None
    ok = db_ok and openclaw_ok and scheduler_ok
    return {
        "ok": ok,
        "backend": True,
        "db": db_ok,
        "openclaw_gateway": openclaw_ok,
        "scheduler": scheduler_ok,
        "adapters": sorted(state.adapters.keys()),
        "verifiers": [v.name for v in state.verifiers],
    }


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard_page(
    request: Request,
    auth_token: str | None = Cookie(default=None),
):
    state: AppState = request.app.state.app_state
    expected = state.settings.dashboard_token
    if not expected:
        return HTMLResponse(
            "<h1>Dashboard token not set — restart the app to auto-generate.</h1>",
            status_code=503,
        )
    candidate = auth_token
    if not candidate or not secrets.compare_digest(candidate, expected):
        return HTMLResponse(
            """<!doctype html>
            <html><body style="font-family: system-ui; padding: 40px;
            max-width: 600px; margin: auto;">
            <h1>SMM Aggregator Dashboard</h1>
            <form method="post" action="/dashboard/login" style="display: grid; gap: 12px;">
              <label for="token">Dashboard token</label>
              <input id="token" name="token" type="password" autocomplete="current-password"
                     style="font: inherit; padding: 10px; border: 1px solid #ddd;">
              <button style="font: inherit; padding: 10px;">Open dashboard</button>
            </form>
            </body></html>""",
            status_code=401,
        )
    # Normal render.
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {"adapters": list(state.adapters.keys())},
    )


@app.post("/dashboard/login")
async def dashboard_login(request: Request):
    state: AppState = request.app.state.app_state
    expected = state.settings.dashboard_token
    if not expected:
        return HTMLResponse("dashboard_token not set on server", status_code=503)
    body = (await request.body()).decode(errors="replace")
    token = (parse_qs(body).get("token") or [""])[0]
    if not token or not secrets.compare_digest(token, expected):
        return HTMLResponse("bad or missing token", status_code=403)
    resp = RedirectResponse("/dashboard", status_code=303)
    resp.set_cookie(
        "auth_token",
        token,
        httponly=True,
        samesite="lax",
        max_age=60 * 60 * 12,
    )
    return resp


# ===== Dashboard JSON endpoints =====


@app.get("/api/state/balances")
async def state_balances(
    state: AppState = _DASHBOARD_AUTH,
    force_refresh: bool = Query(default=False),
):
    names = list(state.adapters.keys())
    coros = [
        cached_balance(state.settings, adapter, force_refresh=force_refresh)
        for adapter in state.adapters.values()
    ]
    results = await asyncio.gather(*coros, return_exceptions=True)
    balances: list[dict] = []
    total = 0.0
    for name, r in zip(names, results, strict=False):
        adapter = state.adapters[name]
        info = await adapter.get_topup_info()
        if isinstance(r, Exception):
            balances.append(
                {
                    "exchange": name,
                    "amount": 0.0,
                    "currency": "RUB",
                    "fetched_at": datetime.now(UTC).isoformat(timespec="seconds"),
                    "stale": True,
                    "error": repr(r),
                    "topup_url": info.topup_url if info else None,
                }
            )
            continue
        balances.append(
            {
                "exchange": r.exchange,
                "amount": r.amount,
                "currency": r.currency,
                "fetched_at": r.fetched_at.isoformat(timespec="seconds"),
                "stale": r.stale,
                "no_api": r.no_api,
                "topup_url": info.topup_url if info else None,
                "min_amount": info.min_amount if info else None,
            }
        )
        if not r.stale and not r.no_api:
            total += r.amount
    return {"balances": balances, "total": total, "currency": "RUB"}


@app.get("/api/state/orders")
async def state_orders(state: AppState = _DASHBOARD_AUTH):
    async with connect(state.settings) as conn:
        cursor = await conn.execute(
            """
            SELECT client_order_uuid, exchange, scenario, target, quantity,
                   max_cost, cost_actual, status, raw_exchange_status,
                   created_at, updated_at, external_order_id, source_platform
            FROM orders ORDER BY created_at DESC LIMIT 100
            """,
        )
        rows = await cursor.fetchall()
    return {
        "orders": [
            {
                "order_uuid": row["client_order_uuid"],
                "exchange": row["exchange"],
                "platform": row["source_platform"],
                "scenario": row["scenario"],
                "target": row["target"],
                "quantity": row["quantity"],
                "max_cost": row["max_cost"],
                "cost_actual": row["cost_actual"],
                "status": row["status"],
                "raw_exchange_status": row["raw_exchange_status"],
                "external_order_id": row["external_order_id"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            }
            for row in rows
        ]
    }


@app.get("/api/state/orders/{order_uuid}")
async def state_order_detail(
    order_uuid: str,
    state: AppState = _DASHBOARD_AUTH,
):
    """Drill-down for one order: audit log + verifications + submissions + the
    place_order_failed event (if any), so the dashboard can explain failures."""
    async with connect(state.settings) as conn:
        order_cursor = await conn.execute(
            """
            SELECT client_order_uuid, exchange, scenario, target, quantity,
                   max_cost, cost_actual, status, raw_exchange_status,
                   created_at, updated_at, external_order_id, source_platform,
                   service_id
            FROM orders WHERE client_order_uuid = ?
            """,
            (order_uuid,),
        )
        order_row = await order_cursor.fetchone()
        if order_row is None:
            raise HTTPException(404, f"order {order_uuid!r} not found")

        audit_cursor = await conn.execute(
            """
            SELECT occurred_at, actor, event, details_json
            FROM audit_log WHERE order_uuid = ?
            ORDER BY audit_id ASC
            """,
            (order_uuid,),
        )
        audit_rows = await audit_cursor.fetchall()

        verif_cursor = await conn.execute(
            """
            SELECT verdict, measured, expected, reason, raw_evidence_json, created_at
            FROM verifications WHERE order_uuid = ?
            ORDER BY created_at DESC
            """,
            (order_uuid,),
        )
        verif_rows = await verif_cursor.fetchall()

        sub_cursor = await conn.execute(
            """
            SELECT submission_uuid, external_submission_id, executor_hint,
                   status, evidence, created_at, updated_at
            FROM submissions WHERE order_uuid = ?
            ORDER BY created_at DESC
            """,
            (order_uuid,),
        )
        sub_rows = await sub_cursor.fetchall()

        events_cursor = await conn.execute(
            """
            SELECT event_id, occurred_at, kind, payload_json
            FROM agent_events WHERE order_uuid = ?
            ORDER BY event_id DESC LIMIT 50
            """,
            (order_uuid,),
        )
        events_rows = await events_cursor.fetchall()

    def parse_json(text: str | None) -> dict | None:
        if not text:
            return None
        import json as _json
        try:
            return _json.loads(text)
        except (ValueError, TypeError):
            return None

    return {
        "order": {
            "order_uuid": order_row["client_order_uuid"],
            "exchange": order_row["exchange"],
            "platform": order_row["source_platform"],
            "scenario": order_row["scenario"],
            "target": order_row["target"],
            "quantity": order_row["quantity"],
            "max_cost": order_row["max_cost"],
            "cost_actual": order_row["cost_actual"],
            "status": order_row["status"],
            "raw_exchange_status": order_row["raw_exchange_status"],
            "external_order_id": order_row["external_order_id"],
            "created_at": order_row["created_at"],
            "updated_at": order_row["updated_at"],
            "service_id": order_row["service_id"],
        },
        "audit_log": [
            {
                "occurred_at": r["occurred_at"],
                "actor": r["actor"],
                "event": r["event"],
                "details": parse_json(r["details_json"]),
            }
            for r in audit_rows
        ],
        "verifications": [
            {
                "verdict": r["verdict"],
                "measured": r["measured"],
                "expected": r["expected"],
                "reason": r["reason"],
                "evidence": parse_json(r["raw_evidence_json"]),
                "created_at": r["created_at"],
            }
            for r in verif_rows
        ],
        "submissions": [
            {
                "submission_uuid": r["submission_uuid"],
                "external_submission_id": r["external_submission_id"],
                "executor_hint": r["executor_hint"],
                "status": r["status"],
                "evidence": r["evidence"],
                "created_at": r["created_at"],
                "updated_at": r["updated_at"],
            }
            for r in sub_rows
        ],
        "events": [
            {
                "event_id": r["event_id"],
                "occurred_at": r["occurred_at"],
                "kind": r["kind"],
                "payload": parse_json(r["payload_json"]) or {},
            }
            for r in events_rows
        ],
    }


@app.get("/api/state/events")
async def state_events(
    state: AppState = _DASHBOARD_AUTH,
    limit: int = Query(default=100, ge=1, le=500),
):
    events = await recent_events(state.settings, limit=limit)
    return {"events": events}


@app.get("/api/state/topups")
async def state_topups(state: AppState = _DASHBOARD_AUTH):
    return {"topups": await list_pending_topups(state.settings)}


# --- Yandex OAuth bootstrap (Metrica) ----------------------------------------


class YandexOAuthExchangeRequest(BaseModel):
    code: str = Field(min_length=1, max_length=200)


@app.get("/api/oauth/yandex/url")
async def oauth_yandex_url(state: AppState = _DASHBOARD_AUTH):
    """Return the authorize URL the operator should click.

    The page will display a one-time `code`; POST it back to /api/oauth/yandex/exchange.
    """
    from scripts.yandex_oauth import authorize_url

    client_id = state.settings.yandex_oauth_client_id
    if not client_id:
        raise HTTPException(503, "YANDEX_OAUTH_CLIENT_ID is not set in .env")
    return {"authorize_url": authorize_url(client_id), "client_id": client_id}


@app.post("/api/oauth/yandex/exchange")
async def oauth_yandex_exchange(
    req: YandexOAuthExchangeRequest,
    state: AppState = _DASHBOARD_AUTH,
):
    """Exchange a one-time `code` for an OAuth token. The caller is responsible
    for persisting METRICA_OAUTH_TOKEN into .env and restarting the backend —
    we don't write .env from the runtime as a safety measure."""
    from scripts.yandex_oauth import exchange_code

    client_id = state.settings.yandex_oauth_client_id
    client_secret = state.settings.yandex_oauth_client_secret
    if not client_id or not client_secret:
        raise HTTPException(503, "YANDEX_OAUTH_CLIENT_ID/SECRET not set in .env")
    try:
        data = await exchange_code(state.http_client, client_id, client_secret, req.code)
    except httpx.HTTPStatusError as exc:
        raise HTTPException(
            400,
            {
                "error": "yandex_oauth_rejected",
                "status_code": exc.response.status_code,
            },
        ) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(502, {"error": "yandex_oauth_transport_error"}) from exc
    if "access_token" not in data:
        raise HTTPException(400, "Yandex response did not include access_token")
    await emit_event(
        state.settings,
        kind="yandex_oauth_exchanged",
        payload={
            "has_refresh": bool(data.get("refresh_token")),
            "expires_in": data.get("expires_in"),
        },
    )
    return data


# --- Admin order finalization (manual accept/reject for verifying orders) ---


class AdminOrderDecisionRequest(BaseModel):
    reason: str = Field(default="", max_length=500)


@app.post("/api/orders/{order_uuid}/admin_accept")
async def admin_accept_order(
    order_uuid: str,
    payload: AdminOrderDecisionRequest | None = None,
    state: AppState = _DASHBOARD_AUTH,
):
    """Manual closure for orders in `verifying` state — operator confirms
    delivery against their own eyes (used when the verifier returned
    NEEDS_HUMAN_REVIEW). Writes a verifications row + transitions to COMPLETED
    + writes a report_rows entry for the weekly Sheets push.
    """
    reason = (payload.reason if payload is not None else "") or "manual admin accept"
    return await _admin_finalize_order(state, order_uuid, OrderStatus.COMPLETED, reason)


@app.post("/api/orders/{order_uuid}/admin_reject")
async def admin_reject_order(
    order_uuid: str,
    payload: AdminOrderDecisionRequest | None = None,
    state: AppState = _DASHBOARD_AUTH,
):
    """Manual closure → FAILED. Operator rejected the delivery."""
    reason = (payload.reason if payload is not None else "") or "manual admin reject"
    return await _admin_finalize_order(state, order_uuid, OrderStatus.FAILED, reason)


async def _admin_finalize_order(
    state: AppState,
    order_uuid: str,
    target: OrderStatus,
    reason: str,
) -> dict:
    from db.database import (
        append_audit,
        claim_order_status,
        latest_verification_measured,
        record_report_row,
    )
    from scheduler.jobs import _write_report_row_for  # noqa: F401 — for typing parity

    if target not in (OrderStatus.COMPLETED, OrderStatus.FAILED):
        raise HTTPException(422, f"invalid target status {target.value!r}")

    async with connect(state.settings) as conn:
        cursor = await conn.execute(
            "SELECT status, exchange, source_platform, quantity, cost_actual "
            "FROM orders WHERE client_order_uuid = ?",
            (order_uuid,),
        )
        row = await cursor.fetchone()
        if row is None:
            raise HTTPException(404, f"order {order_uuid!r} not found")
        if row["status"] != OrderStatus.VERIFYING.value:
            raise HTTPException(
                409,
                f"order is in status {row['status']!r}, can only finalize from 'verifying'",
            )

        claimed = await claim_order_status(
            conn,
            order_uuid,
            target=target,
            allowed_from=(OrderStatus.VERIFYING,),
        )
        if not claimed:
            raise HTTPException(409, "order moved by another worker — refresh and retry")
        await append_audit(
            conn,
            actor="dashboard:admin",
            event=f"order_admin_{target.value}",
            order_uuid=order_uuid,
            details={"reason": reason},
        )

    # Verification row that captures the admin decision.
    import uuid as _uuid

    verdict = (
        VerificationVerdict.AUTO_PASS if target == OrderStatus.COMPLETED
        else VerificationVerdict.FAIL
    )
    async with connect(state.settings) as conn:
        prior_measured = await latest_verification_measured(conn, order_uuid)
        await conn.execute(
            """
            INSERT INTO verifications
            (verification_uuid, order_uuid, verdict, measured, expected, reason,
             raw_evidence_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                f"verif_{_uuid.uuid4().hex[:12]}",
                order_uuid,
                verdict.value,
                prior_measured,
                float(row["quantity"]),
                f"admin: {reason}",
                "{}",
                datetime.now(UTC).isoformat(timespec="seconds"),
            ),
        )
        # Day 3 idempotency: also write a report_rows entry so weekly Sheets
        # picks it up. Partial unique index on order_uuid keeps duplicates out.
        await record_report_row(
            conn,
            order_uuid=order_uuid,
            source_platform=row["source_platform"] or "",
            exchange=row["exchange"],
            ordered_count=int(row["quantity"]),
            actual_count=int(prior_measured) if prior_measured is not None else None,
            cost=row["cost_actual"],
            status=target.value,
        )
        await conn.commit()

    ev = await emit_event(
        state.settings,
        kind="order_status_changed",
        payload={"from": "verifying", "to": target.value, "by": "admin", "reason": reason},
        order_uuid=order_uuid,
    )
    await state.event_bus.publish(ev)
    return {"ok": True, "order_uuid": order_uuid, "new_status": target.value, "reason": reason}


# --- Google Sheets reporting -------------------------------------------------


@app.post("/api/sheets/test")
async def sheets_test(state: AppState = _DASHBOARD_AUTH):
    """Smoke-test connectivity to the operator's spreadsheet. Returns metadata
    (title, tab, header row) on success — no writes."""
    if state.sheets_writer is None:
        raise HTTPException(
            503,
            "Sheets writer is not configured. Set GOOGLE_SHEETS_CREDENTIALS_FILE "
            "and GOOGLE_SHEETS_SPREADSHEET_ID in .env, then restart.",
        )
    try:
        meta = await state.sheets_writer.health_check()
    except Exception as exc:
        raise HTTPException(502, f"Sheets test failed: {exc!r}") from exc
    return {"ok": True, **meta}


@app.post("/api/sheets/sync_now")
async def sheets_sync_now(state: AppState = _DASHBOARD_AUTH):
    """Manually trigger the weekly push (normally Mon 10:00 MSK). Idempotent —
    pushes only rows that haven't been synced yet."""
    from scheduler.jobs import push_report_rows_to_sheets

    result = await push_report_rows_to_sheets(state)
    return result


@app.post("/api/state/topups/{topup_uuid}/cancel")
async def state_topup_cancel(
    topup_uuid: str,
    state: AppState = _DASHBOARD_AUTH,
):
    """Operator-initiated dismissal of a pending topup_request.

    Idempotent: cancelling an already-resolved/cancelled row is a no-op.
    """
    from app.state import resolve_topup_request

    await resolve_topup_request(state.settings, topup_uuid, "cancelled")
    ev = await emit_event(
        state.settings,
        kind="topup_cancelled",
        payload={"topup_uuid": topup_uuid, "by": "dashboard"},
    )
    await state.event_bus.publish(ev)
    return {"ok": True, "topup_uuid": topup_uuid}


@app.post("/api/ops/quote", response_model=GetQuoteResponse)
async def dashboard_quote(
    req: GetQuoteRequest,
    state: AppState = _DASHBOARD_AUTH,
):
    quote_response = await _collect_quotes(state, req)
    ev = await emit_event(
        state.settings,
        kind="dashboard_quote",
        payload={
            "args": req.model_dump(mode="json"),
            "result": {
                "quotes": [q.model_dump(mode="json") for q in quote_response.quotes],
                "recommended": quote_response.recommended_exchange,
                "lowest_price": quote_response.lowest_price_exchange,
                "cheapest": quote_response.cheapest_exchange,
            },
        },
    )
    await state.event_bus.publish(ev)
    return quote_response


@app.post("/api/agent/chat", response_model=AgentChatResponse)
async def dashboard_agent_chat(
    req: AgentChatRequest,
    state: AppState = _DASHBOARD_AUTH,
):
    user_ev = await emit_event(
        state.settings,
        kind="dashboard_chat_user",
        payload={"message": req.message, "session_id": req.session_id},
    )
    await state.event_bus.publish(user_ev)

    timeout = req.timeout_seconds or state.settings.openclaw_agent_timeout_seconds
    try:
        response = await _run_openclaw_agent(state, req.message, req.session_id, timeout)
    except HTTPException as exc:
        err_ev = await emit_event(
            state.settings,
            kind="dashboard_chat_error",
            payload={
                "message": req.message,
                "session_id": req.session_id,
                "error": str(exc.detail),
            },
        )
        await state.event_bus.publish(err_ev)
        raise

    agent_ev = await emit_event(
        state.settings,
        kind="dashboard_chat_agent",
        payload=response.model_dump(mode="json"),
    )
    await state.event_bus.publish(agent_ev)
    return response


# ===== WebSocket =====


@app.websocket("/ws/live")
async def ws_live(ws: WebSocket):
    state: AppState = ws.app.state.app_state
    expected = state.settings.dashboard_token
    candidate = ws.cookies.get("auth_token")
    if not expected or not candidate or not secrets.compare_digest(candidate, expected):
        await ws.close(code=status.WS_1008_POLICY_VIOLATION)
        return
    await ws.accept()
    queue = await state.event_bus.subscribe()
    try:
        # Push the last 50 historical events on connect so the dashboard
        # always has context even if the page was reloaded.
        recent = await recent_events(state.settings, limit=50)
        await ws.send_json({"kind": "history", "events": recent})
        while True:
            event = await queue.get()
            await ws.send_json({"kind": "event", "event": event})
    except (WebSocketDisconnect, asyncio.CancelledError):
        pass
    finally:
        await state.event_bus.unsubscribe(queue)
