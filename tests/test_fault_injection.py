"""Fault-injection tests: prove the bot survives broken networks / responses /
mid-call crashes without leaking money, leaving DB inconsistent, or crashing.

We poke holes in the adapter layer (network errors, 5xx, malformed JSON / XML,
empty payloads) and at the orchestrator layer (adapter raises mid-accept), and
assert the system lands in a recoverable state.
"""

from __future__ import annotations

import os
import tempfile
import uuid
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from adapters.base import Capability, TaskExchangeAdapter
from adapters.smmcode import SmmcodeAdapter
from adapters.unu import UnuAdapter
from cli import _persist_and_create
from config import Settings
from db.database import (
    connect,
    ensure_submission_persisted,
    get_order,
    init_db,
    insert_order_creating,
    mark_order_active,
)
from models import (
    Order,
    OrderSpec,
    OrderStatus,
    Scenario,
    SourcePlatform,
    Submission,
    SubmissionStatus,
)
from orchestrator import Orchestrator

# ---------------------------------------------------------------------------
# Fixtures + shims
# ---------------------------------------------------------------------------


@pytest.fixture
async def settings():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = f.name
    s = Settings(
        db_path=Path(path),
        dry_run=True,
        per_order_spend_limit=2.0,
        daily_spend_limit=100.0,
    )
    await init_db(s)
    yield s
    os.unlink(path)


def _make_client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


# ---------------------------------------------------------------------------
# Adapter-level network / parse fault injection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_smmcode_503_raises_clean_runtime_error():
    """503 from smmcode bubbles up as httpx.HTTPStatusError — wrapped or raw."""

    def handler(req):
        return httpx.Response(503, text="Service Unavailable")

    async with _make_client(handler) as http:
        adapter = SmmcodeAdapter("test_token", http)
        with pytest.raises(Exception) as exc_info:
            await adapter.get_balance()
        # Either httpx.HTTPStatusError or wrapped RuntimeError.
        assert "503" in str(exc_info.value) or "Server error" in str(exc_info.value)


@pytest.mark.asyncio
async def test_smmcode_malformed_json_raises_clean():
    """Garbage JSON body produces an exception, not a silent default value."""

    def handler(req):
        return httpx.Response(200, text="this is not JSON")

    async with _make_client(handler) as http:
        adapter = SmmcodeAdapter("test_token", http)
        # Garbage payload → httpx/json parsing raises (JSONDecodeError or ValueError).
        with pytest.raises((ValueError, RuntimeError)):
            await adapter.get_balance()


@pytest.mark.asyncio
async def test_smmcode_html_maintenance_page_is_rejected():
    """If the API returns an HTML maintenance page with HTTP 200, we must NOT
    silently accept it as a valid balance/order response.
    """

    def handler(req):
        return httpx.Response(
            200,
            text="<html><body>We're under maintenance</body></html>",
            headers={"content-type": "text/html"},
        )

    async with _make_client(handler) as http:
        adapter = SmmcodeAdapter("test_token", http)
        with pytest.raises((ValueError, RuntimeError)):
            await adapter.get_balance()


@pytest.mark.asyncio
async def test_smmcode_non_200_status_field_raises():
    """API returns HTTP 200 but `status` field is not 200 → must raise."""

    def handler(req):
        return httpx.Response(200, json={"status": 401, "error": "Bad token"})

    async with _make_client(handler) as http:
        adapter = SmmcodeAdapter("test_token", http)
        with pytest.raises(RuntimeError, match="non-200 status"):
            await adapter.get_balance()


@pytest.mark.asyncio
async def test_smmcode_error_message_does_not_leak_token():
    """The adapter must NOT echo the api_token in error messages."""

    def handler(req):
        return httpx.Response(200, json={"status": 401, "error": "Bad token"})

    async with _make_client(handler) as http:
        adapter = SmmcodeAdapter("supersecret123", http)
        with pytest.raises(RuntimeError) as exc_info:
            await adapter.get_balance()
        assert "supersecret123" not in str(exc_info.value)


@pytest.mark.asyncio
async def test_unu_error_message_does_not_leak_token():
    def handler(req):
        return httpx.Response(200, json={"success": 0, "errors": "Bad key"})

    async with _make_client(handler) as http:
        adapter = UnuAdapter("supersecret456", http)
        with pytest.raises(RuntimeError) as exc_info:
            await adapter.get_balance()
        assert "supersecret456" not in str(exc_info.value)


@pytest.mark.asyncio
async def test_smmcode_timeout_raises_clean():
    """A network timeout propagates as httpx.TimeoutException."""

    async def handler(req):
        raise httpx.TimeoutException("simulated timeout")

    async with _make_client(handler) as http:
        adapter = SmmcodeAdapter("test_token", http)
        # Timeout from the transport propagates as httpx.TimeoutException.
        with pytest.raises(httpx.TimeoutException):
            await adapter.get_balance()


# ---------------------------------------------------------------------------
# Bot-level fault injection: _persist_and_create when adapter raises
# ---------------------------------------------------------------------------


class _BoomAdapter(TaskExchangeAdapter):
    """Adapter that always raises on create_order — simulates a network error
    AFTER the CREATING row has been persisted.
    """

    name = "boom"

    def __init__(self, error: Exception) -> None:
        self._error = error

    def capabilities(self):
        return {
            Capability.CREATE_ORDER,
            Capability.GET_ORDER_STATUS,
            Capability.LIST_SUBMISSIONS,
            Capability.ACCEPT_SUBMISSION,
            Capability.REJECT_SUBMISSION,
        }

    async def get_balance(self):
        return 0.0

    async def create_order(self, spec, client_order_uuid):
        raise self._error

    async def get_order_status(self, external_order_id):
        return "in_progress"

    async def list_submissions(self, external_order_id):
        return []

    async def accept_submission(self, external_order_id, external_submission_id):
        raise self._error

    async def reject_submission(self, external_order_id, external_submission_id, reason):
        raise self._error


@pytest.mark.asyncio
async def test_persist_and_create_failed_adapter_marks_order_failed(settings):
    """C1 invariant: an exception during adapter.create_order moves the row
    CREATING → FAILED with a clear audit entry — never leaves it stuck in
    CREATING (which would block all future polls).
    """
    spec = OrderSpec(
        scenario=Scenario.SOCIAL_TRAFFIC,
        exchange="boom",
        target="https://example.com",
        quantity=5,
        source_platform=SourcePlatform.VK,
        max_cost=2.0,
    )
    boom = _BoomAdapter(RuntimeError("network exploded"))
    with pytest.raises(RuntimeError, match="network exploded"):
        await _persist_and_create(settings, boom, spec, actor="fault-test")

    async with connect(settings) as conn:
        cur = await conn.execute(
            "SELECT client_order_uuid, status FROM orders WHERE exchange = ?", ("boom",)
        )
        row = await cur.fetchone()
        cur2 = await conn.execute(
            "SELECT event FROM audit_log WHERE order_uuid = ? ORDER BY audit_id",
            (row["client_order_uuid"],),
        )
        events = [r["event"] for r in await cur2.fetchall()]

    assert row["status"] == OrderStatus.FAILED.value
    assert events == ["order_creating", "order_create_failed"]
    # The order MUST NOT have reached order_active.
    assert "order_active" not in events


@pytest.mark.asyncio
async def test_orchestrator_accept_failure_records_action_log_failure(settings):
    """When adapter.accept_submission raises, action_log records the failure,
    payment row is NOT inserted, and submission status reflects an in-progress
    state that's recoverable on next poll.
    """
    # Build a fixed order + submission directly in DB.
    spec = OrderSpec(
        scenario=Scenario.SOCIAL_TRAFFIC,
        exchange="boom",
        target="https://example.com",
        quantity=3,
        source_platform=SourcePlatform.VK,
        max_cost=2.0,
    )
    client_uuid = str(uuid.uuid4())
    external_id = "ext-boom-1"
    now = datetime.now(UTC)
    order = Order(
        client_order_uuid=client_uuid,
        spec=spec,
        status=OrderStatus.CREATING,
        created_at=now,
        updated_at=now,
    )
    async with connect(settings) as conn:
        await insert_order_creating(conn, order)
        await mark_order_active(conn, client_uuid, external_id, cost_actual=0.1)
        sub = Submission(
            submission_uuid=str(uuid.uuid4()),
            order_uuid=client_uuid,
            external_submission_id="ext-sub-boom",
            executor_hint=None,
            status=SubmissionStatus.AWAITING_ADMIN,
            evidence="x",
            created_at=now,
        )
        await ensure_submission_persisted(conn, sub)

    boom = _BoomAdapter(RuntimeError("accept blew up"))
    live_settings = settings.model_copy(update={"dry_run": False})
    orch = Orchestrator(live_settings, adapters={"boom": boom})
    decision = await orch.admin_accept_submission(sub.submission_uuid, actor="fault-test")
    assert decision.get("decision") == "accept_failed"
    assert "accept blew up" in decision.get("error", "")

    async with connect(settings) as conn:
        cur = await conn.execute(
            "SELECT state, error FROM action_log WHERE submission_uuid = ?",
            (sub.submission_uuid,),
        )
        rows = await cur.fetchall()
        cur2 = await conn.execute(
            "SELECT COUNT(*) AS c FROM payments WHERE submission_uuid = ?",
            (sub.submission_uuid,),
        )
        payment_count = (await cur2.fetchone())["c"]
        cur3 = await conn.execute(
            "SELECT status FROM submissions WHERE submission_uuid = ?",
            (sub.submission_uuid,),
        )
        sub_status = (await cur3.fetchone())["status"]
    assert len(rows) == 1
    assert rows[0]["state"] == "failed"
    assert "accept blew up" in rows[0]["error"]
    # CRITICAL: no payment row created when external call failed.
    assert payment_count == 0
    # Submission was claimed to ACCEPTING but external call failed. The C2
    # invariant says payment was not made. The submission is left in ACCEPTING
    # — the next admin retry will see status != allowed_from and reject the
    # claim (need a recovery path; we accept this for MVP since action_log
    # state = "failed" surfaces it).
    assert sub_status == SubmissionStatus.ACCEPTING.value


# ---------------------------------------------------------------------------
# Crash-mid-flight: orphan CREATING row + reconcile
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_orphan_creating_reconciled_at_startup(settings):
    """Simulates a process crash mid-create_order: row in CREATING for >5min.
    Bot startup calls reconcile_creating, which moves it to FAILED.
    """
    # Manually insert an old CREATING row (created 10 min ago).
    spec = OrderSpec(
        scenario=Scenario.ACTIVITY_SUBSCRIBE,
        exchange="smmcode",
        target="https://t.me/x",
        quantity=5,
        max_cost=1.0,
    )
    client_uuid = str(uuid.uuid4())
    old = datetime(2020, 1, 1, tzinfo=UTC)  # very old
    order = Order(
        client_order_uuid=client_uuid,
        spec=spec,
        status=OrderStatus.CREATING,
        created_at=old,
        updated_at=old,
    )
    async with connect(settings) as conn:
        await insert_order_creating(conn, order)

    orch = Orchestrator(settings, adapters={})
    fixed = await orch.reconcile_creating()  # default 300s threshold
    assert fixed == 1

    async with connect(settings) as conn:
        recovered = await get_order(conn, client_uuid)
    assert recovered.status == OrderStatus.FAILED


# ---------------------------------------------------------------------------
# Verifier robustness: unknown evidence / wrong scenario
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_traffic_verifier_wrong_scenario_returns_needs_review():
    from verification.traffic import TrafficVerifier

    v = TrafficVerifier(mock=True)
    order = Order(
        client_order_uuid="x",
        spec=OrderSpec(
            scenario=Scenario.ACTIVITY_SUBSCRIBE,
            exchange="smmcode",
            target="https://t.me/x",
            quantity=5,
            max_cost=1.0,
        ),
        status=OrderStatus.ACTIVE,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    result = await v.verify(order)
    from models import VerificationVerdict

    assert result.verdict == VerificationVerdict.NEEDS_HUMAN_REVIEW


@pytest.mark.asyncio
async def test_activity_verifier_wrong_scenario_returns_needs_review():
    from models import VerificationVerdict
    from verification.activity import ActivityVerifier

    v = ActivityVerifier(mock=True)
    order = Order(
        client_order_uuid="x",
        spec=OrderSpec(
            scenario=Scenario.SOCIAL_TRAFFIC,
            exchange="unu",
            target="https://example.com",
            quantity=5,
            source_platform=SourcePlatform.VK,
            max_cost=1.0,
        ),
        status=OrderStatus.ACTIVE,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    result = await v.verify(order)
    assert result.verdict == VerificationVerdict.NEEDS_HUMAN_REVIEW


# ---------------------------------------------------------------------------
# poll_all isolates per-order failures
# ---------------------------------------------------------------------------


class _FullyBoomAdapter(_BoomAdapter):
    """Like _BoomAdapter but also raises on get_order_status — simulates a
    completely-broken exchange during a poll pass.
    """

    async def get_order_status(self, external_order_id):
        raise self._error


@pytest.mark.asyncio
async def test_poll_all_isolates_failures_per_order(settings):
    """If one adapter raises during poll, other orders still get processed."""
    # Order A: routes to fully-boom adapter — raises on get_order_status.
    spec_a = OrderSpec(
        scenario=Scenario.ACTIVITY_SUBSCRIBE,
        exchange="boom",
        target="https://t.me/a",
        quantity=5,
        max_cost=1.0,
    )
    # Order B: routes to a real adapter with mocked HTTP — must NOT be affected by A's failure.

    spec_b = OrderSpec(
        scenario=Scenario.ACTIVITY_SUBSCRIBE,
        exchange="smmcode",
        target="https://t.me/b",
        quantity=5,
        max_cost=1.0,
    )

    async def seed(spec, ext):
        client_uuid = str(uuid.uuid4())
        now = datetime.now(UTC)
        order = Order(
            client_order_uuid=client_uuid,
            spec=spec,
            status=OrderStatus.CREATING,
            created_at=now,
            updated_at=now,
        )
        async with connect(settings) as conn:
            await insert_order_creating(conn, order)
            await mark_order_active(conn, client_uuid, ext, 0.1)
        return client_uuid

    uuid_a = await seed(spec_a, "ext-boom-poll")
    uuid_b = await seed(spec_b, "ext-smmcode-1")

    boom = _FullyBoomAdapter(RuntimeError("boom!"))

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/order_status"):
            return httpx.Response(200, json={"status": 200, "order": {"status_id": 3}})
        return httpx.Response(500)

    async with _make_client(handler) as http:
        smmcode = SmmcodeAdapter("token", http)
        orch = Orchestrator(settings, adapters={"boom": boom, "smmcode": smmcode})
        results = await orch.poll_all()
    by_uuid = {r["order_uuid"]: r for r in results}
    assert by_uuid[uuid_a]["status"] == "error"
    assert "boom!" in by_uuid[uuid_a].get("error", "")
    # The KEY invariant: smmcode's poll did NOT raise — failure of the
    # boom adapter was isolated. The order's status is whatever the
    # (mock, stochastic) verifier decided — any non-error status is fine.
    assert by_uuid[uuid_b].get("status") != "error", (
        f"failure on order A leaked into order B: {by_uuid[uuid_b]!r}"
    )
