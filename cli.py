"""CLI entry point.

Commands:
- `smoke` (Day 1) - wiring check for config and database.
- `create-order` (Day 2) - place an order on the chosen exchange.
- `autopilot` - parse a natural-language goal via Ollama and place the cheapest viable order.
- `demo` (Day 3+) - disabled since simulated exchanges were removed.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import UTC, datetime

import httpx
from pydantic import ValidationError

from adapters.advego import AdvegoAdapter
from adapters.base import ExchangeAdapter, PanelAdapter, TaskExchangeAdapter
from adapters.ipgold import IpgoldAdapter
from adapters.prskill import PrskillAdapter
from adapters.smmcode import SmmcodeAdapter
from adapters.unu import UnuAdapter
from config import Settings, get_settings
from db.database import (
    append_audit,
    connect,
    count_audit_entries,
    get_order,
    init_db,
    insert_order_creating,
    mark_order_active,
    update_order_status,
)
from models import (
    Order,
    OrderSpec,
    OrderStatus,
    Scenario,
    SourcePlatform,
    new_client_order_uuid,
)
from orchestrator import Orchestrator

PANEL_EXCHANGES = {"smmcode", "prskill"}
TASK_EXCHANGES = {"unu", "advego", "ipgold"}


def build_adapter(
    settings: Settings,
    exchange_name: str,
    http_client: httpx.AsyncClient,
    *,
    dry_run: bool,
) -> ExchangeAdapter:
    """Pick the right adapter for an exchange.

    DRY_RUN is enforced above the adapter layer; this factory only builds real adapters.
    """
    if exchange_name == "smmcode":
        return SmmcodeAdapter(settings.smmcode_api_key, http_client)
    if exchange_name == "prskill":
        return PrskillAdapter(settings.prskill_api_key, http_client)
    if exchange_name == "unu":
        return UnuAdapter(settings.unu_api_key, http_client)
    if exchange_name == "advego":
        return AdvegoAdapter(settings.advego_api_token, http_client)
    if exchange_name == "ipgold":
        return IpgoldAdapter(settings.ipgold_api_key, http_client)
    raise NotImplementedError(f"real adapter for {exchange_name!r} not implemented yet")


async def _persist_and_create(
    settings: Settings,
    adapter: ExchangeAdapter,
    spec: OrderSpec,
    actor: str,
) -> tuple[str, str, float]:
    """Persist CREATING row, call adapter.create_order, mark ACTIVE."""
    if not isinstance(adapter, (PanelAdapter, TaskExchangeAdapter)):
        raise TypeError(f"adapter {adapter.name!r} cannot create orders")

    now = datetime.now(UTC)
    client_uuid = new_client_order_uuid()
    order = Order(
        client_order_uuid=client_uuid,
        spec=spec,
        status=OrderStatus.CREATING,
        created_at=now,
        updated_at=now,
    )
    async with connect(settings) as conn:
        await insert_order_creating(conn, order)
        await append_audit(
            conn,
            actor=actor,
            event="order_creating",
            order_uuid=client_uuid,
            details={
                "exchange": spec.exchange,
                "scenario": spec.scenario.value,
                "target": spec.target,
            },
        )

    try:
        external_id, cost = await adapter.create_order(spec, client_uuid)
    except Exception as exc:
        async with connect(settings) as conn:
            await update_order_status(conn, client_uuid, OrderStatus.FAILED)
            await append_audit(
                conn,
                actor=actor,
                event="order_create_failed",
                order_uuid=client_uuid,
                details={"error": str(exc), "error_type": type(exc).__name__},
            )
        raise

    async with connect(settings) as conn:
        await mark_order_active(conn, client_uuid, external_id, cost)
        await append_audit(
            conn,
            actor=actor,
            event="order_active",
            order_uuid=client_uuid,
            details={"external_order_id": external_id, "cost": cost},
        )
    return client_uuid, external_id, cost


# --- smoke (Day 1) ------------------------------------------------------------


async def _smoke_adapter(
    adapter: ExchangeAdapter, spec: OrderSpec, settings: Settings, label: str
) -> None:
    print(f"\n[Day 1 smoke] === {label} ===")
    caps = sorted(c.value for c in adapter.capabilities())
    print(f"  capabilities: {caps}")
    balance = await adapter.get_balance()
    print(f"  balance: {balance:.2f}")

    client_uuid, external_id, cost = await _persist_and_create(
        settings, adapter, spec, actor="smoke"
    )
    print(f"  created: ext={external_id} cost={cost:.2f}")

    for i in range(1, 4):
        # smoke adapter is always Panel or Task (has get_order_status), but
        # mypy sees only ExchangeAdapter → narrow explicitly.
        raw_status = await adapter.get_order_status(external_id)  # type: ignore[attr-defined]
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
        print(f"  list_submissions: {len(subs)} pending")

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

    async with connect(settings) as conn:
        n_audit = await count_audit_entries(conn)
    print(f"\n[Day 1 smoke] audit_log entries: {n_audit}")
    print("[Day 1 smoke] configured exchanges:")
    configured = {
        "smmcode": bool(settings.smmcode_api_key),
        "prskill": bool(settings.prskill_api_key),
        "unu": bool(settings.unu_api_key),
        "advego": bool(settings.advego_api_token),
        "ipgold": bool(settings.ipgold_api_key),
    }
    for name, has_key in configured.items():
        print(f"  {name}: {'configured' if has_key else 'missing credentials'}")
    print("[Day 1 smoke] done - foundation wiring works.")


# --- create-order (Day 2) -----------------------------------------------------


async def create_order_command(settings: Settings, args: argparse.Namespace) -> int:
    dry_run = args.dry_run if args.dry_run is not None else settings.dry_run
    if dry_run:
        print(
            "[create-order] DRY_RUN=true: order creation is disabled because simulated "
            "exchanges were removed.",
            file=sys.stderr,
        )
        return 2

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
        try:
            client_uuid, external_id, cost = await _persist_and_create(
                settings, adapter, spec, actor="cli"
            )
        except Exception as exc:
            print(
                f"[create-order] FAILED: {type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
            return 1
    print(f"[create-order] OK: client_uuid={client_uuid} external_id={external_id} cost={cost:.2f}")
    return 0


# --- orchestrator commands (Day 3+) -----------------------------------------


def _build_adapters(
    settings: Settings,
    *,
    dry_run: bool,
    http_client: httpx.AsyncClient | None = None,
) -> dict[str, ExchangeAdapter]:
    """Build all adapters keyed by exchange name."""
    adapters: dict[str, ExchangeAdapter] = {}
    if http_client is None:
        raise ValueError("http_client is required for adapters")
    for name in sorted(PANEL_EXCHANGES | TASK_EXCHANGES):
        try:
            adapters[name] = build_adapter(settings, name, http_client, dry_run=dry_run)
        except ValueError:
            # Missing credentials disable that exchange for this process. Orders
            # already stored for it will be surfaced as "no adapter" by the orchestrator.
            continue
    return adapters


async def monitor_command(settings: Settings, args: argparse.Namespace) -> int:
    dry_run = args.dry_run if args.dry_run is not None else settings.dry_run
    print(f"[monitor] mode={'DRY_RUN' if dry_run else 'LIVE'}")
    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as http_client:
        adapters = _build_adapters(settings, dry_run=dry_run, http_client=http_client)
        orch = Orchestrator(settings, adapters)
        results = await orch.poll_all()
    print(f"[monitor] polled {len(results)} orders")
    for r in results:
        print(f"  {r['order_uuid'][:8]}.. {r.get('status')} {r.get('exchange', '')}")
    return 0


async def verify_command(settings: Settings, args: argparse.Namespace) -> int:
    order_uuid = args.order_uuid
    dry_run = args.dry_run if args.dry_run is not None else settings.dry_run
    print(f"[verify] mode={'DRY_RUN' if dry_run else 'LIVE'} order_uuid={order_uuid}")
    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as http_client:
        adapters = _build_adapters(settings, dry_run=dry_run, http_client=http_client)
        orch = Orchestrator(settings, adapters)
        result = await orch.verify_single_order(order_uuid)
    print(f"[verify] result: {result}")
    return 0


async def demo_command(settings: Settings, args: argparse.Namespace) -> int:
    """Explain why the old simulated lifecycle is no longer available."""
    print("[demo] disabled: simulated exchanges were removed.")
    print(
        "[demo] use `smoke` for local wiring checks or `monitor --dry-run` for read-only polling."
    )
    return 0


async def autopilot_command(settings: Settings, args: argparse.Namespace) -> int:
    """LLM-driven goal -> cheapest viable service -> optional live order."""
    from autopilot.ollama import OllamaPlanner
    from autopilot.runner import AutopilotRunner, format_autopilot_result
    from verification.activity_metrics import build_activity_metrics_provider

    goal_text = args.goal or " ".join(args.goal_words or [])
    goal_text = goal_text.strip()
    if not goal_text:
        print("[autopilot] goal text is required", file=sys.stderr)
        return 2

    dry_run = args.dry_run if args.dry_run is not None else settings.dry_run
    effective_settings = settings.model_copy(update={"dry_run": dry_run})
    await init_db(effective_settings)

    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as http_client:
        adapters = _build_adapters(
            effective_settings,
            dry_run=dry_run,
            http_client=http_client,
        )
        planner = OllamaPlanner(
            base_url=effective_settings.ollama_base_url,
            model=effective_settings.ollama_model,
            http_client=http_client,
            timeout_seconds=effective_settings.ollama_timeout_seconds,
        )
        metrics_provider = build_activity_metrics_provider(
            youtube_api_key=effective_settings.youtube_data_api_key,
            http_client=http_client,
        )
        runner = AutopilotRunner(
            effective_settings,
            adapters,
            planner,
            activity_metrics_provider=metrics_provider,
        )
        result = await runner.run_goal(
            goal_text,
            actor="cli:autopilot",
            execute=not args.plan_only,
        )

    print(format_autopilot_result(result))
    return 0 if result.status in {"created", "planned", "dry_run"} else 1


def dashboard_command(settings: Settings, args: argparse.Namespace) -> int:
    """Run the browser dashboard."""
    import uvicorn

    from web_dashboard.app import create_app

    host = args.host or settings.web_dashboard_host
    port = args.port or settings.web_dashboard_port
    effective_settings = settings.model_copy(
        update={
            "web_dashboard_host": host,
            "web_dashboard_port": port,
        }
    )
    if not effective_settings.web_dashboard_token and not _is_loopback_host(host):
        print(
            "[dashboard] WEB_DASHBOARD_TOKEN is required when binding outside localhost",
            file=sys.stderr,
        )
        return 2
    if not effective_settings.web_dashboard_token:
        print("[dashboard] WEB_DASHBOARD_TOKEN is empty; allowing localhost-only access")
    print(f"[dashboard] starting at http://{host}:{port}")
    uvicorn.run(create_app(lambda: effective_settings), host=host, port=port, log_level="info")
    return 0


def _is_loopback_host(host: str) -> bool:
    return host in {"127.0.0.1", "localhost", "::1"}


# --- argparse / dispatch ------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="exchange-monitor-bot")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("smoke", help="Day 1 smoke harness - verify foundation wiring")

    p_monitor = sub.add_parser("monitor", help="One-shot poll of active orders via orchestrator")
    dry_group_monitor = p_monitor.add_mutually_exclusive_group()
    dry_group_monitor.add_argument("--dry-run", dest="dry_run", action="store_true", default=None)
    dry_group_monitor.add_argument("--live", dest="dry_run", action="store_false")

    p_verify = sub.add_parser("verify", help="Run verification on a single order by UUID")
    p_verify.add_argument("--order-uuid", required=True, dest="order_uuid")
    dry_group_verify = p_verify.add_mutually_exclusive_group()
    dry_group_verify.add_argument("--dry-run", dest="dry_run", action="store_true", default=None)
    dry_group_verify.add_argument("--live", dest="dry_run", action="store_false")

    sub.add_parser("demo", help="Full DRY_RUN lifecycle demo (create + poll + verify)")

    p_dashboard = sub.add_parser(
        "dashboard",
        help="Run the browser dashboard for bot operations",
    )
    p_dashboard.add_argument("--host", help="Bind host, defaults to WEB_DASHBOARD_HOST")
    p_dashboard.add_argument("--port", type=int, help="Bind port, defaults to WEB_DASHBOARD_PORT")

    p_autopilot = sub.add_parser(
        "autopilot",
        help="Use Ollama to parse a goal and automatically choose the cheapest viable service",
    )
    p_autopilot.add_argument("goal_words", nargs="*", help="Goal text, e.g. '500 likes ...'")
    p_autopilot.add_argument("--goal", help="Goal text; overrides positional words")
    p_autopilot.add_argument(
        "--plan-only",
        action="store_true",
        help="Parse and select a service but do not create an order",
    )
    dry_group_autopilot = p_autopilot.add_mutually_exclusive_group()
    dry_group_autopilot.add_argument(
        "--dry-run",
        dest="dry_run",
        action="store_true",
        default=None,
        help="Force planning-only mode regardless of .env",
    )
    dry_group_autopilot.add_argument(
        "--live",
        dest="dry_run",
        action="store_false",
        help="Allow live order creation when DRY_RUN=false is also intended",
    )

    p_create = sub.add_parser(
        "create-order",
        help="Place an order on the chosen exchange (Day 2)",
    )
    p_create.add_argument(
        "--exchange",
        required=True,
        choices=sorted(PANEL_EXCHANGES | TASK_EXCHANGES),
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

    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    settings = get_settings()
    if args.command == "smoke":
        asyncio.run(smoke(settings))
        return 0
    if args.command == "create-order":
        return asyncio.run(create_order_command(settings, args))
    if args.command == "monitor":
        return asyncio.run(monitor_command(settings, args))
    if args.command == "verify":
        return asyncio.run(verify_command(settings, args))
    if args.command == "autopilot":
        return asyncio.run(autopilot_command(settings, args))
    if args.command == "dashboard":
        return dashboard_command(settings, args)
    if args.command == "demo":
        return asyncio.run(demo_command(settings, args))
    return 2  # unreachable - argparse requires command


if __name__ == "__main__":
    sys.exit(main())
