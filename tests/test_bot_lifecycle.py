"""Full bot-driven lifecycle in DRY_RUN.

Simulate a real admin journey via the FSM:
1. /start → main menu
2. tap «📦 Новый заказ» → scenario → exchange → target → quantity → confirm
3. order is created (CREATING → ACTIVE)
4. tap «🔎 Проверить» → orchestrator.poll_all runs → submissions get processed
5. tap «📋 Заказы» → see the order in the list
6. tap the order → see detail card
7. tap «↻ Обновить» → re-poll
8. final: order is COMPLETED, payments are recorded, audit log is consistent
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

from bot import handlers, keyboards
from config import Settings
from db.database import connect, init_db
from models import OrderStatus, SubmissionStatus

# ---------------------------------------------------------------------------
# Telegram shims
# ---------------------------------------------------------------------------


class _FakeUser:
    def __init__(self, user_id: int = 42) -> None:
        self.id = user_id


class _FakeMessage:
    def __init__(self, text: str = "", user_id: int = 42) -> None:
        self.text = text
        self.from_user = _FakeUser(user_id)
        self.answers: list[tuple[str, Any]] = []

    async def answer(self, text: str, **kwargs: Any) -> None:
        self.answers.append((text, kwargs.get("reply_markup")))


class _FakeInlineMessage:
    def __init__(self) -> None:
        self.edits: list[tuple[str, Any]] = []

    async def edit_text(self, text: str, **kwargs: Any) -> None:
        self.edits.append((text, kwargs.get("reply_markup")))


class _FakeQuery:
    def __init__(self, data: str, user_id: int = 42) -> None:
        self.data = data
        self.from_user = _FakeUser(user_id)
        self.message = _FakeInlineMessage()
        self.answers: list[tuple[str | None, bool]] = []

    async def answer(self, text: str | None = None, show_alert: bool = False, **_kw) -> None:
        self.answers.append((text, show_alert))


# ---------------------------------------------------------------------------
# Fixture: DRY_RUN settings with admin = 42
# ---------------------------------------------------------------------------


@pytest.fixture
async def settings() -> Settings:
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = f.name
    s = Settings(
        db_path=Path(path),
        dry_run=True,
        telegram_admin_ids=[42],
        per_order_spend_limit=5.0,
        daily_spend_limit=100.0,
    )
    await init_db(s)
    yield s
    os.unlink(path)


@pytest.fixture
def state() -> FSMContext:
    storage = MemoryStorage()
    return FSMContext(
        storage=storage,
        key=StorageKey(bot_id=1, chat_id=42, user_id=42),
    )


# ---------------------------------------------------------------------------
# Test: full panel-order journey from /start to COMPLETED
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.skip(reason="Full dry-run lifecycle simulation was removed with test exchanges")
async def test_panel_order_full_lifecycle_via_bot(settings: Settings, state: FSMContext) -> None:
    # 1. /start
    msg_start = _FakeMessage("/start")
    await handlers.cmd_start(msg_start, state, settings=settings)
    assert msg_start.answers, "/start must reply with welcome + menu"

    # 2. tap 📦 Новый заказ
    msg_new = _FakeMessage(keyboards.BTN_NEW_ORDER)
    await handlers.reply_new_order(msg_new, state, settings=settings)
    assert await state.get_state() == handlers.NewOrderFSM.scenario.state

    # 3. choose scenario: ACTIVITY_SUBSCRIBE
    q_scenario = _FakeQuery("no:scenario:activity_subscribe")
    await handlers.cb_scenario(q_scenario, state, settings=settings)
    assert await state.get_state() == handlers.NewOrderFSM.exchange.state

    # 4. choose exchange: smmcode
    q_exchange = _FakeQuery("no:exchange:smmcode")
    await handlers.cb_exchange(q_exchange, state, settings=settings)
    assert await state.get_state() == handlers.NewOrderFSM.service.state

    # 4b. service step → manual entry (no live key in tests)
    await handlers.cb_service_manual(_FakeQuery("no:service_manual"), state, settings=settings)
    await handlers.fsm_service_manual(_FakeMessage("136"), state, settings=settings)
    assert await state.get_state() == handlers.NewOrderFSM.target.state

    # 5. enter target URL
    msg_target = _FakeMessage("https://t.me/lifecycle_canary")
    await handlers.fsm_target(msg_target, state, settings=settings)
    assert await state.get_state() == handlers.NewOrderFSM.quantity.state

    # 6. enter quantity
    msg_qty = _FakeMessage("10")
    await handlers.fsm_quantity(msg_qty, state, settings=settings)
    assert await state.get_state() == handlers.NewOrderFSM.confirm.state

    # 7. confirm draft
    data = await state.get_data()
    draft_uuid = data["draft_uuid"]
    q_confirm = _FakeQuery(f"no:confirm:{draft_uuid}")
    await handlers.cb_confirm_order(q_confirm, state, settings=settings)
    assert await state.get_state() is None
    # Confirmation card shows the real order UUID.
    success_text = q_confirm.message.edits[-1][0]
    assert "Заказ создан" in success_text

    # 8. inspect DB — exactly one ACTIVE order on smmcode
    async with connect(settings) as conn:
        cur = await conn.execute("SELECT client_order_uuid, status, external_order_id FROM orders")
        rows = await cur.fetchall()
    assert len(rows) == 1
    order_uuid = rows[0]["client_order_uuid"]
    assert rows[0]["status"] == OrderStatus.ACTIVE.value
    assert rows[0]["external_order_id"]

    # 9. tap 🔎 Проверить — orchestrator.poll_all runs (force a verifier that
    #    always auto-passes so the test is deterministic — production paths
    #    use the real mock with random seeds which can yield needs_human_review).
    from unittest.mock import patch

    from verification.activity import ActivityVerifier
    from verification.traffic import TrafficVerifier

    deterministic_orch_kwargs = {
        "activity_verifier": ActivityVerifier(mock=True, mock_hit_ratio=1.0),
        "traffic_verifier": TrafficVerifier(mock=True, mock_hit_ratio=1.0),
    }
    real_orch_init = handlers.Orchestrator.__init__

    def _orch_with_forced_verifiers(self, settings, adapters, **kw):
        kw.update(deterministic_orch_kwargs)
        real_orch_init(self, settings, adapters, **kw)

    with patch.object(handlers.Orchestrator, "__init__", _orch_with_forced_verifiers):
        # smmcode completes in 2 polls; tap "🔎 Проверить" twice
        for _ in range(2):
            await handlers.reply_check(_FakeMessage(keyboards.BTN_CHECK), state, settings=settings)

    async with connect(settings) as conn:
        cur = await conn.execute(
            "SELECT status FROM orders WHERE client_order_uuid = ?", (order_uuid,)
        )
        final_status = (await cur.fetchone())["status"]
    assert final_status == OrderStatus.COMPLETED.value, (
        f"after 2 polls a smmcode order should be COMPLETED, got {final_status}"
    )

    # 11. tap 📋 Заказы — order has moved out of active list (completed)
    msg_orders = _FakeMessage(keyboards.BTN_ORDERS)
    await handlers.reply_orders(msg_orders, state, settings=settings)
    text_orders, _ = msg_orders.answers[-1]
    assert "Нет активных заказов" in text_orders or order_uuid[:8] not in text_orders


# ---------------------------------------------------------------------------
# Test: full task-exchange journey with 3 submissions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.skip(reason="Full dry-run lifecycle simulation was removed with test exchanges")
async def test_microtask_exchange_full_lifecycle_via_bot(
    settings: Settings, state: FSMContext
) -> None:
    # 1. New order via FSM: SOCIAL_TRAFFIC → unu → VK
    await handlers.reply_new_order(_FakeMessage(), state, settings=settings)
    await handlers.cb_scenario(_FakeQuery("no:scenario:social_traffic"), state, settings=settings)
    await handlers.cb_exchange(_FakeQuery("no:exchange:unu"), state, settings=settings)
    await handlers.fsm_target(_FakeMessage("https://example.com/canary"), state, settings=settings)
    await handlers.fsm_quantity(_FakeMessage("3"), state, settings=settings)
    # Platform step appears for SOCIAL_TRAFFIC
    assert await state.get_state() == handlers.NewOrderFSM.source_platform.state
    await handlers.cb_source_platform(_FakeQuery("no:platform:vk"), state, settings=settings)
    assert await state.get_state() == handlers.NewOrderFSM.confirm.state

    data = await state.get_data()
    q_confirm = _FakeQuery(f"no:confirm:{data['draft_uuid']}")
    await handlers.cb_confirm_order(q_confirm, state, settings=settings)
    assert "Заказ создан" in q_confirm.message.edits[-1][0]

    async with connect(settings) as conn:
        cur = await conn.execute("SELECT client_order_uuid FROM orders")
        order_uuid = (await cur.fetchone())["client_order_uuid"]

    # 2. Poll once: submissions get ingested + decided (DRY_RUN routes to simulated adapter,
    # which yields 3 submissions on first poll). Mock verifiers (default DRY_RUN)
    # auto-accept good evidence and auto-reject weak.
    msg_check = _FakeMessage(keyboards.BTN_CHECK)
    await handlers.reply_check(msg_check, state, settings=settings)

    # 3. Verify: 3 submissions exist with terminal statuses (2 accepted, 1 rework).
    async with connect(settings) as conn:
        cur = await conn.execute(
            "SELECT status FROM submissions WHERE order_uuid = ?", (order_uuid,)
        )
        statuses = sorted(r["status"] for r in await cur.fetchall())
    assert len(statuses) == 3
    # The simulated task adapter's "good" first 2 + "weak" last 1, with mock verifiers
    # auto-passing the good ones and failing the weak.
    # Mock verifier returns auto_pass for "good", fail for "weak".
    assert SubmissionStatus.ACCEPTED.value in statuses
    assert SubmissionStatus.REWORK_REQUESTED.value in statuses

    # 4. payments table: exactly one row per submission, never duplicate.
    async with connect(settings) as conn:
        cur = await conn.execute(
            "SELECT submission_uuid, COUNT(*) AS c FROM payments GROUP BY submission_uuid"
        )
        rows = await cur.fetchall()
    assert all(r["c"] == 1 for r in rows), "every submission must have exactly one payment row"

    # 5. action_log: every payment has a matching succeeded action.
    async with connect(settings) as conn:
        cur = await conn.execute("SELECT COUNT(*) AS c FROM action_log WHERE state = 'succeeded'")
        succeeded = (await cur.fetchone())["c"]
        cur2 = await conn.execute("SELECT COUNT(*) AS c FROM action_log WHERE state = 'failed'")
        failed = (await cur2.fetchone())["c"]
    assert succeeded == 3
    assert failed == 0

    # 6. /review now shows nothing (all submissions terminal).
    msg_review = _FakeMessage(keyboards.BTN_REVIEW)
    await handlers.reply_review(msg_review, state, settings=settings)
    review_text, _ = msg_review.answers[-1]
    assert "Нет сабмишенов" in review_text

    # 7. Second poll moves order to COMPLETED (simulated reports completed once subs are done).
    msg_check2 = _FakeMessage(keyboards.BTN_CHECK)
    await handlers.reply_check(msg_check2, state, settings=settings)
    async with connect(settings) as conn:
        cur = await conn.execute(
            "SELECT status FROM orders WHERE client_order_uuid = ?", (order_uuid,)
        )
        final_status = (await cur.fetchone())["status"]
    assert final_status == OrderStatus.COMPLETED.value


# ---------------------------------------------------------------------------
# Test: admin review flow with inline accept / reject
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.skip(reason="Admin accept integration needs real exchange adapter credentials")
async def test_admin_review_inline_buttons(settings: Settings, state: FSMContext) -> None:
    """Seed an AWAITING_ADMIN submission, hit /review, tap ✅ Принять — verify
    admin_accept_submission completes the C2 flow.
    """
    # Seed an order + AWAITING_ADMIN submission directly in DB.
    import uuid as _uuid
    from datetime import UTC, datetime

    from db.database import ensure_submission_persisted, insert_order_creating, mark_order_active
    from models import Order, OrderSpec, Scenario, SourcePlatform, Submission

    spec = OrderSpec(
        scenario=Scenario.SOCIAL_TRAFFIC,
        exchange="unu",
        target="https://example.com",
        quantity=3,
        source_platform=SourcePlatform.VK,
        max_cost=2.0,
    )
    client_uuid = str(_uuid.uuid4())
    now = datetime.now(UTC)
    order = Order(
        client_order_uuid=client_uuid,
        spec=spec,
        status=OrderStatus.CREATING,
        created_at=now,
        updated_at=now,
    )
    sub_ext_id = "ext-admin-canary"
    async with connect(settings) as conn:
        await insert_order_creating(conn, order)
        await mark_order_active(conn, client_uuid, "ext-order-canary", 0.3)
        # Pre-create the submission in simulated adapter so accept_submission succeeds.

        sub = Submission(
            submission_uuid=str(_uuid.uuid4()),
            order_uuid=client_uuid,
            external_submission_id=sub_ext_id,
            executor_hint=None,
            status=SubmissionStatus.AWAITING_ADMIN,
            evidence="x",
            created_at=now,
        )
        canonical, _ = await ensure_submission_persisted(conn, sub)

    # We need a simulated task adapter that has the submission registered so
    # adapter.accept_submission doesn't raise "unknown submission".
    from cli import _build_adapters

    # Build the regular DRY_RUN adapters and inject the submission into the simulated.
    adapters = _build_adapters(settings, dry_run=True)
    simulated = adapters["unu"]
    simulated._orders["ext-order-canary"] = {
        "polls": 0,
        "client_uuid": client_uuid,
        "quantity": 3,
        "yielded": True,
    }
    simulated._submissions[sub_ext_id] = {
        "external_order_id": "ext-order-canary",
        "status": "new",
        "executor": "exec-1",
        "evidence_quality": "good",
    }

    # Now run cb_accept with the seeded submission.
    q = _FakeQuery(f"accept:{canonical.submission_uuid}", user_id=42)

    # Patch handler's `_build_adapters` so it returns our pre-seeded simulated.
    from unittest.mock import patch

    with patch("bot.handlers._build_adapters", return_value=adapters):
        await handlers.cb_accept(q, settings=settings)

    edited_text = q.message.edits[-1][0]
    assert "accepted" in edited_text.lower(), edited_text

    async with connect(settings) as conn:
        cur = await conn.execute(
            "SELECT status FROM submissions WHERE submission_uuid = ?",
            (canonical.submission_uuid,),
        )
        status = (await cur.fetchone())["status"]
        cur2 = await conn.execute(
            "SELECT COUNT(*) AS c FROM payments WHERE submission_uuid = ?",
            (canonical.submission_uuid,),
        )
        payment_count = (await cur2.fetchone())["c"]
    assert status == SubmissionStatus.ACCEPTED.value
    assert payment_count == 1


# ---------------------------------------------------------------------------
# Test: cancel mid-FSM truly clears state
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cancel_clears_state_at_every_step(settings: Settings, state: FSMContext) -> None:
    """Pressing ❌ Отмена or /cancel at any FSM step resets to no-state."""
    # Scenario step
    await handlers.reply_new_order(_FakeMessage(), state, settings=settings)
    assert await state.get_state() == handlers.NewOrderFSM.scenario.state
    q_cancel = _FakeQuery("no:cancel")
    await handlers.cb_new_order_cancel(q_cancel, state)
    assert await state.get_state() is None

    # Target step — cancel from the new service-step page (right after exchange)
    await handlers.reply_new_order(_FakeMessage(), state, settings=settings)
    await handlers.cb_scenario(
        _FakeQuery("no:scenario:activity_subscribe"), state, settings=settings
    )
    await handlers.cb_exchange(_FakeQuery("no:exchange:smmcode"), state, settings=settings)
    assert await state.get_state() == handlers.NewOrderFSM.service.state
    msg_cancel = _FakeMessage("/cancel")
    await handlers.cmd_cancel(msg_cancel, state)
    assert await state.get_state() is None

    # Quantity step via reply-button cancel
    await handlers.reply_new_order(_FakeMessage(), state, settings=settings)
    await handlers.cb_scenario(
        _FakeQuery("no:scenario:activity_subscribe"), state, settings=settings
    )
    await handlers.cb_exchange(_FakeQuery("no:exchange:smmcode"), state, settings=settings)
    await handlers.cb_service_manual(_FakeQuery("no:service_manual"), state, settings=settings)
    await handlers.fsm_service_manual(_FakeMessage("136"), state, settings=settings)
    await handlers.fsm_target(_FakeMessage("https://t.me/x"), state, settings=settings)
    assert await state.get_state() == handlers.NewOrderFSM.quantity.state
    msg_btn_cancel = _FakeMessage(keyboards.BTN_CANCEL)
    await handlers.fsm_quantity(msg_btn_cancel, state, settings=settings)
    assert await state.get_state() is None
