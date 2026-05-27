"""Autopilot orchestration: structured intent -> cheapest viable order."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol

from adapters.base import Capability, ExchangeAdapter, PanelAdapter, TaskExchangeAdapter
from autopilot.models import AutopilotCandidate, AutopilotIntent, AutopilotResult
from autopilot.ollama import OllamaPlannerError
from cli import _persist_and_create
from config import Settings
from models import OrderSpec


class GoalPlanner(Protocol):
    async def plan_goal(self, goal_text: str) -> AutopilotIntent: ...


class AutopilotRunner:
    """Runs the fully automatic order-placement path."""

    def __init__(
        self,
        settings: Settings,
        adapters: Mapping[str, ExchangeAdapter],
        planner: GoalPlanner,
    ) -> None:
        self._settings = settings
        self._adapters = adapters
        self._planner = planner

    async def run_goal(
        self,
        goal_text: str,
        *,
        actor: str,
        execute: bool = True,
    ) -> AutopilotResult:
        try:
            intent = await self._planner.plan_goal(goal_text)
        except OllamaPlannerError as exc:
            return AutopilotResult(status="llm_error", reason=str(exc))

        budget = intent.max_cost or self._settings.per_order_spend_limit
        candidates = await self._collect_candidates(intent, budget)
        if not candidates:
            return AutopilotResult(
                status="no_candidates",
                intent=intent,
                reason=(
                    "No exchange catalogue returned a priced service that fits "
                    f"quantity={intent.quantity} and max_cost={budget:.2f}"
                ),
            )

        selected = candidates[0]
        if not execute:
            return AutopilotResult(
                status="planned",
                intent=intent,
                selected=selected,
                candidates=candidates,
                reason="plan_only",
            )
        if self._settings.dry_run:
            return AutopilotResult(
                status="dry_run",
                intent=intent,
                selected=selected,
                candidates=candidates,
                reason="DRY_RUN=true: external order creation is disabled",
            )

        spec = OrderSpec(
            scenario=intent.scenario,
            exchange=selected.exchange,
            target=intent.target,
            quantity=intent.quantity,
            service_id=selected.service_id,
            source_platform=intent.source_platform,
            max_cost=budget,
        )
        adapter = self._adapters[selected.exchange]
        try:
            order_uuid, external_id, cost = await _persist_and_create(
                self._settings,
                adapter,
                spec,
                actor=actor,
            )
        except Exception as exc:
            return AutopilotResult(
                status="create_failed",
                intent=intent,
                selected=selected,
                candidates=candidates,
                reason=f"{type(exc).__name__}: {exc}",
            )

        return AutopilotResult(
            status="created",
            intent=intent,
            selected=selected,
            candidates=candidates,
            order_uuid=order_uuid,
            external_order_id=external_id,
            cost=cost,
        )

    async def _collect_candidates(
        self,
        intent: AutopilotIntent,
        budget: float,
    ) -> list[AutopilotCandidate]:
        candidates: list[AutopilotCandidate] = []
        for exchange, adapter in sorted(self._adapters.items()):
            if Capability.CREATE_ORDER not in adapter.capabilities():
                continue
            if not isinstance(adapter, (PanelAdapter, TaskExchangeAdapter)):
                continue
            try:
                options = await adapter.list_services_for_scenario(
                    intent.scenario,
                    limit=self._settings.autopilot_candidate_limit_per_exchange,
                )
            except Exception:
                continue
            for option in options:
                if not option.service_id or option.price_per_unit is None:
                    continue
                if option.price_per_unit <= 0:
                    continue
                if option.min_quantity is not None and intent.quantity < option.min_quantity:
                    continue
                if option.max_quantity is not None and intent.quantity > option.max_quantity:
                    continue
                estimated_cost = option.price_per_unit * intent.quantity
                if estimated_cost > budget:
                    continue
                candidates.append(
                    AutopilotCandidate(
                        exchange=exchange,
                        service_id=option.service_id,
                        service_name=option.name,
                        price_per_unit=option.price_per_unit,
                        estimated_cost=estimated_cost,
                        min_quantity=option.min_quantity,
                        max_quantity=option.max_quantity,
                    )
                )
        candidates.sort(key=_candidate_sort_key)
        return candidates


def _candidate_sort_key(candidate: AutopilotCandidate) -> tuple[float, float, str, str]:
    return (
        candidate.estimated_cost,
        candidate.price_per_unit,
        candidate.exchange,
        candidate.service_name,
    )


def format_autopilot_result(result: AutopilotResult) -> str:
    """Human-readable summary for CLI and Telegram."""
    if result.status == "llm_error":
        return f"LLM error: {result.reason}"
    if result.intent is None:
        return f"Autopilot status={result.status}: {result.reason}"

    lines = [
        f"status: {result.status}",
        f"scenario: {result.intent.scenario.value}",
        f"target: {result.intent.target}",
        f"quantity: {result.intent.quantity}",
    ]
    if result.intent.source_platform is not None:
        lines.append(f"source_platform: {result.intent.source_platform.value}")
    if result.intent.max_cost is not None:
        lines.append(f"max_cost: {result.intent.max_cost:.2f}")
    if result.selected is not None:
        lines.extend(
            [
                f"selected_exchange: {result.selected.exchange}",
                f"selected_service: {result.selected.service_id} ({result.selected.service_name})",
                f"estimated_cost: {result.selected.estimated_cost:.2f}",
            ]
        )
    if result.order_uuid is not None:
        lines.append(f"order_uuid: {result.order_uuid}")
    if result.external_order_id is not None:
        lines.append(f"external_order_id: {result.external_order_id}")
    if result.cost is not None:
        lines.append(f"actual_cost: {result.cost:.2f}")
    if result.reason:
        lines.append(f"reason: {result.reason}")
    if result.candidates:
        lines.append("candidates:")
        for candidate in result.candidates[:5]:
            lines.append(
                "- "
                f"{candidate.exchange}:{candidate.service_id} "
                f"{candidate.service_name} "
                f"price={candidate.price_per_unit:.4f} "
                f"cost={candidate.estimated_cost:.2f}"
            )
    return "\n".join(lines)
