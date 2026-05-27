"""Domain models, enums, and value types. Single source of truth for the data shape."""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field, model_validator


class Scenario(StrEnum):
    """Client-facing order scenarios supported by the bot."""

    ACTIVITY_SUBSCRIBE = "activity_subscribe"
    ACTIVITY_LIKE = "activity_like"
    ACTIVITY_VIEW = "activity_view"
    SOCIAL_TRAFFIC = "social_traffic"


class SourcePlatform(StrEnum):
    """Traffic source platforms supported by the reporting flow."""

    VK = "vk"
    X = "x"
    YOUTUBE = "youtube"
    TELEGRAM = "telegram"
    DZEN = "dzen"
    PINTEREST = "pinterest"


class ExchangeKind(StrEnum):
    PANEL = "panel"
    TASK_EXCHANGE = "task_exchange"


class OrderStatus(StrEnum):
    """Order lifecycle. The explicit money-action state `CREATING` enables C1."""

    DRAFT = "draft"
    CREATING = "creating"
    ACTIVE = "active"
    VERIFYING = "verifying"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class SubmissionStatus(StrEnum):
    """Per-submission lifecycle on microtask exchanges; explicit money-action states (C2)."""

    NEW = "new"
    VERIFYING = "verifying"
    AWAITING_ADMIN = "awaiting_admin"
    ACCEPTING = "accepting"
    ACCEPTED = "accepted"
    REJECTING = "rejecting"
    REWORK_REQUESTED = "rework_requested"
    FAILED = "failed"


class VerificationVerdict(StrEnum):
    AUTO_PASS = "auto_pass"
    NEEDS_HUMAN_REVIEW = "needs_human_review"
    FAIL = "fail"


def new_client_order_uuid() -> str:
    """Per-order UUID, generated once at user confirmation.

    Becomes the order's identity (replaces a brittle natural-key hash). Passed to
    exchanges that declare `SUPPORTS_CLIENT_ORDER_ID`.
    """
    return str(uuid.uuid4())


class OrderSpec(BaseModel):
    """User-confirmed order parameters; immutable after creation."""

    scenario: Scenario
    exchange: str = Field(min_length=1)  # adapter name (e.g. 'smmcode', 'unu')
    target: str = Field(min_length=1)  # URL / account / post URL depending on scenario
    quantity: int = Field(gt=0)
    service_id: str | None = None  # exchange-specific catalog id, where present
    source_platform: SourcePlatform | None = None  # required for SOCIAL_TRAFFIC
    max_cost: float = Field(gt=0)

    @model_validator(mode="after")
    def _check_source_platform_required_for_traffic(self) -> OrderSpec:
        """A8: SOCIAL_TRAFFIC orders must declare which platform the traffic comes from."""
        if self.scenario == Scenario.SOCIAL_TRAFFIC and self.source_platform is None:
            raise ValueError(
                "source_platform is required for SOCIAL_TRAFFIC scenario "
                "(must be one of VK / X / YouTube / Telegram / Dzen / Pinterest)"
            )
        return self


class Order(BaseModel):
    """An internal order; SQLite is the source of truth."""

    client_order_uuid: str
    spec: OrderSpec
    status: OrderStatus = OrderStatus.DRAFT
    external_order_id: str | None = None
    cost_actual: float | None = None
    created_at: datetime
    updated_at: datetime


class ExternalSubmission(BaseModel):
    """Submission as the EXCHANGE knows it - before the orchestrator persists it.

    Adapter `list_submissions` returns these; the orchestrator assigns the internal
    `submission_uuid` once on persistence (HIGH-b fix from Day 1 audit: prevents the
    adapter from generating a fresh random UUID on every poll, which would break
    idempotent persistence and FK integrity).
    """

    external_submission_id: str = Field(min_length=1)
    executor_hint: str | None = None
    evidence: str | None = None


class Submission(BaseModel):
    """A worker-submitted report on a microtask exchange - internal persisted form."""

    submission_uuid: str
    order_uuid: str
    external_submission_id: str | None
    executor_hint: str | None = None
    status: SubmissionStatus = SubmissionStatus.NEW
    evidence: str | None = None
    created_at: datetime


class VerificationResult(BaseModel):
    """Output of the verification layer (A4)."""

    verdict: VerificationVerdict
    measured: float
    expected: float
    reason: str
    raw_evidence: dict
