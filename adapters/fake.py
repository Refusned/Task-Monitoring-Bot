"""Fake adapters for the DRY_RUN demo and tests.

Both adapters simulate the full lifecycle deterministically:

- `FakePanelAdapter`: creates a prepaid order; reports `completed` after N polls.
- `FakeTaskExchangeAdapter`: creates a campaign; yields N submissions after one
  poll; each submission carries a deterministic `evidence_quality` hint that the
  verifier (Day 5) reads to produce a verdict. The first `(N - 1)` are 'good',
  the last is 'weak' - so the demo exercises both accept and reject paths.

Adapters enforce `spec.max_cost` (HIGH-c fix). The proper estimate-first contract
will ship on Day 3 with the orchestrator; for now adapters refuse placement if the
computed cost would exceed the cap, so the safety property holds end-to-end.
"""

from __future__ import annotations

import asyncio
import uuid

from adapters.base import Capability, PanelAdapter, TaskExchangeAdapter
from models import ExternalSubmission, OrderSpec

_PANEL_COST_PER_UNIT = 0.05
_TASK_COST_PER_UNIT = 0.10


class FakePanelAdapter(PanelAdapter):
    """SMM-panel simulator. Balance is deducted on create; the order transitions
    to `completed` after `polls_to_complete` `get_order_status` calls.
    """

    name = "fake_panel"

    def __init__(self, polls_to_complete: int = 2, starting_balance: float = 1000.0) -> None:
        self._polls_to_complete = polls_to_complete
        self._orders: dict[str, dict] = {}
        self._balance = starting_balance

    def capabilities(self) -> set[Capability]:
        return {
            Capability.CREATE_ORDER,
            Capability.GET_ORDER_STATUS,
            Capability.GET_BALANCE,
            Capability.SUPPORTS_CLIENT_ORDER_ID,
        }

    async def get_balance(self) -> float:
        await asyncio.sleep(0)
        return self._balance

    async def create_order(self, spec: OrderSpec, client_order_uuid: str) -> tuple[str, float]:
        await asyncio.sleep(0)
        cost = spec.quantity * _PANEL_COST_PER_UNIT
        if cost > spec.max_cost:
            raise ValueError(f"computed cost {cost:.2f} exceeds spec.max_cost {spec.max_cost:.2f}")
        if cost > self._balance:
            raise RuntimeError(f"insufficient balance: cost={cost} balance={self._balance}")
        external_id = f"fake-panel-{uuid.uuid4().hex[:8]}"
        self._orders[external_id] = {
            "polls": 0,
            "client_uuid": client_order_uuid,
            "quantity": spec.quantity,
        }
        self._balance -= cost
        return external_id, cost

    async def get_order_status(self, external_order_id: str) -> str:
        record = self._orders.get(external_order_id)
        if record is None:
            return "failed"
        record["polls"] += 1
        return "completed" if record["polls"] >= self._polls_to_complete else "in_progress"


class FakeTaskExchangeAdapter(TaskExchangeAdapter):
    """Microtask-exchange simulator. Yields N submissions on the first poll;
    each submission carries an evidence_quality hint ('good' / 'weak') so the
    demo exercises both accept and reject paths once the verifier is wired in.

    Submission identity is STABLE (HIGH-b fix): external IDs created on first yield
    are returned by every subsequent `list_submissions` call until they're resolved,
    so the orchestrator can map external -> internal UUID once and persist reliably.
    """

    name = "fake_task_exchange"

    def __init__(
        self,
        polls_to_yield: int = 1,
        submissions_per_order: int = 3,
        starting_balance: float = 1000.0,
    ) -> None:
        self._polls_to_yield = polls_to_yield
        self._submissions_per_order = submissions_per_order
        self._orders: dict[str, dict] = {}
        self._submissions: dict[str, dict] = {}
        self._balance = starting_balance

    def capabilities(self) -> set[Capability]:
        return {
            Capability.CREATE_ORDER,
            Capability.GET_ORDER_STATUS,
            Capability.GET_BALANCE,
            Capability.LIST_SUBMISSIONS,
            Capability.ACCEPT_SUBMISSION,
            Capability.REJECT_SUBMISSION,
            Capability.SUPPORTS_CLIENT_ORDER_ID,
        }

    async def get_balance(self) -> float:
        await asyncio.sleep(0)
        return self._balance

    async def create_order(self, spec: OrderSpec, client_order_uuid: str) -> tuple[str, float]:
        await asyncio.sleep(0)
        cost = spec.quantity * _TASK_COST_PER_UNIT
        if cost > spec.max_cost:
            raise ValueError(f"computed cost {cost:.2f} exceeds spec.max_cost {spec.max_cost:.2f}")
        if cost > self._balance:
            raise RuntimeError(f"insufficient balance: cost={cost} balance={self._balance}")
        external_id = f"fake-task-{uuid.uuid4().hex[:8]}"
        self._orders[external_id] = {
            "polls": 0,
            "client_uuid": client_order_uuid,
            "quantity": spec.quantity,
            "yielded": False,
        }
        self._balance -= cost
        return external_id, cost

    async def get_order_status(self, external_order_id: str) -> str:
        record = self._orders.get(external_order_id)
        if record is None:
            return "failed"
        record["polls"] += 1
        if record["polls"] >= self._polls_to_yield and not record["yielded"]:
            record["yielded"] = True
            for i in range(self._submissions_per_order):
                sid = f"fake-sub-{uuid.uuid4().hex[:8]}"
                quality = "good" if i < self._submissions_per_order - 1 else "weak"
                self._submissions[sid] = {
                    "external_order_id": external_order_id,
                    "status": "new",
                    "executor": f"executor-{i + 1}",
                    "evidence_quality": quality,
                }
        unresolved = [
            sid
            for sid, s in self._submissions.items()
            if s["external_order_id"] == external_order_id and s["status"] == "new"
        ]
        if record["yielded"] and not unresolved:
            return "completed"
        return "in_progress"

    async def list_submissions(self, external_order_id: str) -> list[ExternalSubmission]:
        await asyncio.sleep(0)
        results: list[ExternalSubmission] = []
        for sid, s in self._submissions.items():
            if s["external_order_id"] != external_order_id:
                continue
            if s["status"] != "new":
                continue
            results.append(
                ExternalSubmission(
                    external_submission_id=sid,
                    executor_hint=s["executor"],
                    evidence=s["evidence_quality"],
                )
            )
        return results

    async def accept_submission(self, external_order_id: str, external_submission_id: str) -> None:
        await asyncio.sleep(0)
        s = self._submissions.get(external_submission_id)
        if s is None or s["external_order_id"] != external_order_id:
            raise ValueError(f"unknown submission {external_submission_id}")
        if s["status"] != "new":
            raise ValueError(f"submission {external_submission_id} already terminal")
        s["status"] = "accepted"

    async def reject_submission(
        self, external_order_id: str, external_submission_id: str, reason: str
    ) -> None:
        await asyncio.sleep(0)
        s = self._submissions.get(external_submission_id)
        if s is None or s["external_order_id"] != external_order_id:
            raise ValueError(f"unknown submission {external_submission_id}")
        if s["status"] != "new":
            raise ValueError(f"submission {external_submission_id} already terminal")
        s["status"] = "rejected"
        s["reject_reason"] = reason
