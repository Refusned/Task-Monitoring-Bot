"""Typed contracts for the LLM autopilot layer."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator

from models import Scenario, SourcePlatform


class AutopilotIntent(BaseModel):
    """Structured interpretation of a user's natural-language business goal."""

    scenario: Scenario
    target: str = Field(min_length=1)
    quantity: int = Field(gt=0)
    source_platform: SourcePlatform | None = None
    max_cost: float | None = Field(default=None, gt=0)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    notes: str = ""

    @model_validator(mode="after")
    def _traffic_needs_source(self) -> AutopilotIntent:
        if self.scenario == Scenario.SOCIAL_TRAFFIC and self.source_platform is None:
            raise ValueError("source_platform is required for social_traffic")
        return self


class AutopilotCandidate(BaseModel):
    """A costed service candidate from an exchange catalogue."""

    exchange: str
    service_id: str
    service_name: str
    price_per_unit: float = Field(gt=0)
    estimated_cost: float = Field(ge=0)
    min_quantity: int | None = None
    max_quantity: int | None = None


class AutopilotResult(BaseModel):
    """Outcome of autopilot planning and optional execution."""

    status: Literal["created", "planned", "dry_run", "no_candidates", "llm_error", "create_failed"]
    intent: AutopilotIntent | None = None
    selected: AutopilotCandidate | None = None
    candidates: list[AutopilotCandidate] = Field(default_factory=list)
    order_uuid: str | None = None
    external_order_id: str | None = None
    cost: float | None = None
    reason: str = ""
