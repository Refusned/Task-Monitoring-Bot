"""Live round-trip: prove the bot REALLY talks to real exchanges, with $0 risk.

Strategy: drive each real adapter through the same code path the bot uses, but
set `max_cost` so low that the estimate-first check rejects placement BEFORE
any /create_order or /addOrder endpoint is touched. This verifies:

1. We can connect, authenticate, parse responses against live APIs.
2. The cost cap actually fires against real catalogue prices.
3. The bot's adapter code paths work end-to-end on the real wire.

These tests spend ZERO money. They will fail (with a clear message) if the
estimate-first guard is broken or a credential is bad.
"""

from __future__ import annotations

import sys
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from adapters.advego import AdvegoAdapter  # noqa: E402
from adapters.smmcode import SmmcodeAdapter  # noqa: E402
from adapters.unu import UnuAdapter  # noqa: E402
from config import get_settings  # noqa: E402
from models import OrderSpec, Scenario, SourcePlatform  # noqa: E402


@pytest.fixture
def settings():
    return get_settings()


def _http():
    return httpx.AsyncClient(timeout=30.0, follow_redirects=True)


# ---------------------------------------------------------------------------
# smmcode — cost cap MUST fire against live catalogue
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_smmcode_cost_cap_rejects_before_create(settings):
    """Real smmcode `/services` → estimate-first → ValueError BEFORE create_order.

    A working bot must refuse to place an over-budget order, period. This
    test calls the live API but never reaches /create_order.
    """
    if not settings.smmcode_api_key:
        pytest.skip("SMMCODE_API_KEY not set")

    async with _http() as http:
        adapter = SmmcodeAdapter(settings.smmcode_api_key, http)
        # Find any real service in the catalogue.
        services = await adapter._get_services()
        assert services, "live smmcode returned empty catalogue"
        sample_id = next(iter(services))
        price = float(services[sample_id]["price"])

        # max_cost set far below price * quantity → estimate-first must reject.
        spec = OrderSpec(
            scenario=Scenario.ACTIVITY_SUBSCRIBE,
            exchange="smmcode",
            target="https://t.me/test_canary",  # never actually used
            quantity=100,
            service_id=sample_id,
            max_cost=price * 0.001,  # 0.1% of one-unit price
        )
        with pytest.raises(ValueError, match=r"exceeds spec\.max_cost"):
            await adapter.create_order(spec, "canary-uuid")


# ---------------------------------------------------------------------------
# unu — cost cap MUST fire against live tariffs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unu_cost_cap_rejects_before_create(settings):
    if not settings.unu_api_key:
        pytest.skip("UNU_API_KEY not set")

    async with _http() as http:
        adapter = UnuAdapter(settings.unu_api_key, http)
        tariffs = await adapter._get_tariffs()
        assert tariffs, "live unu returned empty tariffs"

        # Mirror the adapter's actual price-resolution logic: try min_price_rub
        # first (the field UNU currently uses), then price, then cost.
        def _resolve_price(tariff: dict) -> float:
            for key in ("min_price_rub", "price", "cost"):
                raw = tariff.get(key)
                if raw is None:
                    continue
                try:
                    val = float(raw)
                except (TypeError, ValueError):
                    continue
                if val > 0:
                    return val
            return 0.0

        # Pick the cheapest tariff that has a real price.
        priced = [(tid, t, _resolve_price(t)) for tid, t in tariffs.items()]
        priced = [(tid, t, p) for tid, t, p in priced if p > 0]
        assert priced, "live unu: no tariff with a usable price field"
        sample_id, _tariff, price = min(priced, key=lambda x: x[2])

        spec = OrderSpec(
            scenario=Scenario.SOCIAL_TRAFFIC,
            exchange="unu",
            target="https://example.com/canary",
            quantity=100,
            service_id=str(sample_id),
            source_platform=SourcePlatform.VK,
            max_cost=price * 0.001,
        )
        with pytest.raises(ValueError, match=r"exceeds spec\.max_cost"):
            await adapter.create_order(spec, "canary-uuid")


# ---------------------------------------------------------------------------
# advego — cost cap MUST fire against live order types
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_advego_cost_cap_rejects_before_create(settings):
    if not settings.advego_api_token:
        pytest.skip("ADVEGO_API_TOKEN not set")

    async with _http() as http:
        adapter = AdvegoAdapter(settings.advego_api_token, http)
        # Pick order_type 18 (likes) — explicitly listed in docs/api/advego.md.
        spec = OrderSpec(
            scenario=Scenario.ACTIVITY_LIKE,
            exchange="advego",
            target="https://vk.com/wall_canary",
            quantity=100,
            service_id="18",
            max_cost=0.001,  # almost certainly below 100 × min_cost (~6 RUB)
        )
        with pytest.raises(ValueError, match=r"exceeds spec\.max_cost"):
            await adapter.create_order(spec, "canary-uuid")


# ---------------------------------------------------------------------------
# Round-trip via the bot's persist+create code path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bot_create_path_refuses_over_budget_on_smmcode(settings, tmp_path):
    """Full bot machinery: _persist_and_create against the LIVE smmcode.

    Expectation: CREATING row is written → adapter rejects on cost → row is
    moved to FAILED with an audit entry. No /create_order endpoint touched.
    """
    if not settings.smmcode_api_key:
        pytest.skip("SMMCODE_API_KEY not set")

    from cli import _persist_and_create
    from db.database import connect, init_db
    from models import OrderStatus

    test_settings = settings.model_copy(update={"db_path": tmp_path / "live_canary.db"})
    await init_db(test_settings)

    async with _http() as http:
        adapter = SmmcodeAdapter(test_settings.smmcode_api_key, http)
        services = await adapter._get_services()
        sample_id = next(iter(services))

        spec = OrderSpec(
            scenario=Scenario.ACTIVITY_SUBSCRIBE,
            exchange="smmcode",
            target="https://t.me/test_canary",
            quantity=100,
            service_id=sample_id,
            max_cost=0.001,  # well below catalogue price
        )
        with pytest.raises(ValueError, match=r"exceeds spec\.max_cost"):
            await _persist_and_create(test_settings, adapter, spec, actor="live_canary")

    async with connect(test_settings) as conn:
        cur = await conn.execute(
            "SELECT client_order_uuid, status FROM orders WHERE exchange = ?", ("smmcode",)
        )
        row = await cur.fetchone()
        cur2 = await conn.execute(
            "SELECT event FROM audit_log WHERE order_uuid = ? ORDER BY audit_id",
            (row["client_order_uuid"],),
        )
        events = [r["event"] for r in await cur2.fetchall()]

    assert row is not None, "expected the CREATING row to be persisted"
    assert row["status"] == OrderStatus.FAILED.value, (
        "estimate-first must move the order to FAILED, not leave it CREATING"
    )
    assert "order_creating" in events
    assert "order_create_failed" in events
    # The order MUST NOT have reached 'order_active'.
    assert "order_active" not in events
    recovered = await _get_order_helper(test_settings, row["client_order_uuid"])
    assert recovered.status == OrderStatus.FAILED


async def _get_order_helper(settings, client_uuid):
    from db.database import connect, get_order

    async with connect(settings) as conn:
        return await get_order(conn, client_uuid)
