"""Tests for the new `service_id` FSM step (Day 8 polish).

Covers:
- ServiceOption.button_label formatting
- keyboards_for_scenario keyword sets
- per-adapter list_services_for_scenario filter logic against fixture catalogues
- FSM: catalogue-driven button click sets service_id and moves to target
- FSM: manual-entry fallback sets service_id and moves to target
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

from adapters.advego import AdvegoAdapter
from adapters.base import ServiceOption, keywords_for_scenario
from adapters.prskill import PrskillAdapter
from adapters.smmcode import SmmcodeAdapter
from adapters.unu import UnuAdapter
from bot import handlers, keyboards
from config import Settings
from db.database import init_db
from models import Scenario

# ---------------------------------------------------------------------------
# ServiceOption + keyword catalogue
# ---------------------------------------------------------------------------


def test_service_option_button_label_subscribers() -> None:
    opt = ServiceOption(
        service_id="136",
        name="Подписчики со всего мира",
        price_per_unit=0.18,
        min_quantity=20,
    )
    label = opt.button_label
    assert "Подписчики" in label
    assert "₽0.18/шт" in label
    assert "≥20" in label
    assert len(label) <= 64, "label must fit in a Telegram inline button"


def test_service_option_label_no_price() -> None:
    opt = ServiceOption(service_id="18", name="Лайки в соцсетях")
    assert "Лайки" in opt.button_label
    assert "₽" not in opt.button_label  # no price → no fake number


def test_keywords_cover_all_scenarios() -> None:
    for sc in (
        Scenario.ACTIVITY_SUBSCRIBE,
        Scenario.ACTIVITY_LIKE,
        Scenario.SOCIAL_TRAFFIC,
    ):
        assert keywords_for_scenario(sc), f"no keywords defined for {sc.value}"


# ---------------------------------------------------------------------------
# Per-adapter catalogue filter — drives a MockTransport so no live calls
# ---------------------------------------------------------------------------


def _mock_client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


_SMMCODE_FIXTURE = {
    "status": 200,
    "services": {
        "instagram": {
            "subscribers": {
                "136": {
                    "service_id": 136,
                    "price": 0.18,
                    "min": 20,
                    "max": 1000,
                    "name": "Подписчики со всего мира",
                },
                "137": {
                    "service_id": 137,
                    "price": 0.05,
                    "min": 50,
                    "max": 5000,
                    "name": "Подписчики РФ",
                },
                "999": {
                    "service_id": 999,
                    "price": 0.0,
                    "min": 10,
                    "max": 100,
                    "name": "Подписчики бесплатно",
                },
            },
            "likes": {
                "200": {
                    "service_id": 200,
                    "price": 0.10,
                    "min": 10,
                    "max": 500,
                    "name": "Лайки от живых",
                },
            },
            "traffic": {
                "300": {
                    "service_id": 300,
                    "price": 1.20,
                    "min": 100,
                    "max": 10000,
                    "name": "Переходы из ВК",
                },
            },
        }
    },
}


@pytest.mark.asyncio
async def test_smmcode_list_services_filters_by_scenario() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_SMMCODE_FIXTURE)

    async with _mock_client(handler) as http:
        adapter = SmmcodeAdapter("token", http)
        subs = await adapter.list_services_for_scenario(Scenario.ACTIVITY_SUBSCRIBE)
        likes = await adapter.list_services_for_scenario(Scenario.ACTIVITY_LIKE)
        traffic = await adapter.list_services_for_scenario(Scenario.SOCIAL_TRAFFIC)

    ids_subs = {o.service_id for o in subs}
    # "Подписчики бесплатно" has price=0 → skipped (cost-cap-safety)
    assert ids_subs == {"136", "137"}
    # Cheapest sub first
    assert subs[0].service_id == "137"
    assert subs[0].price_per_unit == pytest.approx(0.05)

    assert {o.service_id for o in likes} == {"200"}
    assert {o.service_id for o in traffic} == {"300"}


@pytest.mark.asyncio
async def test_unu_list_services_uses_min_price_rub() -> None:
    fixture = {
        "success": 1,
        "tariffs": [
            {"id": "1", "name": "Лайки", "min_price_rub": "1"},
            {"id": "65", "name": "Марафоны/розыгрыши/гивы в соц. сетях", "min_price_rub": "20"},
            {"id": "48", "name": "Действия в ботах/приложениях соц. сетей", "min_price_rub": "6"},
            {"id": "83", "name": "Просмотры", "min_price_rub": "0.55"},
            # No usable price → skipped:
            {"id": "999", "name": "Лайки бесплатные", "min_price_rub": "0"},
        ],
    }

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=fixture)

    async with _mock_client(handler) as http:
        adapter = UnuAdapter("token", http)
        likes = await adapter.list_services_for_scenario(Scenario.ACTIVITY_LIKE)

    assert [o.service_id for o in likes] == ["1"]  # only "Лайки" with real price
    assert likes[0].price_per_unit == pytest.approx(1.0)


@pytest.mark.asyncio
async def test_advego_list_services_filter_likes() -> None:
    """advego.getOrderTypes returns names without per-unit cost. We still filter
    by scenario keywords; price stays None and the cost cap uses the adapter's
    server-floor at placement time.
    """
    # Build an XML-RPC response for getOrderTypes.
    xml = """<?xml version="1.0"?>
<methodResponse><params><param><value><array><data>
  <value><struct>
    <member><name>id</name><value><int>3</int></value></member>
    <member><name>name</name><value><string>Статья</string></value></member>
  </struct></value>
  <value><struct>
    <member><name>id</name><value><int>18</int></value></member>
    <member><name>name</name><value><string>Лайки в соцсетях</string></value></member>
  </struct></value>
  <value><struct>
    <member><name>id</name><value><int>6</int></value></member>
    <member><name>name</name><value><string>Поиск информации</string></value></member>
  </struct></value>
</data></array></value></param></params></methodResponse>"""

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=xml.encode("utf-8"))

    async with _mock_client(handler) as http:
        adapter = AdvegoAdapter("token", http)
        likes = await adapter.list_services_for_scenario(Scenario.ACTIVITY_LIKE)

    assert len(likes) == 1
    assert likes[0].service_id == "18"
    assert likes[0].name == "Лайки в соцсетях"
    assert likes[0].price_per_unit is None  # Advego doesn't expose unit price


@pytest.mark.asyncio
async def test_prskill_list_services_filters_by_scenario() -> None:
    fixture = [
        {"service": "10", "name": "Подписчики ВК", "rate": "50.0", "min": 100, "max": 10000},
        {"service": "11", "name": "Лайки ВК", "rate": "10.0", "min": 10, "max": 1000},
        {"service": "12", "name": "Подписчики ИГ", "rate": "30.0", "min": 50, "max": 5000},
    ]

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=fixture)

    async with _mock_client(handler) as http:
        adapter = PrskillAdapter("token", http)
        subs = await adapter.list_services_for_scenario(Scenario.ACTIVITY_SUBSCRIBE)

    assert {o.service_id for o in subs} == {"10", "12"}
    # Cheaper first (rate 30 < 50)
    assert subs[0].service_id == "12"


# ---------------------------------------------------------------------------
# FSM: catalogue-driven service pick + manual fallback
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


@pytest.fixture
async def settings(tmp_path: Path) -> Settings:
    s = Settings(
        db_path=tmp_path / "test.db",
        dry_run=True,
        telegram_admin_ids=[42],
        per_order_spend_limit=5.0,
        smmcode_api_key="test_token",  # so build_adapter doesn't crash
    )
    await init_db(s)
    return s


@pytest.fixture
def state() -> FSMContext:
    return FSMContext(
        storage=MemoryStorage(),
        key=StorageKey(bot_id=1, chat_id=42, user_id=42),
    )


@pytest.mark.asyncio
async def test_service_step_uses_catalogue_when_available(
    state: FSMContext, settings: Settings
) -> None:
    """Mock the adapter's list_services_for_scenario to return one option;
    the bot must render it as an inline button and accept the click.
    """
    fake_options = [ServiceOption(service_id="136", name="Подписчики РФ", price_per_unit=0.05)]

    async def _fake_list(*_a, **_kw):
        return fake_options

    # Patch SmmcodeAdapter so any instance returns our options.
    with patch.object(SmmcodeAdapter, "list_services_for_scenario", _fake_list):
        await handlers.reply_new_order(_FakeMessage(), state, settings=settings)
        await handlers.cb_scenario(
            _FakeQuery("no:scenario:activity_subscribe"), state, settings=settings
        )
        q = _FakeQuery("no:exchange:smmcode")
        await handlers.cb_exchange(q, state, settings=settings)

    # We must be in the `service` state and the edited message must contain
    # the option's button.
    assert await state.get_state() == handlers.NewOrderFSM.service.state
    _edited_text, edited_kb = q.message.edits[-1]
    cb_data = [btn.callback_data for row in edited_kb.inline_keyboard for btn in row]
    assert "no:service:136" in cb_data
    assert "no:service_manual" in cb_data

    # Tap the service button → state moves to target, service_id stored.
    q_pick = _FakeQuery("no:service:136")
    await handlers.cb_service(q_pick, state, settings=settings)
    assert await state.get_state() == handlers.NewOrderFSM.target.state
    data = await state.get_data()
    assert data["service_id"] == "136"


@pytest.mark.asyncio
async def test_service_step_falls_back_to_manual_on_fetch_error(
    state: FSMContext, settings: Settings
) -> None:
    """If list_services_for_scenario raises, the user gets the manual-entry
    keyboard and a friendly error note.
    """

    async def _boom(*_a, **_kw):
        raise RuntimeError("upstream 500")

    with patch.object(SmmcodeAdapter, "list_services_for_scenario", _boom):
        await handlers.reply_new_order(_FakeMessage(), state, settings=settings)
        await handlers.cb_scenario(
            _FakeQuery("no:scenario:activity_subscribe"), state, settings=settings
        )
        q = _FakeQuery("no:exchange:smmcode")
        await handlers.cb_exchange(q, state, settings=settings)

    edited_text, edited_kb = q.message.edits[-1]
    assert "Не удалось загрузить каталог" in edited_text
    assert "upstream 500" in edited_text
    cb_data = [btn.callback_data for row in edited_kb.inline_keyboard for btn in row]
    assert "no:service_manual" in cb_data


@pytest.mark.asyncio
async def test_service_manual_entry_path(state: FSMContext, settings: Settings) -> None:
    async def _empty(*_a, **_kw):
        return []

    with patch.object(SmmcodeAdapter, "list_services_for_scenario", _empty):
        await handlers.reply_new_order(_FakeMessage(), state, settings=settings)
        await handlers.cb_scenario(
            _FakeQuery("no:scenario:activity_subscribe"), state, settings=settings
        )
        await handlers.cb_exchange(_FakeQuery("no:exchange:smmcode"), state, settings=settings)

    # Empty catalogue → still in service state with manual fallback.
    assert await state.get_state() == handlers.NewOrderFSM.service.state

    # Tap "Ввести ID вручную"
    q_manual = _FakeQuery("no:service_manual")
    await handlers.cb_service_manual(q_manual, state, settings=settings)
    assert await state.get_state() == handlers.NewOrderFSM.service_manual.state

    # Type the service_id
    msg_sid = _FakeMessage("136")
    await handlers.fsm_service_manual(msg_sid, state, settings=settings)
    assert await state.get_state() == handlers.NewOrderFSM.target.state
    data = await state.get_data()
    assert data["service_id"] == "136"


@pytest.mark.asyncio
async def test_service_manual_rejects_empty(state: FSMContext, settings: Settings) -> None:
    async def _empty(*_a, **_kw):
        return []

    with patch.object(SmmcodeAdapter, "list_services_for_scenario", _empty):
        await handlers.reply_new_order(_FakeMessage(), state, settings=settings)
        await handlers.cb_scenario(
            _FakeQuery("no:scenario:activity_subscribe"), state, settings=settings
        )
        await handlers.cb_exchange(_FakeQuery("no:exchange:smmcode"), state, settings=settings)
        await handlers.cb_service_manual(_FakeQuery("no:service_manual"), state, settings=settings)

    msg = _FakeMessage("")
    await handlers.fsm_service_manual(msg, state, settings=settings)
    # Still in manual state, friendly error.
    assert await state.get_state() == handlers.NewOrderFSM.service_manual.state
    assert any("ID услуги" in a[0] for a in msg.answers)


@pytest.mark.asyncio
async def test_service_choice_keyboard_with_options() -> None:
    options = [
        ServiceOption(service_id="1", name="Один", price_per_unit=0.1),
        ServiceOption(service_id="2", name="Два", price_per_unit=0.2),
    ]
    kb = keyboards.service_choice(options)
    cb_data = [btn.callback_data for row in kb.inline_keyboard for btn in row]
    assert "no:service:1" in cb_data
    assert "no:service:2" in cb_data
    assert "no:service_manual" in cb_data
    assert "no:cancel" in cb_data


def test_service_choice_keyboard_empty_offers_manual_and_cancel() -> None:
    kb = keyboards.service_choice([])
    cb_data = [btn.callback_data for row in kb.inline_keyboard for btn in row]
    assert "no:service_manual" in cb_data
    assert "no:cancel" in cb_data
    # No real service buttons when catalogue is empty.
    assert not any(cd and cd.startswith("no:service:") for cd in cb_data)


# Suppress lint warning about unused AsyncMock import (kept for future tests).
_ = AsyncMock
