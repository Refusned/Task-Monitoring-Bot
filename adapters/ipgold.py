"""Stub adapter for the ipgold.ru microtask exchange.

Status: API documented as existing but specific method names are NOT publicly
confirmed at the time of writing. The plan calls for capability-gated honesty:
the adapter declares minimal capabilities, and `create_order` / submission
methods raise `NotImplementedError` with a clear message so the orchestrator's
capability-driven branching short-circuits to "exchange unsupported" rather
than producing silent failures.

Action items before promoting this adapter to live status (Day 4 leftover):
- Confirm method names (likely Perfect Panel form: `add`, `status`, `services`,
  `balance` over `https://ipgold.ru/api/v2` or similar).
- Confirm whether ipgold supports the submission accept/reject cycle. The plan
  notes "ipgold (Да, ожидается)" — to be verified from the live cabinet.
- Provide IPGOLD_API_KEY in `.env`.
"""

from __future__ import annotations

import httpx

from adapters.base import Capability, TaskExchangeAdapter, quote_from_mock
from models import ExternalSubmission, OrderSpec, Quote, SourcePlatform, TaskType, TopupInfo


class IpgoldAdapter(TaskExchangeAdapter):
    """ipgold.ru capability-gated stub.

    `capabilities()` returns an empty set so the orchestrator will refuse to
    place new orders on this exchange (the per-operation guard surfaces a clear
    "exchange unsupported" path). Methods raise `NotImplementedError` if called
    directly, with a message pointing at the credential / spec gap.
    """

    name = "ipgold"

    def __init__(self, api_key: str, http_client: httpx.AsyncClient) -> None:
        # We accept empty api_key to allow the adapter to be instantiated in the
        # registry without provisioning — the real guards live in `capabilities`
        # and the method stubs.
        self._api_key = api_key
        self._client = http_client

    def capabilities(self) -> set[Capability]:
        # CREATE_ORDER intentionally NOT here: real placement still unconfirmed.
        # GET_BALANCE also NOT here: ipgold /api/v1 needs action param that
        # docs don't expose publicly. Operator checks balance in their cabinet.
        return {
            Capability.GET_QUOTE,
            Capability.GET_TOPUP_INFO,
        }

    async def get_balance(self) -> float | None:
        # ipgold /api/v1 responds with "Action is not specified" — the action
        # parameter name isn't documented publicly and probing didn't reveal
        # it. Operators check the real balance at https://ipgold.ru/cabinet/
        # directly. Returning None signals "no API" to the dashboard.
        return None

    async def get_quote(
        self, metric: TaskType, platform: SourcePlatform, quantity: int
    ) -> Quote | None:
        from config import get_settings

        return quote_from_mock(
            self.name, metric, platform, quantity, get_settings().mock_quotes_csv,
            confidence=0.3,
        )

    async def get_topup_info(self) -> TopupInfo:
        return TopupInfo(
            exchange=self.name,
            topup_url="https://ipgold.ru/cabinet/finances/",
            min_amount=100.0,
            currency="RUB",
            payment_methods=["card", "yoomoney", "qiwi"],
            notes="Стартовый стаб; API ipgold не подтверждён, размещение заказа невозможно.",
        )

    async def create_order(self, spec: OrderSpec, client_order_uuid: str) -> tuple[str, float]:
        raise NotImplementedError(
            "ipgold create_order unconfirmed; orchestrator must consult "
            "capabilities() and refuse to place new orders on this exchange."
        )

    async def get_order_status(self, external_order_id: str) -> str:
        raise NotImplementedError("ipgold get_order_status unconfirmed")

    async def list_submissions(self, external_order_id: str) -> list[ExternalSubmission]:
        raise NotImplementedError("ipgold list_submissions unconfirmed")

    async def accept_submission(self, external_order_id: str, external_submission_id: str) -> None:
        raise NotImplementedError("ipgold accept_submission unconfirmed")

    async def reject_submission(
        self, external_order_id: str, external_submission_id: str, reason: str
    ) -> None:
        raise NotImplementedError("ipgold reject_submission unconfirmed")
