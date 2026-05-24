"""End-to-end test for the CLI demo command."""

from __future__ import annotations

import argparse
import os
import tempfile
from pathlib import Path

import pytest

from cli import demo_command
from config import Settings
from db.database import connect, get_order, init_db


@pytest.fixture
async def demo_db():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = f.name
    settings = Settings(
        dry_run=True,
        db_path=Path(path),
        auto_accept_threshold_cost=0.0,
        daily_spend_limit=1000.0,
        per_order_spend_limit=200.0,
    )
    await init_db(settings)
    yield settings
    os.unlink(path)


@pytest.mark.asyncio
async def test_demo_runs_without_error(demo_db: Settings) -> None:
    """Demo creates orders, polls them, and reaches terminal states."""
    args = argparse.Namespace()
    exit_code = await demo_command(demo_db, args)
    assert exit_code == 0

    async with connect(demo_db) as conn:
        cursor = await conn.execute("SELECT client_order_uuid FROM orders")
        rows = await cursor.fetchall()
        orders = [row[0] for row in rows]
        assert len(orders) == 2

        # At least one should be terminal
        terminal = {"completed", "failed"}
        statuses = []
        for cid in orders:
            o = await get_order(conn, cid)
            statuses.append(o.status.value if o else "missing")
        assert any(s in terminal for s in statuses), f"statuses={statuses}"
