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

from models import ExternalSubmission, OrderSpec, Quote, SourcePlatform, TaskType, TopupInfo


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
    # v4 agent pivot
    GET_QUOTE = "get_quote"
    GET_TOPUP_INFO = "get_topup_info"


class ExchangeAdapter(ABC):
    """Thin base: name, capability declaration, balance access."""

    name: str

    @abstractmethod
    def capabilities(self) -> set[Capability]: ...

    @abstractmethod
    async def get_balance(self) -> float: ...

    # ----- v4 agent pivot: optional methods (non-abstract; default = unsupported)
    async def get_quote(
        self, metric: TaskType, platform: SourcePlatform, quantity: int
    ) -> Quote | None:
        """Return a price quote for this (metric, platform, qty) on this exchange.
        Default: not supported. Adapters override to declare support."""
        return None

    async def get_topup_info(self) -> TopupInfo | None:
        """Return how to fund this exchange (URL + min + methods).
        Default: not supported. Adapters override with hardcoded URLs."""
        return None


# ----- Shared helpers for adapter authors --------------------------------

def parse_mock_quotes(csv: str) -> dict[tuple[str, str, str], dict]:
    """Parse mock_quotes_csv (see config.Settings) into a (ex,pl,met) lookup."""
    out: dict[tuple[str, str, str], dict] = {}
    for entry in csv.split(","):
        entry = entry.strip()
        if not entry or "=" not in entry:
            continue
        key, val = entry.split("=", 1)
        parts = key.strip().split(":")
        if len(parts) != 3:
            continue
        exchange, platform, metric = parts
        price_eta = val.strip().split(";")
        if len(price_eta) != 3:
            continue
        try:
            price = float(price_eta[0])
            eta_min = int(price_eta[1])
            eta_max = int(price_eta[2])
        except ValueError:
            continue
        out[(exchange.lower(), platform.lower(), metric.lower())] = {
            "price_per_unit": price,
            "eta_min": eta_min,
            "eta_max": eta_max,
        }
    return out


def quote_from_mock(
    adapter_name: str,
    metric: TaskType,
    platform: SourcePlatform,
    quantity: int,
    mock_quotes_csv: str,
    service_id: str | None = None,
    confidence: float = 0.5,
) -> Quote | None:
    """Build a Quote from the config mock table. None if combo not mocked."""
    table = parse_mock_quotes(mock_quotes_csv)
    key = (adapter_name.lower(), platform.value, metric.value)
    entry = table.get(key)
    if entry is None:
        return None
    return Quote(
        exchange=adapter_name,
        service_id=service_id or f"mock-{platform.value}-{metric.value}",
        metric=metric,
        platform=platform,
        quantity=quantity,
        price_per_unit=entry["price_per_unit"],
        total_price=entry["price_per_unit"] * quantity,
        eta_minutes_min=entry["eta_min"],
        eta_minutes_max=entry["eta_max"],
        confidence=confidence,
        raw={"source": "mock_config"},
    )


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
