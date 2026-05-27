"""Tests for the browser dashboard API."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

from fastapi.testclient import TestClient

from config import Settings
from db.database import connect, init_db, insert_order_creating
from models import Order, OrderSpec, OrderStatus, Scenario
from web_dashboard.app import create_app


def _settings(tmp_path: Path, *, token: str = "") -> Settings:
    return Settings(
        dry_run=True,
        db_path=tmp_path / "dashboard.db",
        telegram_admin_ids=[42],
        web_dashboard_token=token,
    )


async def _seed_order(settings: Settings) -> str:
    await init_db(settings)
    now = datetime.now(UTC)
    order = Order(
        client_order_uuid="dash-order-1",
        spec=OrderSpec(
            scenario=Scenario.ACTIVITY_VIEW,
            exchange="smmcode",
            target="https://youtube.com/watch?v=dQw4w9WgXcQ",
            quantity=100,
            service_id="views",
            max_cost=10.0,
        ),
        status=OrderStatus.CREATING,
        created_at=now,
        updated_at=now,
    )
    async with connect(settings) as conn:
        await insert_order_creating(conn, order)
    return order.client_order_uuid


def test_dashboard_serves_browser_shell(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    client = TestClient(create_app(lambda: settings))

    response = client.get("/")

    assert response.status_code == 200
    assert "Task Monitoring Bot" in response.text
    assert "LLM-автопилот" in response.text


def test_dashboard_serves_favicon(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    client = TestClient(create_app(lambda: settings))

    response = client.get("/favicon.ico")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("image/svg+xml")


def test_dashboard_overview_reads_sqlite_state(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    asyncio.run(_seed_order(settings))
    client = TestClient(create_app(lambda: settings))

    response = client.get("/api/overview")

    assert response.status_code == 200
    payload = response.json()
    assert payload["mode"] == "DRY_RUN"
    assert payload["active_orders"] == 1
    assert payload["orders_by_status"] == {"creating": 1}
    assert payload["recent_orders"][0]["client_order_uuid"] == "dash-order-1"


def test_dashboard_token_protects_api(tmp_path: Path) -> None:
    settings = _settings(tmp_path, token="secret-token")
    client = TestClient(create_app(lambda: settings))

    denied = client.get("/api/overview")
    allowed = client.get("/api/overview", headers={"Authorization": "Bearer secret-token"})

    assert denied.status_code == 401
    assert allowed.status_code == 200


def test_dashboard_lists_orders(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    asyncio.run(_seed_order(settings))
    client = TestClient(create_app(lambda: settings))

    response = client.get("/api/orders")

    assert response.status_code == 200
    rows = response.json()
    assert rows[0]["client_order_uuid"] == "dash-order-1"
    assert rows[0]["scenario"] == "activity_view"
