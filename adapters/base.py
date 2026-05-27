"""Adapter ABCs.

Two adapter archetypes model the two exchange types:

- `PanelAdapter` — SMM-panel (smmcode, prskill). Order is prepaid at creation; no
  per-submission accept/reject cycle.
- `TaskExchangeAdapter` — microtask exchange (unu, advego, ipgold). Per-submission
  review with accept/reject.

`ExchangeAdapter` is intentionally thin — only `get_balance` and `capabilities`.
Payable methods (`create_order`, `accept_submission`, ...) live on the appropriate
subclass; this prevents consumers from calling them blindly via the base type.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import StrEnum

from models import ExternalSubmission, OrderSpec, Scenario


class Capability(StrEnum):
    """Per-operation capability flags. The orchestrator branches on these
    rather than `isinstance` checks deep in business logic. See CLAUDE.md -> A8.
    """

    CREATE_ORDER = "create_order"
    GET_ORDER_STATUS = "get_order_status"
    GET_BALANCE = "get_balance"
    LIST_SUBMISSIONS = "list_submissions"
    ACCEPT_SUBMISSION = "accept_submission"
    REJECT_SUBMISSION = "reject_submission"
    SUPPORTS_CLIENT_ORDER_ID = "supports_client_order_id"


@dataclass(frozen=True, slots=True)
class ServiceOption:
    """A single picker option in the bot's «choose service» step.

    Adapters convert their native catalogue entries into `ServiceOption`s so the
    bot can render them as inline buttons without knowing the per-exchange JSON
    shape. `price_per_unit` is in RUB where applicable; `None` means the price
    can't be known up front (advego order_types) and the cost cap will kick in
    against a conservative server-side floor at placement time.
    """

    service_id: str
    name: str
    price_per_unit: float | None = None
    min_quantity: int | None = None
    max_quantity: int | None = None

    @property
    def button_label(self) -> str:
        """Short label fit for an inline-keyboard button (Telegram caps ~64 chars)."""
        bits: list[str] = [self.name[:38]]
        if self.price_per_unit is not None and self.price_per_unit > 0:
            bits.append(f"₽{self.price_per_unit:g}/шт")
        if self.min_quantity is not None and self.min_quantity > 1:
            bits.append(f"≥{self.min_quantity}")
        return " · ".join(bits)


# Scenario → list of lower-case substrings to match against service names.
# Each adapter's catalogue uses its own naming conventions; these keywords
# cover the common Russian + English wording on smmcode/unu/advego.
_SCENARIO_KEYWORDS: dict[Scenario, tuple[str, ...]] = {
    Scenario.ACTIVITY_SUBSCRIBE: (
        "подпис",  # Подписчики / Подписка
        "follow",
        "subscrib",
    ),
    Scenario.ACTIVITY_LIKE: (
        "лайк",
        "like",
        "оценк",
        "сердечк",
        "нравит",
    ),
    Scenario.ACTIVITY_VIEW: (
        "просмотр",
        "view",
        "watch",
        "показ",
    ),
    Scenario.SOCIAL_TRAFFIC: (
        "переход",
        "трафик",
        "traffic",
        "посет",
        "visit",
        "click",
        "клик",
    ),
}


def keywords_for_scenario(scenario: Scenario) -> tuple[str, ...]:
    """Return the substring filters used by adapters to narrow the catalogue."""
    return _SCENARIO_KEYWORDS.get(scenario, ())


class ExchangeAdapter(ABC):
    """Thin base: name, capability declaration, balance access."""

    name: str

    @abstractmethod
    def capabilities(self) -> set[Capability]: ...

    @abstractmethod
    async def get_balance(self) -> float: ...

    async def list_services_for_scenario(
        self,
        scenario: Scenario,
        limit: int = 8,
    ) -> list[ServiceOption]:
        """Return up to `limit` services from this exchange's catalogue that
        plausibly satisfy `scenario`.

        Adapters override this with a real catalogue fetch + keyword filter.
        The base implementation returns an empty list — this surfaces honestly
        to the bot's UI as "manual entry only" rather than crashing.
        """
        return []


class PanelAdapter(ExchangeAdapter):
    """SMM-panel adapter. Order is prepaid at creation; no accept/rework."""

    @abstractmethod
    async def create_order(self, spec: OrderSpec, client_order_uuid: str) -> tuple[str, float]:
        """Place an order. Returns (external_order_id, actual_cost)."""

    @abstractmethod
    async def get_order_status(self, external_order_id: str) -> str:
        """Normalized: 'in_progress' | 'completed' | 'failed'."""


class TaskExchangeAdapter(ExchangeAdapter):
    """Microtask-exchange adapter. Per-submission accept/reject lifecycle."""

    @abstractmethod
    async def create_order(self, spec: OrderSpec, client_order_uuid: str) -> tuple[str, float]:
        """Create a campaign/order. Returns (external_order_id, actual_cost)."""

    @abstractmethod
    async def get_order_status(self, external_order_id: str) -> str:
        """Normalized: 'in_progress' | 'completed' | 'failed'."""

    @abstractmethod
    async def list_submissions(self, external_order_id: str) -> list[ExternalSubmission]:
        """List submissions still pending a decision.

        Returns adapter-level DTOs (no internal UUID): the orchestrator owns the
        mapping from `external_submission_id` to the persisted `Submission` row.
        """

    @abstractmethod
    async def accept_submission(self, external_order_id: str, external_submission_id: str) -> None:
        """A6: accept the submission (pays the executor on the exchange)."""

    @abstractmethod
    async def reject_submission(
        self, external_order_id: str, external_submission_id: str, reason: str
    ) -> None:
        """A6: return for rework."""
