"""CLI entry point.

Commands:
- `smoke` (Day 1) - foundation wiring check.
- `create-order` (Day 2) - place an order on the chosen exchange; routes to a
  fake adapter when `DRY_RUN=true`, to the real adapter otherwise.
- `demo` (Day 3) - full lifecycle via the Orchestrator on fake adapters: create,
  poll, process submissions, accept/reject, finalize. Shows C1 / C2 invariants
  end-to-end without any real network calls.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

import httpx
from pydantic import ValidationError

from adapters.advego import AdvegoAdapter
from adapters.base import ExchangeAdapter, TaskExchangeAdapter
from adapters.fake import FakePanelAdapter, FakeTaskExchangeAdapter
from adapters.ipgold import IpgoldAdapter
from adapters.prskill import PrSkillAdapter
from adapters.smmcode import SmmcodeAdapter
from adapters.unu import UnuAdapter
from config import Settings, get_settings
from db.database import (
    append_audit,
    connect,
    count_audit_entries,
    get_order,
    init_db,
    update_order_status,
)
from models import (
    OrderSpec,
    OrderStatus,
    Scenario,
    SourcePlatform,
)
from orchestrator import Orchestrator, persist_and_create_order

PANEL_EXCHANGES = {"smmcode", "prskill"}
TASK_EXCHANGES = {"unu", "advego", "ipgold"}
FAKE_PANEL_NAME = "fake_panel"
FAKE_TASK_NAME = "fake_task_exchange"

TERMINAL_STATUSES = {OrderStatus.COMPLETED, OrderStatus.FAILED, OrderStatus.CANCELLED}


def build_adapter(
    settings: Settings,
    exchange_name: str,
    http_client: httpx.AsyncClient,
    *,
    dry_run: bool,
) -> ExchangeAdapter:
    """Pick the right adapter for an exchange.

    In DRY_RUN mode, every real exchange routes to its archetype's fake (panel
    or task-exchange). In live mode, dispatches to the concrete adapter.
    """
    if dry_run:
        if exchange_name in PANEL_EXCHANGES or exchange_name == FAKE_PANEL_NAME:
            return FakePanelAdapter()
        if exchange_name in TASK_EXCHANGES or exchange_name == FAKE_TASK_NAME:
            return FakeTaskExchangeAdapter()
        raise ValueError(f"unknown exchange {exchange_name!r}")
    if exchange_name == "smmcode":
        return SmmcodeAdapter(settings.smmcode_api_key, http_client)
    if exchange_name == "unu":
        return UnuAdapter(settings.unu_api_key, http_client)
    if exchange_name == "advego":
        return AdvegoAdapter(
            settings.advego_api_token,
            http_client,
            default_campaign_id=settings.advego_default_campaign_id,
        )
    if exchange_name == "prskill":
        return PrSkillAdapter(settings.prskill_api_key, http_client)
    if exchange_name == "ipgold":
        return IpgoldAdapter(settings.ipgold_api_key, http_client)
    raise ValueError(f"unknown exchange {exchange_name!r}")


# --- smoke (Day 1) ------------------------------------------------------------


async def _smoke_adapter(
    adapter: ExchangeAdapter, spec: OrderSpec, settings: Settings, label: str
) -> None:
    print(f"\n[Day 1 smoke] === {label} ===")
    caps = sorted(c.value for c in adapter.capabilities())
    print(f"  capabilities: {caps}")
    balance = await adapter.get_balance()
    print(f"  balance: {balance:.2f}")

    client_uuid, external_id, cost = await persist_and_create_order(
        settings, adapter, spec, actor="smoke"
    )
    print(f"  created: ext={external_id} cost={cost:.2f}")

    for i in range(1, 4):
        raw_status = await adapter.get_order_status(external_id)
        print(f"  poll {i}: {raw_status}")
        async with connect(settings) as conn:
            await append_audit(
                conn,
                actor="smoke",
                event="poll",
                order_uuid=client_uuid,
                details={"raw_status": raw_status, "iteration": i},
            )
        if raw_status == "completed":
            async with connect(settings) as conn:
                await update_order_status(
                    conn,
                    client_uuid,
                    OrderStatus.VERIFYING,
                    raw_exchange_status=raw_status,
                )
            break

    if isinstance(adapter, TaskExchangeAdapter):
        subs = await adapter.list_submissions(external_id)
        print(f"  list_submissions: {len(subs)} pending (full lifecycle via `demo`)")

    async with connect(settings) as conn:
        stored = await get_order(conn, client_uuid)
    print(f"  DB final status: {stored.status.value if stored else 'MISSING'}")


async def smoke(settings: Settings) -> None:
    print("[Day 1 smoke] starting...")
    print(
        f"[Day 1 smoke] settings: DRY_RUN={settings.dry_run} "
        f"db_path={settings.db_path} daily_limit={settings.daily_spend_limit}"
    )
    await init_db(settings)
    print(f"[Day 1 smoke] DB initialised at {settings.db_path}")

    panel_spec = OrderSpec(
        scenario=Scenario.ACTIVITY_SUBSCRIBE,
        exchange="fake_panel",
        target="https://t.me/example_channel",
        quantity=10,
        max_cost=2.0,
    )
    task_spec = OrderSpec(
        scenario=Scenario.SOCIAL_TRAFFIC,
        exchange="fake_task_exchange",
        target="https://example.com/landing",
        quantity=5,
        source_platform=SourcePlatform.VK,
        max_cost=2.0,
    )

    await _smoke_adapter(FakePanelAdapter(), panel_spec, settings, "FakePanelAdapter")
    await _smoke_adapter(FakeTaskExchangeAdapter(), task_spec, settings, "FakeTaskExchangeAdapter")

    async with connect(settings) as conn:
        n_audit = await count_audit_entries(conn)
    print(f"\n[Day 1 smoke] audit_log entries: {n_audit}")
    print("[Day 1 smoke] done - foundation wiring works.")


# --- create-order (Day 2) -----------------------------------------------------


async def create_order_command(settings: Settings, args: argparse.Namespace) -> int:
    dry_run = args.dry_run if args.dry_run is not None else settings.dry_run

    try:
        spec = OrderSpec(
            scenario=Scenario(args.scenario),
            exchange=args.exchange,
            target=args.target,
            quantity=args.quantity,
            service_id=args.service_id,
            source_platform=(
                SourcePlatform(args.source_platform) if args.source_platform else None
            ),
            max_cost=args.max_cost,
        )
    except ValidationError as exc:
        print(f"[create-order] invalid spec:\n{exc}", file=sys.stderr)
        return 2

    print(f"[create-order] mode={'DRY_RUN' if dry_run else 'LIVE'}  exchange={spec.exchange}")
    print(
        f"[create-order] spec: scenario={spec.scenario.value} target={spec.target} "
        f"quantity={spec.quantity} service_id={spec.service_id} max_cost={spec.max_cost}"
    )

    await init_db(settings)
    async with httpx.AsyncClient(timeout=30.0) as http:
        adapter = build_adapter(settings, args.exchange, http, dry_run=dry_run)
        orch = Orchestrator(settings, {spec.exchange: adapter})
        try:
            client_uuid, external_id, cost = await orch.create_order(spec, actor="cli")
        except Exception as exc:
            print(
                f"[create-order] FAILED: {type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
            return 1
    print(f"[create-order] OK: client_uuid={client_uuid} external_id={external_id} cost={cost:.2f}")
    return 0


# --- demo (Day 3) -------------------------------------------------------------


async def demo_command(settings: Settings) -> int:
    print("[demo] Day 3 - full DRY_RUN lifecycle via Orchestrator on fake adapters")
    await init_db(settings)

    adapters: dict[str, ExchangeAdapter] = {
        "fake_panel": FakePanelAdapter(),
        "fake_task_exchange": FakeTaskExchangeAdapter(),
    }
    orch = Orchestrator(settings, adapters)

    fixed = await orch.reconcile_creating(actor="demo")
    if fixed:
        print(f"[demo] reconciled {fixed} orphan CREATING row(s) -> FAILED")

    panel_spec = OrderSpec(
        scenario=Scenario.ACTIVITY_SUBSCRIBE,
        exchange="fake_panel",
        target="https://t.me/our_channel",
        quantity=20,
        max_cost=2.0,
    )
    task_spec = OrderSpec(
        scenario=Scenario.SOCIAL_TRAFFIC,
        exchange="fake_task_exchange",
        target="https://example.com/landing",
        quantity=3,
        source_platform=SourcePlatform.VK,
        max_cost=2.0,
    )

    panel_uuid, panel_ext, panel_cost = await orch.create_order(panel_spec, actor="demo")
    task_uuid, task_ext, task_cost = await orch.create_order(task_spec, actor="demo")
    print(
        f"[demo] panel order created: client={panel_uuid[:8]}... "
        f"ext={panel_ext} cost={panel_cost:.2f}"
    )
    print(
        f"[demo] task  order created: client={task_uuid[:8]}... ext={task_ext} cost={task_cost:.2f}"
    )

    print("\n[demo] polling until both orders terminal...")
    for tick in range(1, 11):
        panel_status = await orch.poll_order(panel_uuid, actor="demo")
        task_status = await orch.poll_order(task_uuid, actor="demo")
        print(f"  tick {tick}: panel={panel_status.value:10}  task={task_status.value}")
        if panel_status in TERMINAL_STATUSES and task_status in TERMINAL_STATUSES:
            break

    await _print_demo_summary(settings)
    print("\n[demo] done.")
    return 0


async def _print_demo_summary(settings: Settings) -> None:
    async with connect(settings) as conn:
        print("\n[demo] orders:")
        cur = await conn.execute(
            "SELECT client_order_uuid, exchange, status, cost_actual "
            "FROM orders ORDER BY created_at"
        )
        async for row in cur:
            print(
                f"  {row['client_order_uuid'][:8]}...  "
                f"exchange={row['exchange']:20}  "
                f"status={row['status']:10}  "
                f"cost={row['cost_actual']}"
            )

        print("\n[demo] submissions:")
        cur = await conn.execute(
            "SELECT submission_uuid, external_submission_id, status, evidence "
            "FROM submissions ORDER BY created_at"
        )
        any_subs = False
        async for row in cur:
            any_subs = True
            print(
                f"  {row['submission_uuid'][:8]}...  "
                f"ext={row['external_submission_id']:24}  "
                f"status={row['status']:18}  "
                f"evidence={row['evidence']}"
            )
        if not any_subs:
            print("  (none)")

        print("\n[demo] payments (terminal money decisions, C2 UNIQUE-key protected):")
        cur = await conn.execute(
            "SELECT exchange, external_submission_id, action, decided_by "
            "FROM payments ORDER BY decided_at"
        )
        any_pay = False
        async for row in cur:
            any_pay = True
            print(
                f"  exchange={row['exchange']:20}  "
                f"ext={row['external_submission_id']:24}  "
                f"action={row['action']:6}  "
                f"by={row['decided_by']}"
            )
        if not any_pay:
            print("  (none)")

        print("\n[demo] action_log (in-flight + completed actions):")
        cur = await conn.execute(
            "SELECT action_uuid, action, state, error FROM action_log ORDER BY started_at"
        )
        any_act = False
        async for row in cur:
            any_act = True
            err = row["error"] or "-"
            print(
                f"  {row['action_uuid'][:8]}...  "
                f"action={row['action']:6}  "
                f"state={row['state']:10}  "
                f"error={err}"
            )
        if not any_act:
            print("  (none)")

        n_audit = await count_audit_entries(conn)
    print(f"\n[demo] audit_log entries: {n_audit}")


# --- argparse / dispatch ------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="exchange-monitor-bot")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("smoke", help="Day 1 smoke harness - verify foundation wiring")

    p_create = sub.add_parser(
        "create-order",
        help="Place an order on the chosen exchange (Day 2)",
    )
    p_create.add_argument(
        "--exchange",
        required=True,
        choices=sorted(PANEL_EXCHANGES | TASK_EXCHANGES | {FAKE_PANEL_NAME, FAKE_TASK_NAME}),
    )
    p_create.add_argument("--scenario", required=True, choices=[s.value for s in Scenario])
    p_create.add_argument("--target", required=True, help="URL / account / post link")
    p_create.add_argument("--quantity", required=True, type=int)
    p_create.add_argument("--max-cost", required=True, type=float, dest="max_cost")
    p_create.add_argument(
        "--service-id",
        dest="service_id",
        help="Exchange-specific service catalogue id (required for smmcode/prskill)",
    )
    p_create.add_argument(
        "--source-platform",
        dest="source_platform",
        choices=[p.value for p in SourcePlatform],
        help="Traffic source platform (required for social_traffic scenario)",
    )
    dry_group = p_create.add_mutually_exclusive_group()
    dry_group.add_argument(
        "--dry-run",
        dest="dry_run",
        action="store_true",
        default=None,
        help="Force DRY_RUN regardless of settings",
    )
    dry_group.add_argument(
        "--live",
        dest="dry_run",
        action="store_false",
        help="Force LIVE mode (override DRY_RUN=true in .env)",
    )

    sub.add_parser(
        "demo",
        help="Day 3 - full lifecycle via Orchestrator on fake adapters (DRY_RUN)",
    )

    p_start = sub.add_parser(
        "start",
        help="v4 — start FastAPI tools backend + dashboard + scheduler (for OpenClaw agent)",
    )
    p_start.add_argument("--host", default=None, help="override APP_HOST")
    p_start.add_argument("--port", type=int, default=None, help="override APP_PORT")
    p_start.add_argument("--reload", action="store_true", help="uvicorn --reload (dev)")

    sub.add_parser(
        "agent-smoke",
        help=(
            "v4 — sanity-check the new agent stack "
            "(imports + DB schema + adapter+verifier registries)"
        ),
    )

    return parser


def _start_app(settings: Settings, args: argparse.Namespace) -> int:
    """Launch uvicorn against app.main:app. The FastAPI lifespan handles
    DB init, adapter/verifier registry, APScheduler. OpenClaw runs in a
    separate process and points at this server via APP_BASE_URL."""
    import uvicorn  # local import: uvicorn is optional for the smoke/demo commands

    host = args.host or settings.app_host
    port = args.port or settings.app_port
    print(f"[start] FastAPI + scheduler on http://{host}:{port}")
    print(f"[start] dashboard:  http://{host}:{port}/dashboard")
    print(f"[start] tools base: http://{host}:{port}/api/tools/*")
    print(
        "[start] OpenClaw should point APP_BASE_URL at the same host:port "
        "and use AGENT_TOOLS_TOKEN as Bearer."
    )
    uvicorn.run(
        "app.main:app",
        host=host,
        port=port,
        reload=args.reload,
        log_level="info",
    )
    return 0


async def _agent_smoke(settings: Settings) -> int:
    """v4 sanity: DB schema applies, adapters instantiate, verifiers register."""
    from app.state import build_app_state

    await init_db(settings)
    print(f"[agent-smoke] DB schema applied at {settings.db_path}")
    state = await build_app_state()
    try:
        print(f"[agent-smoke] adapters loaded: {sorted(state.adapters.keys())}")
        print(f"[agent-smoke] verifiers loaded: {[v.name for v in state.verifiers]}")
        for name, adapter in state.adapters.items():
            caps = sorted(c.value for c in adapter.capabilities())
            print(f"  - {name}: {caps}")
        # Try a quote across adapters in DRY_RUN to surface the mock pipeline.
        from models import SourcePlatform as _SP
        from models import TaskType as _TT

        quotes = []
        for _name, adapter in state.adapters.items():
            q = await adapter.get_quote(_TT.LIKES, _SP.YOUTUBE, 200)
            if q is not None:
                quotes.append(q)
        quotes.sort(key=lambda q: q.total_price)
        print("[agent-smoke] sample quotes for 200 YT likes (sorted):")
        for q in quotes:
            print(
                f"  - {q.exchange}: ${q.total_price:.3f} "
                f"({q.eta_minutes_min}-{q.eta_minutes_max} min, conf={q.confidence})"
            )
        print("[agent-smoke] OK")
    finally:
        await state.http_client.aclose()
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    settings = get_settings()
    if args.command == "smoke":
        asyncio.run(smoke(settings))
        return 0
    if args.command == "create-order":
        return asyncio.run(create_order_command(settings, args))
    if args.command == "demo":
        return asyncio.run(demo_command(settings))
    if args.command == "start":
        return _start_app(settings, args)
    if args.command == "agent-smoke":
        return asyncio.run(_agent_smoke(settings))
    return 2  # unreachable - argparse requires command


if __name__ == "__main__":
    sys.exit(main())
