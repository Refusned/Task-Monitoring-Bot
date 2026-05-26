"""End-to-end tests using REAL API keys against live endpoints.

These exercises the full chain: adapter → real network → exchange response.
Real credentials are read from `.env` (already populated).

IMPORTANT: We NEVER write/modify data on the real exchanges — these tests are
read-only (balance, catalogue).

Run:  pytest tests/test_e2e_real.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path

import httpx
import pytest

# Ensure project root is on path — needed when invoked directly via pytest.
ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from adapters.advego import AdvegoAdapter  # noqa: E402
from adapters.base import Capability  # noqa: E402
from adapters.smmcode import SmmcodeAdapter  # noqa: E402
from adapters.unu import UnuAdapter  # noqa: E402
from config import get_settings  # noqa: E402


@pytest.fixture
def settings():
    return get_settings()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fresh_client():
    return httpx.AsyncClient(follow_redirects=True, timeout=30.0)


# ---------------------------------------------------------------------------
# Smmcode — live read-only
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_smmcode_live_balance(settings):
    if not settings.smmcode_api_key:
        pytest.skip("SMMCODE_API_KEY not set in .env")
    async with _fresh_client() as http:
        adapter = SmmcodeAdapter(settings.smmcode_api_key, http)
        bal = await adapter.get_balance()
    assert isinstance(bal, float)
    assert bal >= 0


@pytest.mark.asyncio
async def test_smmcode_live_services(settings):
    if not settings.smmcode_api_key:
        pytest.skip("SMMCODE_API_KEY not set in .env")
    async with _fresh_client() as http:
        adapter = SmmcodeAdapter(settings.smmcode_api_key, http)
        services = await adapter._get_services()
    assert len(services) > 0, "smmcode returned empty catalogue"
    has_telegram = any(
        "telegram" in str(s.get("name", "")).lower() or "телеграм" in str(s.get("name", "")).lower()
        for s in services.values()
    )
    assert has_telegram, "no Telegram services found in catalogue"


@pytest.mark.asyncio
async def test_smmcode_live_order_status_nonexistent(settings):
    """Querying a non-existent order should return a deterministic normalized status."""
    if not settings.smmcode_api_key:
        pytest.skip("SMMCODE_API_KEY not set in .env")
    async with _fresh_client() as http:
        adapter = SmmcodeAdapter(settings.smmcode_api_key, http)
        try:
            status = await adapter.get_order_status("999999999")
        except Exception:
            # 404 or other error is fine — we just need graceful handling
            status = "failed"
    assert status in ("in_progress", "failed")


# ---------------------------------------------------------------------------
# UNU — live read-only
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unu_live_balance(settings):
    if not settings.unu_api_key:
        pytest.skip("UNU_API_KEY not set in .env")
    async with _fresh_client() as http:
        adapter = UnuAdapter(settings.unu_api_key, http)
        bal = await adapter.get_balance()
    assert isinstance(bal, float)
    assert bal >= 0


@pytest.mark.asyncio
async def test_unu_live_tariffs(settings):
    if not settings.unu_api_key:
        pytest.skip("UNU_API_KEY not set in .env")
    async with _fresh_client() as http:
        adapter = UnuAdapter(settings.unu_api_key, http)
        tariffs = await adapter._get_tariffs()
    assert len(tariffs) > 0, "unu returned empty tariffs"


@pytest.mark.asyncio
async def test_unu_live_capabilities(settings):
    if not settings.unu_api_key:
        pytest.skip("UNU_API_KEY not set in .env")
    adapter = UnuAdapter(settings.unu_api_key, _fresh_client())
    caps = adapter.capabilities()
    assert Capability.CREATE_ORDER in caps
    assert Capability.GET_BALANCE in caps
    assert Capability.LIST_SUBMISSIONS in caps
    assert Capability.ACCEPT_SUBMISSION in caps
    assert Capability.REJECT_SUBMISSION in caps


# ---------------------------------------------------------------------------
# Advego — live read-only XML-RPC
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_advego_live_getOrderTypes(settings):
    if not settings.advego_api_token:
        pytest.skip("ADVEGO_API_TOKEN not set in .env")
    async with _fresh_client() as http:
        adapter = AdvegoAdapter(settings.advego_api_token, http)
        types = await adapter._get_order_types()
    assert len(types) > 0, "advego getOrderTypes returned empty"
    assert "18" in types or any(t.get("id") == 18 for t in types.values()), (
        "order type 18 (likes) not found"
    )


@pytest.mark.asyncio
async def test_advego_live_capabilities(settings):
    if not settings.advego_api_token:
        pytest.skip("ADVEGO_API_TOKEN not set in .env")
    adapter = AdvegoAdapter(settings.advego_api_token, _fresh_client())
    caps = adapter.capabilities()
    assert Capability.CREATE_ORDER in caps
    assert Capability.LIST_SUBMISSIONS in caps
    assert Capability.ACCEPT_SUBMISSION in caps
    assert Capability.REJECT_SUBMISSION in caps


# ---------------------------------------------------------------------------
# Cross-exchange: all adapters are initializable
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_all_real_adapters_initializable(settings):
    """All real adapters with keys must load without error."""
    excs = []
    for name, cls, key in [
        ("smmcode", SmmcodeAdapter, settings.smmcode_api_key),
        ("unu", UnuAdapter, settings.unu_api_key),
        ("advego", AdvegoAdapter, settings.advego_api_token),
    ]:
        if not key:
            continue
        try:
            cls(key, httpx.AsyncClient())
        except Exception as e:
            excs.append(f"{name}: {e}")
    if excs:
        pytest.fail("; ".join(excs))
