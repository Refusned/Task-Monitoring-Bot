from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app.main import require_dashboard_auth, require_tool_auth
from app.state import claim_snapshot_for_order, redact_secrets, save_snapshot
from config import Settings
from db.database import init_db


def _request(
    settings: Settings,
    *,
    authorization: str | None = None,
    cookie: str | None = None,
) -> Request:
    headers: list[tuple[bytes, bytes]] = []
    if authorization:
        headers.append((b"authorization", authorization.encode()))
    if cookie:
        headers.append((b"cookie", cookie.encode()))
    app = SimpleNamespace(state=SimpleNamespace(app_state=SimpleNamespace(settings=settings)))
    return Request({"type": "http", "headers": headers, "app": app})


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        db_path=tmp_path / "test.db",
        dashboard_token="dashboard-secret-123",
        agent_tools_token="agent-secret-456",
    )


async def test_tool_auth_rejects_dashboard_token(settings: Settings) -> None:
    with pytest.raises(HTTPException) as exc:
        await require_tool_auth(_request(settings, authorization="Bearer dashboard-secret-123"))
    assert exc.value.status_code == 403

    state = await require_tool_auth(_request(settings, authorization="Bearer agent-secret-456"))
    assert state.settings is settings


async def test_dashboard_auth_rejects_agent_token(settings: Settings) -> None:
    with pytest.raises(HTTPException) as exc:
        await require_dashboard_auth(
            _request(settings, authorization="Bearer agent-secret-456"),
            auth_token=None,
        )
    assert exc.value.status_code == 403

    state = await require_dashboard_auth(_request(settings), auth_token="dashboard-secret-123")
    assert state.settings is settings


def test_redact_secrets_removes_known_values(settings: Settings) -> None:
    payload = {
        "reply": "DASHBOARD_TOKEN: dashboard-secret-123",
        "nested": ["AGENT_TOOLS_TOKEN=agent-secret-456"],
    }
    redacted = redact_secrets(settings, payload)
    assert "dashboard-secret-123" not in str(redacted)
    assert "agent-secret-456" not in str(redacted)
    assert "[REDACTED_DASHBOARD_TOKEN]" in str(redacted)


async def test_snapshot_claim_requires_match_and_single_use(settings: Settings) -> None:
    await init_db(settings)
    await save_snapshot(
        settings,
        snapshot_id="snap-test",
        platform="youtube",
        target_url="https://youtu.be/dQw4w9WgXcQ",
        metric="likes",
        baseline_value=10,
        raw={"verifier": "test"},
    )

    bad = await claim_snapshot_for_order(
        settings,
        "snap-test",
        "order-bad",
        platform="vk",
        target_url="https://youtu.be/dQw4w9WgXcQ",
        metric="likes",
    )
    assert bad is False

    first = await claim_snapshot_for_order(
        settings,
        "snap-test",
        "order-good",
        platform="youtube",
        target_url="https://youtu.be/dQw4w9WgXcQ",
        metric="likes",
    )
    assert first is True

    second = await claim_snapshot_for_order(
        settings,
        "snap-test",
        "order-second",
        platform="youtube",
        target_url="https://youtu.be/dQw4w9WgXcQ",
        metric="likes",
    )
    assert second is False
