"""FastAPI dashboard for browser-based bot operations."""
# ruff: noqa: E501

from __future__ import annotations

import hmac
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel, Field

from adapters.base import Capability, ExchangeAdapter
from autopilot.ollama import OllamaPlanner
from autopilot.runner import AutopilotRunner, format_autopilot_result
from cli import _build_adapters, build_adapter
from config import Settings, get_settings
from db.database import connect, init_db
from orchestrator import Orchestrator
from reporting.sheets import preview_weekly_rows
from verification.activity_metrics import build_activity_metrics_provider

DashboardSettingsFactory = Callable[[], Settings]
AdapterFactory = Callable[[Settings, httpx.AsyncClient], dict[str, ExchangeAdapter]]

_BALANCE_EXCHANGES: tuple[tuple[str, str], ...] = (
    ("smmcode", "SMMCODE_API_KEY"),
    ("prskill", "PRSKILL_API_KEY"),
    ("unu", "UNU_API_KEY"),
    ("advego", "ADVEGO_API_TOKEN"),
    ("ipgold", "IPGOLD_API_KEY"),
)


class AutopilotRequest(BaseModel):
    """Browser request for goal planning or execution."""

    goal: str = Field(min_length=1)
    execute: bool = False


class RejectRequest(BaseModel):
    """Manual rejection payload."""

    reason: str = "Возвращено администратором на доработку"


def create_app(
    settings_factory: DashboardSettingsFactory = get_settings,
    adapter_factory: AdapterFactory | None = None,
) -> FastAPI:
    """Create the dashboard app.

    `settings_factory` is called per request so tests and reloads can override
    runtime configuration without leaking global state.
    """

    app = FastAPI(title="Task Monitoring Bot Dashboard")

    async def require_dashboard_access(request: Request) -> None:
        settings = settings_factory()
        token = settings.web_dashboard_token
        if not token:
            return
        supplied = _extract_token(request)
        if not supplied or not hmac.compare_digest(supplied, token):
            raise HTTPException(status_code=401, detail="dashboard token required")

    def get_adapters(
        settings: Settings,
        http_client: httpx.AsyncClient,
    ) -> dict[str, ExchangeAdapter]:
        if adapter_factory is not None:
            return adapter_factory(settings, http_client)
        return _build_adapters(settings, dry_run=settings.dry_run, http_client=http_client)

    async def with_orchestrator(
        action: Callable[[Settings, Orchestrator], Awaitable[Any]],
    ) -> Any:
        settings = settings_factory()
        await init_db(settings)
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as http_client:
            adapters = get_adapters(settings, http_client)
            return await action(settings, Orchestrator(settings, adapters))

    @app.get("/", response_class=HTMLResponse)
    async def index() -> HTMLResponse:
        return HTMLResponse(_DASHBOARD_HTML)

    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon() -> Response:
        svg = (
            "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'>"
            "<rect width='64' height='64' rx='12' fill='#168a5b'/>"
            "<text x='32' y='40' font-size='24' text-anchor='middle' "
            "font-family='Arial' font-weight='700' fill='white'>TM</text>"
            "</svg>"
        )
        return Response(svg, media_type="image/svg+xml")

    @app.get("/api/overview", dependencies=[Depends(require_dashboard_access)])
    async def api_overview() -> dict[str, Any]:
        settings = settings_factory()
        await init_db(settings)
        async with connect(settings) as conn:
            order_counts = await _counts_by(conn, "orders", "status")
            submission_counts = await _counts_by(conn, "submissions", "status")
            spend_today = await _scalar_float(
                conn,
                "SELECT COALESCE(SUM(cost_actual), 0) AS v "
                "FROM orders WHERE date(created_at) = date('now')",
            )
            spend_total = await _scalar_float(
                conn,
                "SELECT COALESCE(SUM(cost_actual), 0) AS v FROM orders",
            )
            active_orders = await _scalar_int(
                conn,
                "SELECT COUNT(*) AS v FROM orders "
                "WHERE status IN ('creating','active','verifying')",
            )
            awaiting_review = await _scalar_int(
                conn,
                "SELECT COUNT(*) AS v FROM submissions WHERE status = 'awaiting_admin'",
            )
            recent_orders = await _fetch_all(
                conn,
                "SELECT client_order_uuid, external_order_id, status, exchange, scenario, "
                "target, quantity, cost_actual, created_at, updated_at "
                "FROM orders ORDER BY created_at DESC LIMIT 8",
            )
            recent_events = await _fetch_all(
                conn,
                "SELECT occurred_at, actor, event, order_uuid, submission_uuid "
                "FROM audit_log ORDER BY audit_id DESC LIMIT 10",
            )
        return {
            "mode": "DRY_RUN" if settings.dry_run else "LIVE",
            "token_configured": bool(settings.web_dashboard_token),
            "limits": {
                "daily": settings.daily_spend_limit,
                "per_order": settings.per_order_spend_limit,
            },
            "spend": {"today": spend_today, "total": spend_total},
            "active_orders": active_orders,
            "awaiting_review": awaiting_review,
            "orders_by_status": order_counts,
            "submissions_by_status": submission_counts,
            "recent_orders": recent_orders,
            "recent_events": recent_events,
        }

    @app.get("/api/orders", dependencies=[Depends(require_dashboard_access)])
    async def api_orders(status: str = "", limit: int = 50) -> list[dict[str, Any]]:
        settings = settings_factory()
        await init_db(settings)
        safe_limit = max(1, min(limit, 200))
        where = ""
        params: tuple[Any, ...] = ()
        if status:
            where = "WHERE status = ?"
            params = (status,)
        async with connect(settings) as conn:
            return await _fetch_all(
                conn,
                "SELECT client_order_uuid, external_order_id, status, exchange, scenario, "
                "target, quantity, service_id, source_platform, max_cost, cost_actual, "
                f"created_at, updated_at FROM orders {where} "
                "ORDER BY created_at DESC LIMIT ?",
                (*params, safe_limit),
            )

    @app.get("/api/orders/{order_uuid}", dependencies=[Depends(require_dashboard_access)])
    async def api_order_detail(order_uuid: str) -> dict[str, Any]:
        settings = settings_factory()
        await init_db(settings)
        async with connect(settings) as conn:
            order = await _fetch_one(
                conn,
                "SELECT * FROM orders WHERE client_order_uuid = ?",
                (order_uuid,),
            )
            if order is None:
                raise HTTPException(status_code=404, detail="order not found")
            submissions = await _fetch_all(
                conn,
                "SELECT * FROM submissions WHERE order_uuid = ? ORDER BY created_at DESC",
                (order_uuid,),
            )
            verifications = await _fetch_all(
                conn,
                "SELECT verdict, measured, expected, reason, created_at "
                "FROM verifications WHERE order_uuid = ? ORDER BY created_at DESC LIMIT 10",
                (order_uuid,),
            )
            events = await _fetch_all(
                conn,
                "SELECT occurred_at, actor, event, details_json "
                "FROM audit_log WHERE order_uuid = ? ORDER BY audit_id DESC LIMIT 20",
                (order_uuid,),
            )
        return {
            "order": order,
            "submissions": submissions,
            "verifications": verifications,
            "events": events,
        }

    @app.post("/api/orders/{order_uuid}/verify", dependencies=[Depends(require_dashboard_access)])
    async def api_verify_order(order_uuid: str) -> dict[str, Any]:
        return await with_orchestrator(lambda _settings, orch: orch.verify_single_order(order_uuid))

    @app.post("/api/check", dependencies=[Depends(require_dashboard_access)])
    async def api_check() -> dict[str, Any]:
        results = await with_orchestrator(lambda _settings, orch: orch.poll_all())
        return {"results": results}

    @app.post("/api/autopilot", dependencies=[Depends(require_dashboard_access)])
    async def api_autopilot(payload: AutopilotRequest) -> dict[str, Any]:
        settings = settings_factory()
        await init_db(settings)
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as http_client:
            adapters = get_adapters(settings, http_client)
            planner = OllamaPlanner(
                base_url=settings.ollama_base_url,
                model=settings.ollama_model,
                http_client=http_client,
                timeout_seconds=settings.ollama_timeout_seconds,
            )
            metrics_provider = build_activity_metrics_provider(
                youtube_api_key=settings.youtube_data_api_key,
                http_client=http_client,
            )
            result = await AutopilotRunner(
                settings,
                adapters,
                planner,
                activity_metrics_provider=metrics_provider,
            ).run_goal(
                payload.goal.strip(),
                actor="web:dashboard",
                execute=payload.execute,
            )
        return {
            "status": result.status,
            "summary": format_autopilot_result(result),
            "result": result.model_dump(mode="json"),
        }

    @app.get("/api/review", dependencies=[Depends(require_dashboard_access)])
    async def api_review() -> list[dict[str, Any]]:
        settings = settings_factory()
        await init_db(settings)
        async with connect(settings) as conn:
            return await _fetch_all(
                conn,
                "SELECT s.submission_uuid, s.external_submission_id, s.executor_hint, "
                "s.evidence, s.created_at, o.client_order_uuid AS order_uuid, "
                "o.exchange, o.scenario, o.target, o.quantity "
                "FROM submissions s JOIN orders o ON o.client_order_uuid = s.order_uuid "
                "WHERE s.status = 'awaiting_admin' ORDER BY s.created_at ASC LIMIT 50",
            )

    @app.post(
        "/api/submissions/{submission_uuid}/accept",
        dependencies=[Depends(require_dashboard_access)],
    )
    async def api_accept_submission(submission_uuid: str) -> dict[str, Any]:
        return await with_orchestrator(
            lambda _settings, orch: orch.admin_accept_submission(
                submission_uuid,
                actor="web:dashboard",
            )
        )

    @app.post(
        "/api/submissions/{submission_uuid}/reject",
        dependencies=[Depends(require_dashboard_access)],
    )
    async def api_reject_submission(
        submission_uuid: str,
        payload: RejectRequest | None = None,
    ) -> dict[str, Any]:
        reason = payload.reason if payload is not None else RejectRequest().reason
        return await with_orchestrator(
            lambda _settings, orch: orch.admin_reject_submission(
                submission_uuid,
                actor="web:dashboard",
                reason=reason,
            )
        )

    @app.get("/api/report", dependencies=[Depends(require_dashboard_access)])
    async def api_report() -> dict[str, Any]:
        rows = await preview_weekly_rows(settings_factory())
        return {"rows": rows}

    @app.get("/api/health", dependencies=[Depends(require_dashboard_access)])
    async def api_health() -> dict[str, Any]:
        settings = settings_factory()
        await init_db(settings)
        checks = [
            {"name": "Dashboard token", "ok": bool(settings.web_dashboard_token)},
            {"name": "Telegram admins", "ok": bool(settings.telegram_admin_ids)},
            {
                "name": "Yandex Metrica",
                "ok": bool(settings.metrica_counter_id and settings.metrica_oauth_token),
            },
            {"name": "YouTube Data API", "ok": bool(settings.youtube_data_api_key)},
            {"name": "Google Sheets", "ok": bool(settings.google_sheets_spreadsheet_id)},
            {"name": "Ollama", "ok": bool(settings.ollama_base_url and settings.ollama_model)},
            {"name": "smmcode", "ok": bool(settings.smmcode_api_key) or settings.dry_run},
            {"name": "prskill", "ok": bool(settings.prskill_api_key) or settings.dry_run},
            {"name": "unu", "ok": bool(settings.unu_api_key) or settings.dry_run},
            {"name": "advego", "ok": bool(settings.advego_api_token) or settings.dry_run},
            {"name": "ipgold", "ok": bool(settings.ipgold_api_key) or settings.dry_run},
        ]
        return {"mode": "DRY_RUN" if settings.dry_run else "LIVE", "checks": checks}

    @app.get("/api/balances", dependencies=[Depends(require_dashboard_access)])
    async def api_balances() -> dict[str, Any]:
        settings = settings_factory()
        results: list[dict[str, Any]] = []
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as http_client:
            for name, env_key in _BALANCE_EXCHANGES:
                try:
                    adapter = build_adapter(
                        settings,
                        name,
                        http_client,
                        dry_run=settings.dry_run,
                    )
                except ValueError:
                    results.append(
                        {
                            "exchange": name,
                            "status": "missing_credentials",
                            "message": f"{env_key} is not set",
                        }
                    )
                    continue
                except Exception as exc:
                    results.append(
                        {
                            "exchange": name,
                            "status": "error",
                            "message": _safe_error(exc),
                        }
                    )
                    continue

                if Capability.GET_BALANCE not in adapter.capabilities():
                    results.append(
                        {
                            "exchange": name,
                            "status": "unsupported",
                            "message": "public balance endpoint is not available",
                        }
                    )
                    continue

                try:
                    balance = await adapter.get_balance()
                    results.append(
                        {
                            "exchange": name,
                            "status": "ok",
                            "balance": balance,
                        }
                    )
                except Exception as exc:
                    results.append(
                        {
                            "exchange": name,
                            "status": "error",
                            "message": _safe_error(exc),
                        }
                    )
        return {"balances": results}

    return app


def _extract_token(request: Request) -> str | None:
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    header_token = request.headers.get("x-dashboard-token")
    if header_token:
        return header_token
    cookie_token = request.cookies.get("dashboard_token")
    if cookie_token:
        return cookie_token
    return request.query_params.get("token")


async def _counts_by(conn: Any, table: str, column: str) -> dict[str, int]:
    cursor = await conn.execute(
        f"SELECT {column} AS k, COUNT(*) AS c FROM {table} GROUP BY {column}"
    )
    rows = await cursor.fetchall()
    return {str(row["k"]): int(row["c"]) for row in rows}


async def _scalar_float(conn: Any, query: str, params: tuple[Any, ...] = ()) -> float:
    row = await _fetch_one(conn, query, params)
    return float(row["v"]) if row is not None and row["v"] is not None else 0.0


async def _scalar_int(conn: Any, query: str, params: tuple[Any, ...] = ()) -> int:
    row = await _fetch_one(conn, query, params)
    return int(row["v"]) if row is not None and row["v"] is not None else 0


async def _fetch_one(
    conn: Any,
    query: str,
    params: tuple[Any, ...] = (),
) -> dict[str, Any] | None:
    cursor = await conn.execute(query, params)
    row = await cursor.fetchone()
    return dict(row) if row is not None else None


async def _fetch_all(
    conn: Any,
    query: str,
    params: tuple[Any, ...] = (),
) -> list[dict[str, Any]]:
    cursor = await conn.execute(query, params)
    rows = await cursor.fetchall()
    return [dict(row) for row in rows]


def _safe_error(exc: Exception) -> str:
    return f"{type(exc).__name__}: {exc}"[:160]


_DASHBOARD_HTML = r"""<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Task Monitoring Bot Dashboard</title>
  <style>
    :root {
      color-scheme: light;
      --bg: #eef1f4;
      --surface: #ffffff;
      --surface-2: #f7f9fb;
      --text: #172026;
      --muted: #64717d;
      --line: #d9e0e6;
      --blue: #1f6feb;
      --green: #168a5b;
      --amber: #b76e00;
      --red: #c73737;
      --ink: #0f1720;
      --shadow: 0 12px 28px rgba(23, 32, 38, .08);
      font-family:
        Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont,
        "Segoe UI", sans-serif;
    }

    * { box-sizing: border-box; }

    body {
      margin: 0;
      background: var(--bg);
      color: var(--text);
      min-width: 320px;
    }

    button, input, textarea, select {
      font: inherit;
    }

    .app {
      display: grid;
      grid-template-columns: 260px minmax(0, 1fr);
      min-height: 100vh;
    }

    .sidebar {
      background: #111820;
      color: #f6f8fb;
      padding: 22px 18px;
      position: sticky;
      top: 0;
      height: 100vh;
      display: flex;
      flex-direction: column;
      gap: 22px;
    }

    .brand {
      display: flex;
      align-items: center;
      gap: 12px;
      min-height: 48px;
    }

    .brand-mark {
      width: 38px;
      height: 38px;
      border-radius: 8px;
      background: #2a8c66;
      display: grid;
      place-items: center;
      font-weight: 800;
      letter-spacing: 0;
    }

    .brand-title {
      font-weight: 760;
      line-height: 1.1;
    }

    .brand-subtitle {
      color: #a9b6c2;
      font-size: 12px;
      margin-top: 3px;
    }

    .nav {
      display: grid;
      gap: 6px;
    }

    .nav button {
      width: 100%;
      border: 0;
      border-radius: 8px;
      background: transparent;
      color: #cbd5df;
      text-align: left;
      padding: 11px 12px;
      cursor: pointer;
    }

    .nav button:hover,
    .nav button.active {
      background: #1c2834;
      color: #ffffff;
    }

    .sidebar-footer {
      margin-top: auto;
      color: #a9b6c2;
      font-size: 12px;
      line-height: 1.45;
      border-top: 1px solid #2b3744;
      padding-top: 16px;
    }

    .content {
      padding: 22px;
      display: grid;
      gap: 18px;
      align-content: start;
    }

    .topbar {
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 14px;
      min-height: 48px;
    }

    h1 {
      margin: 0;
      font-size: 24px;
      line-height: 1.15;
      letter-spacing: 0;
    }

    h2 {
      margin: 0;
      font-size: 16px;
      letter-spacing: 0;
    }

    .muted {
      color: var(--muted);
    }

    .status-line {
      display: flex;
      align-items: center;
      flex-wrap: wrap;
      gap: 8px;
      justify-content: flex-end;
    }

    .badge {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      min-height: 26px;
      padding: 3px 9px;
      border-radius: 999px;
      background: var(--surface);
      border: 1px solid var(--line);
      color: var(--muted);
      font-size: 12px;
      white-space: nowrap;
    }

    .badge.live { color: var(--red); border-color: #efb5b5; background: #fff5f5; }
    .badge.dry { color: var(--green); border-color: #bce7d2; background: #f0fbf5; }
    .badge.warn { color: var(--amber); border-color: #f4d192; background: #fff8e8; }

    .command {
      background: var(--surface);
      border: 1px solid var(--line);
      border-radius: 8px;
      box-shadow: var(--shadow);
      padding: 16px;
      display: grid;
      gap: 12px;
    }

    .command-grid {
      display: grid;
      grid-template-columns: minmax(0, 1fr) 170px 140px;
      gap: 10px;
      align-items: end;
    }

    textarea,
    input,
    select {
      border: 1px solid var(--line);
      background: var(--surface-2);
      color: var(--text);
      border-radius: 8px;
      padding: 10px 11px;
      outline: none;
      width: 100%;
    }

    textarea {
      resize: vertical;
      min-height: 44px;
      max-height: 150px;
    }

    textarea:focus,
    input:focus,
    select:focus {
      border-color: #8ab4f8;
      box-shadow: 0 0 0 3px rgba(31, 111, 235, .12);
    }

    .label {
      display: grid;
      gap: 6px;
      color: var(--muted);
      font-size: 12px;
    }

    .button {
      border: 0;
      border-radius: 8px;
      background: var(--blue);
      color: #ffffff;
      min-height: 42px;
      padding: 0 14px;
      cursor: pointer;
      font-weight: 650;
    }

    .button:hover { filter: brightness(.96); }
    .button.secondary { background: #273341; }
    .button.green { background: var(--green); }
    .button.red { background: var(--red); }
    .button.ghost {
      color: var(--text);
      background: var(--surface);
      border: 1px solid var(--line);
    }
    .button:disabled {
      opacity: .58;
      cursor: wait;
    }

    .kpis {
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 12px;
    }

    .kpi,
    .panel {
      background: var(--surface);
      border: 1px solid var(--line);
      border-radius: 8px;
      box-shadow: var(--shadow);
    }

    .kpi {
      padding: 14px;
      display: grid;
      gap: 7px;
      min-height: 104px;
    }

    .kpi-label {
      color: var(--muted);
      font-size: 12px;
    }

    .kpi-value {
      font-size: 26px;
      line-height: 1;
      font-weight: 780;
      letter-spacing: 0;
    }

    .kpi-foot {
      color: var(--muted);
      font-size: 12px;
    }

    .grid-2 {
      display: grid;
      grid-template-columns: minmax(0, 1.4fr) minmax(340px, .8fr);
      gap: 12px;
    }

    .panel {
      overflow: hidden;
    }

    .panel-head {
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 12px;
      padding: 14px 16px;
      border-bottom: 1px solid var(--line);
      background: #fbfcfd;
    }

    .panel-body {
      padding: 14px 16px;
      overflow-x: auto;
    }

    .toolbar {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      align-items: center;
    }

    table {
      width: 100%;
      border-collapse: collapse;
      table-layout: auto;
    }

    th,
    td {
      padding: 11px 12px;
      border-bottom: 1px solid var(--line);
      text-align: left;
      vertical-align: top;
      overflow-wrap: normal;
    }

    th {
      color: var(--muted);
      font-size: 12px;
      font-weight: 650;
      background: #fbfcfd;
    }

    td {
      font-size: 13px;
    }

    tr:hover td {
      background: #fbfcfd;
    }

    .target-cell {
      color: #2b3a45;
      font-size: 12px;
      line-height: 1.35;
      overflow-wrap: anywhere;
    }

    .mono {
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    }

    .stack {
      display: grid;
      gap: 10px;
    }

    .empty {
      color: var(--muted);
      padding: 24px;
      text-align: center;
    }

    .event {
      display: grid;
      gap: 3px;
      border-bottom: 1px solid var(--line);
      padding: 10px 0;
    }

    .event:last-child { border-bottom: 0; }

    .event-title {
      font-weight: 650;
      font-size: 13px;
    }

    .event-meta {
      color: var(--muted);
      font-size: 12px;
    }

    .detail-grid {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 12px;
    }

    .detail-section {
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 12px;
      background: var(--surface-2);
      display: grid;
      gap: 8px;
    }

    .detail-section.wide {
      grid-column: 1 / -1;
    }

    .detail-row {
      display: grid;
      grid-template-columns: 120px minmax(0, 1fr);
      gap: 10px;
      font-size: 13px;
    }

    .detail-key {
      color: var(--muted);
    }

    .view {
      display: none;
      gap: 12px;
    }

    .view.active {
      display: grid;
    }

    pre {
      margin: 0;
      padding: 12px;
      border-radius: 8px;
      background: #111820;
      color: #edf3f8;
      overflow: auto;
      max-height: 320px;
      font-size: 12px;
      line-height: 1.45;
    }

    .auth {
      position: fixed;
      inset: 0;
      display: none;
      place-items: center;
      background: rgba(17, 24, 32, .58);
      padding: 18px;
      z-index: 10;
    }

    .auth.show { display: grid; }

    .auth-panel {
      width: min(420px, 100%);
      background: var(--surface);
      border-radius: 8px;
      border: 1px solid var(--line);
      box-shadow: 0 24px 60px rgba(0,0,0,.26);
      padding: 18px;
      display: grid;
      gap: 12px;
    }

    .toast {
      position: fixed;
      right: 18px;
      bottom: 18px;
      background: #111820;
      color: white;
      padding: 12px 14px;
      border-radius: 8px;
      box-shadow: var(--shadow);
      max-width: 420px;
      display: none;
      z-index: 11;
      font-size: 13px;
    }

    .toast.show { display: block; }

    @media (max-width: 1080px) {
      .app {
        grid-template-columns: 1fr;
      }
      .sidebar {
        position: static;
        height: auto;
      }
      .nav {
        grid-template-columns: repeat(3, minmax(0, 1fr));
      }
      .sidebar-footer {
        display: none;
      }
      .grid-2,
      .command-grid,
      .kpis {
        grid-template-columns: 1fr;
      }
    }

    @media (max-width: 620px) {
      .content {
        padding: 14px;
      }
      .topbar {
        align-items: flex-start;
        flex-direction: column;
      }
      .status-line {
        justify-content: flex-start;
      }
      th:nth-child(3),
      td:nth-child(3) {
        display: none;
      }
      .nav {
        grid-template-columns: 1fr 1fr;
      }
      .detail-grid {
        grid-template-columns: 1fr;
      }
    }
  </style>
</head>
<body>
  <div class="app">
    <aside class="sidebar">
      <div class="brand">
        <div class="brand-mark">TM</div>
        <div>
          <div class="brand-title">Task Monitoring Bot</div>
          <div class="brand-subtitle">Operations dashboard</div>
        </div>
      </div>
      <nav class="nav">
        <button class="active" data-view="overview">Сводка</button>
        <button data-view="orders">Заказы</button>
        <button data-view="review">Проверка</button>
        <button data-view="report">Отчёт</button>
        <button data-view="health">Интеграции</button>
      </nav>
      <div class="sidebar-footer">
        SQLite остаётся source of truth. Денежные действия проходят через тот же orchestrator,
        что и Telegram/CLI.
      </div>
    </aside>

    <main class="content">
      <div class="topbar">
        <div>
          <h1>Панель управления</h1>
          <div class="muted" id="updatedAt">Загрузка...</div>
        </div>
        <div class="status-line">
          <span class="badge" id="modeBadge">mode</span>
          <span class="badge" id="tokenBadge">token</span>
          <button class="button ghost" id="refreshBtn">Обновить</button>
        </div>
      </div>

      <section class="command">
        <div class="panel-head" style="padding:0;border:0;background:transparent">
          <h2>LLM-автопилот</h2>
          <div class="toolbar">
            <button class="button secondary" id="checkBtn">Запустить проверку</button>
            <button class="button ghost" id="balancesBtn">Балансы</button>
          </div>
        </div>
        <div class="command-grid">
          <label class="label">
            Цель
            <textarea id="goalInput" placeholder="500 лайков на https://youtube.com/watch?v=..."></textarea>
          </label>
          <label class="label">
            Режим
            <select id="goalMode">
              <option value="plan">Только план</option>
              <option value="execute">Создать заказ</option>
            </select>
          </label>
          <button class="button green" id="goalBtn">Выполнить</button>
        </div>
        <pre id="commandOutput" hidden></pre>
      </section>

      <section id="view-overview" class="view active">
        <div class="kpis">
          <div class="kpi">
            <div class="kpi-label">Активные заказы</div>
            <div class="kpi-value" id="kpiActive">0</div>
            <div class="kpi-foot">creating / active / verifying</div>
          </div>
          <div class="kpi">
            <div class="kpi-label">На ручной проверке</div>
            <div class="kpi-value" id="kpiReview">0</div>
            <div class="kpi-foot">awaiting_admin</div>
          </div>
          <div class="kpi">
            <div class="kpi-label">Расход сегодня</div>
            <div class="kpi-value" id="kpiSpendToday">0</div>
            <div class="kpi-foot" id="kpiLimit">лимит</div>
          </div>
          <div class="kpi">
            <div class="kpi-label">Расход всего</div>
            <div class="kpi-value" id="kpiSpendTotal">0</div>
            <div class="kpi-foot">по локальной БД</div>
          </div>
        </div>

        <div class="grid-2">
          <div class="panel">
            <div class="panel-head">
              <h2>Последние заказы</h2>
              <button class="button ghost" data-view-jump="orders">Открыть все</button>
            </div>
            <div class="panel-body" style="padding:0">
              <table>
                <thead>
                  <tr>
                    <th>Статус</th>
                    <th>Биржа</th>
                    <th>Сценарий</th>
                    <th>Цель</th>
                    <th>Стоимость</th>
                  </tr>
                </thead>
                <tbody id="recentOrders"></tbody>
              </table>
            </div>
          </div>
          <div class="panel">
            <div class="panel-head"><h2>События</h2></div>
            <div class="panel-body stack" id="recentEvents"></div>
          </div>
        </div>
      </section>

      <section id="view-orders" class="view">
        <div class="panel">
          <div class="panel-head">
            <h2>Заказы</h2>
            <div class="toolbar">
              <select id="orderStatus">
                <option value="">Все статусы</option>
                <option value="creating">creating</option>
                <option value="active">active</option>
                <option value="verifying">verifying</option>
                <option value="completed">completed</option>
                <option value="failed">failed</option>
                <option value="cancelled">cancelled</option>
              </select>
              <button class="button ghost" id="ordersRefreshBtn">Обновить</button>
            </div>
          </div>
          <div class="panel-body" style="padding:0">
            <table>
              <thead>
                <tr>
                  <th>UUID</th>
                  <th>Статус</th>
                  <th>Биржа</th>
                  <th>Сценарий</th>
                  <th>Цель</th>
                  <th>Действия</th>
                </tr>
              </thead>
              <tbody id="ordersTable"></tbody>
            </table>
          </div>
        </div>
        <div class="panel" id="orderDetailPanel" hidden>
          <div class="panel-head"><h2>Детали заказа</h2></div>
          <div class="panel-body"><div class="detail-grid" id="orderDetail"></div></div>
        </div>
      </section>

      <section id="view-review" class="view">
        <div class="panel">
          <div class="panel-head">
            <h2>Сабмишены на решение</h2>
            <button class="button ghost" id="reviewRefreshBtn">Обновить</button>
          </div>
          <div class="panel-body" style="padding:0">
            <table>
              <thead>
                <tr>
                  <th>Submission</th>
                  <th>Биржа</th>
                  <th>Цель</th>
                  <th>Исполнитель</th>
                  <th>Действия</th>
                </tr>
              </thead>
              <tbody id="reviewTable"></tbody>
            </table>
          </div>
        </div>
      </section>

      <section id="view-report" class="view">
        <div class="panel">
          <div class="panel-head">
            <h2>Отчёт за неделю</h2>
            <button class="button ghost" id="reportRefreshBtn">Обновить</button>
          </div>
          <div class="panel-body" style="padding:0">
            <table>
              <thead>
                <tr>
                  <th>Источник</th>
                  <th>Биржа</th>
                  <th>Заказано</th>
                  <th>Факт</th>
                  <th>Стоимость</th>
                  <th>Статус</th>
                </tr>
              </thead>
              <tbody id="reportTable"></tbody>
            </table>
          </div>
        </div>
      </section>

      <section id="view-health" class="view">
        <div class="grid-2">
          <div class="panel">
            <div class="panel-head">
              <h2>Интеграции</h2>
              <button class="button ghost" id="healthRefreshBtn">Обновить</button>
            </div>
            <div class="panel-body stack" id="healthList"></div>
          </div>
          <div class="panel">
            <div class="panel-head"><h2>Балансы</h2></div>
            <div class="panel-body stack" id="balanceList"></div>
          </div>
        </div>
      </section>
    </main>
  </div>

  <div class="auth" id="authBox">
    <form class="auth-panel" id="authForm">
      <h2>Dashboard token</h2>
      <input name="username" autocomplete="username" value="dashboard" hidden>
      <input
        id="tokenInput"
        type="password"
        autocomplete="current-password"
        placeholder="WEB_DASHBOARD_TOKEN"
      >
      <button class="button" id="saveTokenBtn" type="submit">Продолжить</button>
    </form>
  </div>
  <div class="toast" id="toast"></div>

  <script>
    const state = {
      token: sessionStorage.getItem('dashboardToken') || '',
      activeView: 'overview'
    };

    const params = new URLSearchParams(location.search);
    if (params.get('token')) {
      state.token = params.get('token');
      sessionStorage.setItem('dashboardToken', state.token);
      history.replaceState(null, '', location.pathname);
    }

    const $ = (id) => document.getElementById(id);

    function money(value) {
      const number = Number(value || 0);
      return new Intl.NumberFormat('ru-RU', {
        style: 'currency',
        currency: 'RUB',
        maximumFractionDigits: 2
      }).format(number);
    }

    function shortId(value) {
      return value ? String(value).slice(0, 8) : '—';
    }

    function escapeHtml(value) {
      return String(value ?? '').replace(/[&<>"']/g, (ch) => ({
        '&': '&amp;',
        '<': '&lt;',
        '>': '&gt;',
        '"': '&quot;',
        "'": '&#39;'
      }[ch]));
    }

    function showToast(text) {
      const toast = $('toast');
      toast.textContent = text;
      toast.classList.add('show');
      clearTimeout(showToast.timer);
      showToast.timer = setTimeout(() => toast.classList.remove('show'), 4200);
    }

    function headers() {
      const h = {'Content-Type': 'application/json'};
      if (state.token) h.Authorization = `Bearer ${state.token}`;
      return h;
    }

    async function api(path, options = {}) {
      const response = await fetch(path, {
        ...options,
        headers: {
          ...headers(),
          ...(options.headers || {})
        }
      });
      if (response.status === 401) {
        $('authBox').classList.add('show');
        throw new Error('Нужен dashboard token');
      }
      const contentType = response.headers.get('content-type') || '';
      const payload = contentType.includes('application/json') ? await response.json() : null;
      if (!response.ok) {
        throw new Error(payload?.detail || `HTTP ${response.status}`);
      }
      return payload;
    }

    async function loadOverview() {
      const data = await api('/api/overview');
      $('modeBadge').textContent = data.mode;
      $('modeBadge').className = `badge ${data.mode === 'LIVE' ? 'live' : 'dry'}`;
      $('tokenBadge').textContent = data.token_configured ? 'token enabled' : 'local access';
      $('tokenBadge').className = `badge ${data.token_configured ? '' : 'warn'}`;
      $('kpiActive').textContent = data.active_orders;
      $('kpiReview').textContent = data.awaiting_review;
      $('kpiSpendToday').textContent = money(data.spend.today);
      $('kpiSpendTotal').textContent = money(data.spend.total);
      $('kpiLimit').textContent = `лимит ${money(data.limits.daily)}`;
      $('updatedAt').textContent = `Обновлено ${new Date().toLocaleString('ru-RU')}`;
      renderRecentOrders(data.recent_orders || []);
      renderEvents(data.recent_events || []);
    }

    function renderRecentOrders(rows) {
      $('recentOrders').innerHTML = rows.length ? rows.map(order => `
        <tr>
          <td>${statusBadge(order.status)}</td>
          <td>${escapeHtml(order.exchange)}</td>
          <td>${escapeHtml(order.scenario)}</td>
          <td class="target-cell">${escapeHtml(order.target)}</td>
          <td>${order.cost_actual == null ? '—' : money(order.cost_actual)}</td>
        </tr>
      `).join('') : `<tr><td colspan="5" class="empty">Нет заказов</td></tr>`;
    }

    function renderEvents(rows) {
      $('recentEvents').innerHTML = rows.length ? rows.map(event => `
        <div class="event">
          <div class="event-title">${escapeHtml(event.event)}</div>
          <div class="event-meta">${escapeHtml(event.actor)} · ${escapeHtml(event.occurred_at)}</div>
          <div class="event-meta mono">${escapeHtml(event.order_uuid || event.submission_uuid || '')}</div>
        </div>
      `).join('') : `<div class="empty">Событий пока нет</div>`;
    }

    function statusBadge(status) {
      const tone = status === 'completed' || status === 'accepted'
        ? 'dry'
        : status === 'failed' || status === 'cancelled'
          ? 'live'
          : status === 'awaiting_admin'
            ? 'warn'
            : '';
      return `<span class="badge ${tone}">${escapeHtml(status)}</span>`;
    }

    async function loadOrders() {
      const status = $('orderStatus').value;
      const rows = await api(`/api/orders?limit=80${status ? `&status=${encodeURIComponent(status)}` : ''}`);
      $('ordersTable').innerHTML = rows.length ? rows.map(order => `
        <tr>
          <td class="mono">${shortId(order.client_order_uuid)}</td>
          <td>${statusBadge(order.status)}</td>
          <td>${escapeHtml(order.exchange)}</td>
          <td>${escapeHtml(order.scenario)}</td>
          <td class="target-cell">${escapeHtml(order.target)}</td>
          <td>
            <div class="toolbar">
              <button class="button ghost" data-open-order="${escapeHtml(order.client_order_uuid)}">Открыть</button>
              <button class="button secondary" data-verify-order="${escapeHtml(order.client_order_uuid)}">Проверить</button>
            </div>
          </td>
        </tr>
      `).join('') : `<tr><td colspan="6" class="empty">Заказов нет</td></tr>`;
    }

    async function openOrder(uuid) {
      const detail = await api(`/api/orders/${encodeURIComponent(uuid)}`);
      $('orderDetailPanel').hidden = false;
      renderOrderDetail(detail);
    }

    function detailRow(key, value) {
      return `
        <div class="detail-row">
          <div class="detail-key">${escapeHtml(key)}</div>
          <div>${escapeHtml(value ?? '—')}</div>
        </div>
      `;
    }

    function renderOrderDetail(detail) {
      const order = detail.order || {};
      const submissions = detail.submissions || [];
      const verifications = detail.verifications || [];
      const events = detail.events || [];
      $('orderDetail').innerHTML = `
        <section class="detail-section">
          <h2>Заказ</h2>
          ${detailRow('UUID', order.client_order_uuid)}
          ${detailRow('Статус', order.status)}
          ${detailRow('Биржа', order.exchange)}
          ${detailRow('Сценарий', order.scenario)}
          ${detailRow('Количество', order.quantity)}
          ${detailRow('Стоимость', order.cost_actual == null ? '—' : money(order.cost_actual))}
        </section>
        <section class="detail-section">
          <h2>Внешняя система</h2>
          ${detailRow('External ID', order.external_order_id)}
          ${detailRow('Raw status', order.raw_exchange_status)}
          ${detailRow('Service', order.service_id)}
          ${detailRow('Источник', order.source_platform)}
          ${detailRow('Создан', order.created_at)}
          ${detailRow('Обновлён', order.updated_at)}
        </section>
        <section class="detail-section wide">
          <h2>Цель</h2>
          <div class="target-cell">${escapeHtml(order.target)}</div>
        </section>
        <section class="detail-section wide">
          <h2>Проверки</h2>
          ${verifications.length ? verifications.map(v => `
            <div class="event">
              <div class="event-title">${escapeHtml(v.verdict)} · ${escapeHtml(v.measured)} / ${escapeHtml(v.expected)}</div>
              <div class="event-meta">${escapeHtml(v.reason)}</div>
              <div class="event-meta">${escapeHtml(v.created_at)}</div>
            </div>
          `).join('') : '<div class="empty">Проверок пока нет</div>'}
        </section>
        <section class="detail-section">
          <h2>Сабмишены</h2>
          ${submissions.length ? submissions.map(s => `
            <div class="event">
              <div class="event-title">${escapeHtml(s.status)} · ${shortId(s.submission_uuid)}</div>
              <div class="event-meta">${escapeHtml(s.executor_hint || '—')}</div>
            </div>
          `).join('') : '<div class="empty">Сабмишенов нет</div>'}
        </section>
        <section class="detail-section">
          <h2>Audit</h2>
          ${events.length ? events.map(e => `
            <div class="event">
              <div class="event-title">${escapeHtml(e.event)}</div>
              <div class="event-meta">${escapeHtml(e.actor)} · ${escapeHtml(e.occurred_at)}</div>
            </div>
          `).join('') : '<div class="empty">Событий нет</div>'}
        </section>
      `;
    }

    async function verifyOrder(uuid) {
      const result = await api(`/api/orders/${encodeURIComponent(uuid)}/verify`, {method: 'POST'});
      showToast(`Проверка ${shortId(uuid)}: ${result.status}`);
      await Promise.all([loadOverview(), loadOrders()]);
    }

    async function loadReview() {
      const rows = await api('/api/review');
      $('reviewTable').innerHTML = rows.length ? rows.map(item => `
        <tr>
          <td class="mono">${shortId(item.submission_uuid)}</td>
          <td>${escapeHtml(item.exchange)}<br><span class="muted">${escapeHtml(item.scenario)}</span></td>
          <td class="target-cell">${escapeHtml(item.target)}</td>
          <td>${escapeHtml(item.executor_hint || '—')}</td>
          <td>
            <div class="toolbar">
              <button class="button green" data-accept="${escapeHtml(item.submission_uuid)}">Принять</button>
              <button class="button red" data-reject="${escapeHtml(item.submission_uuid)}">Вернуть</button>
            </div>
          </td>
        </tr>
      `).join('') : `<tr><td colspan="5" class="empty">Нет сабмишенов на ручное решение</td></tr>`;
    }

    async function decideSubmission(uuid, action) {
      const body = action === 'reject'
        ? JSON.stringify({reason: 'Возвращено из web dashboard'})
        : undefined;
      const result = await api(`/api/submissions/${encodeURIComponent(uuid)}/${action}`, {
        method: 'POST',
        body
      });
      showToast(`${action}: ${result.status || 'ok'}`);
      await Promise.all([loadOverview(), loadReview()]);
    }

    async function loadReport() {
      const data = await api('/api/report');
      const rows = data.rows || [];
      $('reportTable').innerHTML = rows.length ? rows.map(row => `
        <tr>
          <td>${escapeHtml(row.source_platform)}</td>
          <td>${escapeHtml(row.exchange)}</td>
          <td>${escapeHtml(row.ordered_count)}</td>
          <td>${escapeHtml(row.actual_count ?? '—')}</td>
          <td>${money(row.cost)}</td>
          <td>${statusBadge(row.status)}</td>
        </tr>
      `).join('') : `<tr><td colspan="6" class="empty">За текущую неделю строк нет</td></tr>`;
    }

    async function loadHealth() {
      const data = await api('/api/health');
      $('healthList').innerHTML = (data.checks || []).map(check => `
        <div class="event">
          <div class="event-title">${check.ok ? 'OK' : 'WARN'} · ${escapeHtml(check.name)}</div>
          <div class="event-meta">${check.ok ? 'configured' : 'needs attention'}</div>
        </div>
      `).join('');
    }

    async function loadBalances() {
      const data = await api('/api/balances');
      $('balanceList').innerHTML = (data.balances || []).map(item => `
        <div class="event">
          <div class="event-title">${escapeHtml(item.exchange)} · ${escapeHtml(item.status)}</div>
          <div class="event-meta">${item.balance == null ? escapeHtml(item.message || '') : money(item.balance)}</div>
        </div>
      `).join('');
    }

    async function runAutopilot() {
      const goal = $('goalInput').value.trim();
      if (!goal) {
        showToast('Введите цель');
        return;
      }
      const output = $('commandOutput');
      output.hidden = false;
      output.textContent = 'Выполняю...';
      const result = await api('/api/autopilot', {
        method: 'POST',
        body: JSON.stringify({goal, execute: $('goalMode').value === 'execute'})
      });
      output.textContent = result.summary || JSON.stringify(result, null, 2);
      await Promise.all([loadOverview(), loadOrders()]);
    }

    async function runCheck() {
      const output = $('commandOutput');
      output.hidden = false;
      output.textContent = 'Проверяю активные заказы...';
      const result = await api('/api/check', {method: 'POST'});
      output.textContent = JSON.stringify(result, null, 2);
      await Promise.all([loadOverview(), loadOrders(), loadReview(), loadReport()]);
    }

    async function refreshCurrent() {
      try {
        await loadOverview();
        if (state.activeView === 'orders') await loadOrders();
        if (state.activeView === 'review') await loadReview();
        if (state.activeView === 'report') await loadReport();
        if (state.activeView === 'health') await Promise.all([loadHealth(), loadBalances()]);
      } catch (error) {
        showToast(error.message);
      }
    }

    function switchView(view) {
      state.activeView = view;
      document.querySelectorAll('.view').forEach(el => el.classList.remove('active'));
      document.querySelector(`#view-${view}`).classList.add('active');
      document.querySelectorAll('.nav button').forEach(btn => {
        btn.classList.toggle('active', btn.dataset.view === view);
      });
      refreshCurrent();
    }

    document.addEventListener('click', async (event) => {
      const target = event.target.closest('button');
      if (!target) return;
      try {
        if (target.dataset.view) switchView(target.dataset.view);
        if (target.dataset.viewJump) switchView(target.dataset.viewJump);
        if (target.dataset.openOrder) await openOrder(target.dataset.openOrder);
        if (target.dataset.verifyOrder) await verifyOrder(target.dataset.verifyOrder);
        if (target.dataset.accept) await decideSubmission(target.dataset.accept, 'accept');
        if (target.dataset.reject) await decideSubmission(target.dataset.reject, 'reject');
      } catch (error) {
        showToast(error.message);
      }
    });

    $('refreshBtn').addEventListener('click', refreshCurrent);
    $('goalBtn').addEventListener('click', () => runAutopilot().catch(e => showToast(e.message)));
    $('checkBtn').addEventListener('click', () => runCheck().catch(e => showToast(e.message)));
    $('balancesBtn').addEventListener('click', async () => {
      switchView('health');
      await loadBalances();
    });
    $('ordersRefreshBtn').addEventListener('click', () => loadOrders().catch(e => showToast(e.message)));
    $('orderStatus').addEventListener('change', () => loadOrders().catch(e => showToast(e.message)));
    $('reviewRefreshBtn').addEventListener('click', () => loadReview().catch(e => showToast(e.message)));
    $('reportRefreshBtn').addEventListener('click', () => loadReport().catch(e => showToast(e.message)));
    $('healthRefreshBtn').addEventListener('click', () => Promise.all([loadHealth(), loadBalances()]).catch(e => showToast(e.message)));
    $('authForm').addEventListener('submit', (event) => {
      event.preventDefault();
      state.token = $('tokenInput').value.trim();
      sessionStorage.setItem('dashboardToken', state.token);
      $('authBox').classList.remove('show');
      refreshCurrent();
    });

    refreshCurrent();
  </script>
</body>
</html>
"""
