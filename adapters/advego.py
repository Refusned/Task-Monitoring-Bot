"""Real adapter for the advego.com microtask exchange API.

API form: XML-RPC over HTTPS POST to `https://api.advego.com/xml`. Auth: every
method takes a `token` member. Docs reference: `docs/api/advego.md`.

We use Python's stdlib `xmlrpc.client` to (de)serialize the XML envelopes, then
ship them over `httpx` to stay async-native. The wire format details (XML
escaping, dateTime types, fault handling) all live inside `xmlrpc.client`.

Notable behaviours:

- **Estimate-first cost check.** Before placing, the adapter requires `spec.max_cost`
  to be set; the API's `addOrder` accepts `cost`/`jobs_total` so we pass
  `cost = spec.max_cost / spec.quantity` per-job and validate it stays positive.
- **Submission lifecycle.** `getJobsCompleted` returns jobs awaiting moderation;
  `acceptJob` pays, `returnJob` sends to revision (≥10-char comment required).
- **Client order id**: advego does not accept client-side ids; identity = `order_id`.
- **Order status.** `ordersGetState` returns per-order state; we map raw values to
  normalized `'in_progress' | 'completed' | 'failed'`.
"""

from __future__ import annotations

import xmlrpc.client
from typing import Any

import httpx

from adapters.base import Capability, TaskExchangeAdapter, quote_from_mock
from models import ExternalSubmission, OrderSpec, Quote, SourcePlatform, TaskType, TopupInfo

_BASE_URL = "https://api.advego.com/xml"

# advego.ordersGetState raw `state` -> normalized status. Per the public docs the
# state strings are lowercase identifiers; the mapping is conservative ("anything
# we don't know -> failed" so we never silently auto-finalize).
_ORDER_STATE_MAP: dict[str, str] = {
    "active": "in_progress",
    "pending": "in_progress",
    "moderation": "in_progress",
    "waiting": "in_progress",
    "completed": "completed",
    "finished": "completed",
    "done": "completed",
    "stopped": "failed",
    "cancelled": "failed",
    "rejected": "failed",
    "error": "failed",
}

# advego.returnJob / declineJob require a comment of at least 10 chars.
_MIN_COMMENT_LEN = 10


class AdvegoAdapter(TaskExchangeAdapter):
    """advego.com XML-RPC adapter."""

    name = "advego"

    def __init__(
        self,
        token: str,
        http_client: httpx.AsyncClient,
        *,
        default_campaign_id: int | None = None,
    ) -> None:
        if not token:
            raise ValueError("advego token is required (set ADVEGO_API_TOKEN in .env)")
        self._token = token
        self._client = http_client
        self._default_campaign_id = default_campaign_id

    def capabilities(self) -> set[Capability]:
        return {
            Capability.CREATE_ORDER,
            Capability.GET_ORDER_STATUS,
            # GET_BALANCE intentionally NOT here: advego XML-RPC has no public
            # balance method (confirmed against 8 candidate method names).
            Capability.LIST_SUBMISSIONS,
            Capability.ACCEPT_SUBMISSION,
            Capability.REJECT_SUBMISSION,
            Capability.GET_QUOTE,
            Capability.GET_TOPUP_INFO,
        }

    async def get_quote(
        self, metric: TaskType, platform: SourcePlatform, quantity: int
    ) -> Quote | None:
        """Mock-only for MVP. advego catalog is XML-RPC; parsing is post-MVP."""
        from config import get_settings

        return quote_from_mock(
            self.name, metric, platform, quantity, get_settings().mock_quotes_csv,
            confidence=0.4,
        )

    async def get_topup_info(self) -> TopupInfo:
        return TopupInfo(
            exchange=self.name,
            topup_url="https://advego.com/billing/",
            min_amount=300.0,
            currency="RUB",
            payment_methods=["card", "yoomoney", "qiwi", "wire_transfer"],
            notes="Микрозадачная биржа Advego; пополняется через раздел Billing.",
        )

    # --- balance + create -----------------------------------------------------

    async def get_balance(self) -> float | None:
        # advego's XML-RPC API has NO balance method. Confirmed 2026-05-28
        # against 8 candidate names — all return {"error": "Wrong method"}.
        # `advego.getCompletedJobs` does work (proves auth is fine), but
        # there's no public way to read customer cash balance.
        # Real balance lives at https://advego.com/dashboard/ for the operator.
        return None

    async def create_order(self, spec: OrderSpec, client_order_uuid: str) -> tuple[str, float]:
        if self._default_campaign_id is None:
            raise ValueError(
                "advego requires default_campaign_id (caller must supply or fetch via "
                "advego.getMyCampaigns)"
            )
        # Estimate-first: cap is the budget; cost-per-job = cap / quantity.
        per_job = spec.max_cost / spec.quantity
        if per_job <= 0:
            raise ValueError(f"computed per-job cost must be positive (got {per_job!r})")
        # `order_type` 18 = "Лайки в соцсетях" (cheapest social-action type) is the
        # default. Real callers can pass `spec.service_id` to override (string→int).
        try:
            order_type = int(spec.service_id) if spec.service_id else 18
        except ValueError as exc:
            raise ValueError(f"advego service_id must be a numeric order_type: {exc}") from exc

        params = {
            "token": self._token,
            "campaign_id": self._default_campaign_id,
            "title": f"order-{client_order_uuid[:8]}"[:120],
            "description": spec.target[:50000],
            "order_type": order_type,
            "length": 1,  # minimal — these task types don't need text length
            "cost": per_job,
            "jobs_total": spec.quantity,
        }
        data = await self._call("advego.addOrder", [params])
        external_id = str(data["order_id"]) if isinstance(data, dict) else str(data)
        # The adapter's accept-side cost is the cap; actual spend is per accepted job
        # and recorded as payments. We return cap as the placed amount.
        return external_id, spec.max_cost

    async def get_order_status(self, external_order_id: str) -> str:
        data = await self._call(
            "advego.ordersGetState",
            [{"token": self._token, "orders": [int(external_order_id)]}],
        )
        # Expected shape: {order_id: {state: "...", ...}} OR list of dicts.
        state_str: str | None = None
        if isinstance(data, dict):
            entry = data.get(int(external_order_id)) or data.get(str(external_order_id))
            if isinstance(entry, dict):
                state_str = entry.get("state")
            elif isinstance(entry, str):
                state_str = entry
        if state_str is None:
            return "failed"
        return _ORDER_STATE_MAP.get(state_str.strip().lower(), "failed")

    # --- submissions ----------------------------------------------------------

    async def list_submissions(self, external_order_id: str) -> list[ExternalSubmission]:
        data = await self._call(
            "advego.getJobsCompleted",
            [
                {
                    "token": self._token,
                    "id_order": int(external_order_id),
                    "date_type": "complete",
                }
            ],
        )
        if isinstance(data, list):
            jobs = data
        elif isinstance(data, dict):
            jobs = data.get("jobs") or []
        else:
            jobs = []
        results: list[ExternalSubmission] = []
        for j in jobs:
            if not isinstance(j, dict):
                continue
            job_id = j.get("id") or j.get("job_id")
            if job_id is None:
                continue
            worker_id = j.get("worker_id")
            results.append(
                ExternalSubmission(
                    external_submission_id=str(job_id),
                    executor_hint=str(worker_id) if worker_id is not None else None,
                    evidence=self._extract_evidence(j),
                )
            )
        return results

    async def accept_submission(self, external_order_id: str, external_submission_id: str) -> None:
        await self._call(
            "advego.acceptJob",
            [{"token": self._token, "ID": int(external_submission_id)}],
        )

    async def reject_submission(
        self, external_order_id: str, external_submission_id: str, reason: str
    ) -> None:
        comment = reason or ""
        if len(comment) < _MIN_COMMENT_LEN:
            # advego refuses comments <10 chars; pad with deterministic suffix so
            # the caller's intent (rework) still goes through.
            comment = (comment + " — rework requested by bot").strip()
        await self._call(
            "advego.returnJob",
            [
                {
                    "token": self._token,
                    "ID": int(external_submission_id),
                    "comment": comment,
                }
            ],
        )

    # --- internal helpers -----------------------------------------------------

    async def _call(self, method: str, params: list[Any]) -> Any:
        body = xmlrpc.client.dumps(tuple(params), methodname=method, allow_none=True)
        response = await self._client.post(
            _BASE_URL,
            content=body,
            headers={"Content-Type": "text/xml; charset=utf-8"},
        )
        response.raise_for_status()
        try:
            result, _ = xmlrpc.client.loads(response.content)
        except xmlrpc.client.Fault as fault:
            # Strip token from any echo; the message is operator-relevant.
            raise RuntimeError(
                f"advego {method} fault: code={fault.faultCode} string={fault.faultString!r}"
            ) from fault
        if not result:
            return None
        return result[0]

    @staticmethod
    def _extract_evidence(job: dict) -> str | None:
        """Pull the worker's evidence from an advego job payload.

        advego jobs typically carry the report text in `report` / `comment` /
        `description`; the verifier downstream consumes the raw blob.
        """
        for key in ("report", "comment", "description", "url"):
            value = job.get(key)
            if value:
                return str(value)
        return None
