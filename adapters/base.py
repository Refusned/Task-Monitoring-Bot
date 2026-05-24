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
from enum import StrEnum

from models import ExternalSubmission, OrderSpec


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


class ExchangeAdapter(ABC):
    """Thin base: name, capability declaration, balance access."""

    name: str

    @abstractmethod
    def capabilities(self) -> set[Capability]: ...

    @abstractmethod
    async def get_balance(self) -> float: ...


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
