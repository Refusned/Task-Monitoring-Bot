"""Day 1 smoke tests: foundation wiring for DB and real adapter contracts."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from adapters.base import Capability
from adapters.smmcode import SmmcodeAdapter
from adapters.unu import UnuAdapter
from config import Settings
from db.database import (
    append_audit,
    connect,
    count_audit_entries,
    init_db,
)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(db_path=tmp_path / "test.db")


async def test_init_db_idempotent(settings: Settings) -> None:
    await init_db(settings)
    await init_db(settings)  # second call must not error


async def test_smmcode_capabilities() -> None:
    transport = httpx.MockTransport(lambda r: httpx.Response(500))
    async with httpx.AsyncClient(transport=transport) as http:
        adapter = SmmcodeAdapter("token", http)
        caps = adapter.capabilities()
        assert Capability.CREATE_ORDER in caps
        assert Capability.GET_ORDER_STATUS in caps
        assert Capability.ACCEPT_SUBMISSION not in caps


async def test_unu_capabilities() -> None:
    transport = httpx.MockTransport(lambda r: httpx.Response(500))
    async with httpx.AsyncClient(transport=transport) as http:
        adapter = UnuAdapter("token", http)
        caps = adapter.capabilities()
        assert Capability.LIST_SUBMISSIONS in caps
        assert Capability.ACCEPT_SUBMISSION in caps
        assert Capability.REJECT_SUBMISSION in caps


async def test_audit_log_appends(settings: Settings) -> None:
    await init_db(settings)
    async with connect(settings) as conn:
        before = await count_audit_entries(conn)
        await append_audit(conn, actor="test", event="ping")
        after = await count_audit_entries(conn)
    assert after == before + 1
