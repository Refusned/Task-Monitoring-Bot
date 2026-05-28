"""Domain models, enums, and value types. Single source of truth for the data shape."""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field, model_validator


class Scenario(StrEnum):
    """The two MVP scenarios from the test task."""

    ACTIVITY_SUBSCRIBE = "activity_subscribe"
    ACTIVITY_LIKE = "activity_like"
    SOCIAL_TRAFFIC = "social_traffic"


class SourcePlatform(StrEnum):
    """A8 - six traffic source platforms named in the task."""

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


# ===== Agent-layer additions (v4 pivot — LLM orchestrator) =====
# These are agent-facing concepts; the orchestrator/adapters keep OrderSpec.scenario
# as a wire-compatible mapping (LIKES→ACTIVITY_LIKE, VIEWS→SOCIAL_TRAFFIC, etc.).


class TaskType(StrEnum):
    """Metric the agent is asked to deliver. Maps to legacy Scenario at the
    orchestrator boundary so existing adapters/idempotency stay untouched."""

    LIKES = "likes"
    VIEWS = "views"
    SUBSCRIBES = "subscribes"
    COMMENTS = "comments"
    SHARES = "shares"
    TRAFFIC = "traffic"


def task_type_to_scenario(task: TaskType) -> Scenario:
    """Bridge: agent-facing TaskType → legacy Scenario the orchestrator understands."""
    if task in (TaskType.SUBSCRIBES,):
        return Scenario.ACTIVITY_SUBSCRIBE
    if task in (TaskType.LIKES, TaskType.COMMENTS, TaskType.SHARES):
        return Scenario.ACTIVITY_LIKE
    # VIEWS and TRAFFIC both end up as traffic-style scenarios for accounting.
    return Scenario.SOCIAL_TRAFFIC


class Quote(BaseModel):
    """Price quote for one (exchange, platform, metric, quantity) tuple.

    `None` is returned by `get_quote` when the exchange does not support the
    requested combination; callers must handle that.
    """

    exchange: str = Field(min_length=1)
    service_id: str = Field(min_length=1)
    metric: TaskType
    platform: SourcePlatform
    quantity: int = Field(gt=0)
    price_per_unit: float = Field(ge=0)
    total_price: float = Field(ge=0)
    currency: str = Field(default="RUB", min_length=1)
    eta_minutes_min: int = Field(ge=0)
    eta_minutes_max: int = Field(ge=0)
    confidence: float = Field(ge=0.0, le=1.0)
    raw: dict = Field(default_factory=dict)


class TopupInfo(BaseModel):
    """How to fund this specific exchange. Hardcoded URLs per adapter."""

    exchange: str = Field(min_length=1)
    topup_url: str = Field(min_length=1)
    min_amount: float = Field(ge=0)
    currency: str = "USD"
    payment_methods: list[str] = Field(default_factory=list)
    notes: str = ""


class MetricSnapshot(BaseModel):
    """Baseline reading of a platform metric BEFORE an order is placed.
    Persisted so the verifier can compute the delta later."""

    snapshot_id: str = Field(min_length=1)
    platform: SourcePlatform
    target_url: str = Field(min_length=1)
    metric: TaskType
    baseline_value: float = Field(ge=0)
    captured_at: datetime
    raw: dict = Field(default_factory=dict)


def new_snapshot_id() -> str:
    """Snapshot identity, generated once at baseline capture."""
    return f"snap_{uuid.uuid4().hex[:12]}"


class ExchangeBalance(BaseModel):
    """A single exchange balance reading; many of these form `get_balances` output.
    All 5 exchanges in our stack (smmcode/prskill/unu/advego/ipgold) operate in RUB."""

    exchange: str = Field(min_length=1)
    amount: float = Field(ge=0)
    currency: str = "RUB"
    fetched_at: datetime
    stale: bool = False  # True if cache hit older than ttl, returned anyway as best-effort
    no_api: bool = False  # True if the exchange's public API has no balance method
    # (prskill/advego/ipgold — operator checks balance in their cabinet)


class AgentEvent(BaseModel):
    """One entry in the live agent feed for the dashboard."""

    event_id: int | None = None  # autoincrement on insert
    occurred_at: datetime
    kind: str = Field(min_length=1)  # "tool_call" | "tool_result" | "schedule" | "report"
    payload: dict
    order_uuid: str | None = None
