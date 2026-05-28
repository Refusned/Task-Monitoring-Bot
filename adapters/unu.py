"""Real adapter for the unu.im microtask-exchange API.

API form: POST `https://unu.im/api` with body params `api_key` + `action` + per-action
extras. Responses JSON: `{"success": int, "errors": text, ...payload}`. Docs reference:
`docs/api/unu.md`.

Notable behaviours:

- **Estimate-first cost check** (HIGH-c invariant). Before placing, the adapter looks
  up `tarif_id` price via cached `get_tariffs`, computes `cost = price * quantity`,
  refuses if it exceeds `spec.max_cost`. `add_task_start` is NOT called when the cap
  is exceeded.
- **Order lifecycle = "task".** `add_task_start` creates+funds in one atomic call;
  status read via `get_tasks` (raw `status`) and mapped to our normalized
  `'in_progress' | 'completed' | 'failed'`.
- **Submission lifecycle = "report".** `get_reports` lists pending reports;
  `approve_report` / `reject_report` are the money actions.
- **Client order id**: UNU does not support a client-side identifier; identity on
  the exchange side is the returned `task_id`.
"""

from __future__ import annotations

import time
from typing import Any

import httpx

from adapters.base import Capability, TaskExchangeAdapter, quote_from_mock
from models import ExternalSubmission, OrderSpec, Quote, SourcePlatform, TaskType, TopupInfo

_BASE_URL = "https://unu.im/api"
_TARIFFS_TTL_SECONDS = 60.0

# Raw `task.status` (`get_tasks`) -> normalized status. Source: docs/api/unu.md.
_TASK_STATUS_MAP: dict[int, str] = {
    1: "in_progress",  # New, needs pay
    2: "completed",  # Limit reached
    3: "failed",  # Stopped / cancelled
    4: "in_progress",  # Active
    5: "failed",  # Rejected by moderator
    6: "in_progress",  # On moderation
}

# Raw `report.status` (`get_reports`) -> only "awaiting_admin" reports need attention.
# `2` = "on review" is the only one we surface for accept/reject (the others are
# already terminal from our perspective: 1 in-work, 3 in-revision, 6 paid).
_REPORT_STATUS_AWAITING = 2


class UnuAdapter(TaskExchangeAdapter):
    """unu.im task-exchange adapter."""

    name = "unu"

    def __init__(
        self,
        api_key: str,
        http_client: httpx.AsyncClient,
        *,
        default_folder_id: int = 0,
    ) -> None:
        if not api_key:
            raise ValueError("unu api_key is required (set UNU_API_KEY in .env)")
        self._api_key = api_key
        self._client = http_client
        self._default_folder_id = default_folder_id
        self._tariffs_cache: dict[str, dict[str, Any]] | None = None
        self._tariffs_cache_at: float = 0.0

    def capabilities(self) -> set[Capability]:
        return {
            Capability.CREATE_ORDER,
            Capability.GET_ORDER_STATUS,
            Capability.GET_BALANCE,
            Capability.LIST_SUBMISSIONS,
            Capability.ACCEPT_SUBMISSION,
            Capability.REJECT_SUBMISSION,
            Capability.GET_QUOTE,
            Capability.GET_TOPUP_INFO,
            # NB: UNU does not accept client-side order ids.
        }

    async def get_quote(
        self, metric: TaskType, platform: SourcePlatform, quantity: int
    ) -> Quote | None:
        """Mock-only for MVP. To go real: hardcode a tarif_id map and call
        _tariff_price() — the tariff catalogue is already cached."""
        from config import get_settings

        return quote_from_mock(
            self.name, metric, platform, quantity, get_settings().mock_quotes_csv,
            confidence=0.4,
        )

    async def get_topup_info(self) -> TopupInfo:
        return TopupInfo(
            exchange=self.name,
            topup_url="https://unu.im/?do=funds",
            min_amount=100.0,
            currency="RUB",
            payment_methods=["card", "yoomoney", "qiwi", "webmoney"],
            notes="Микрозадачная биржа; пополняется в личном кабинете.",
        )

    # --- balance + create -----------------------------------------------------

    async def get_balance(self) -> float:
        data = await self._post("get_balance", {})
        return float(data["balance"])

    async def create_order(self, spec: OrderSpec, client_order_uuid: str) -> tuple[str, float]:
        if not spec.service_id:
            raise ValueError("unu requires spec.service_id (tarif_id from get_tariffs)")
        price_per_unit = await self._tariff_price(spec.service_id)
        cost = price_per_unit * spec.quantity
        if cost > spec.max_cost:
            raise ValueError(f"computed cost {cost:.2f} exceeds spec.max_cost {spec.max_cost:.2f}")

        params: dict[str, Any] = {
            "name": f"order-{client_order_uuid[:8]}",
            "descr": spec.target,
            "need_for_report": spec.target,
            "price": str(price_per_unit),
            "tarif_id": str(spec.service_id),
            "folder_id": str(self._default_folder_id),
            "add_to_limit": str(spec.quantity),
            "link": spec.target,
        }
        data = await self._post("add_task_start", params)
        external_id = str(data["task_id"])
        return external_id, cost

    async def get_order_status(self, external_order_id: str) -> str:
        data = await self._post("get_tasks", {"task_id": str(external_order_id)})
        tasks = data.get("tasks") or []
        if not tasks:
            return "failed"
        task = tasks[0] if isinstance(tasks, list) else tasks
        try:
            raw_status = int(task.get("status", 0))
        except (TypeError, ValueError):
            return "failed"
        return _TASK_STATUS_MAP.get(raw_status, "failed")

    # --- submissions ----------------------------------------------------------

    async def list_submissions(self, external_order_id: str) -> list[ExternalSubmission]:
        data = await self._post("get_reports", {"task_id": str(external_order_id)})
        reports = data.get("reports") or []
        results: list[ExternalSubmission] = []
        for r in reports:
            if not isinstance(r, dict):
                continue
            try:
                status = int(r.get("status", 0))
            except (TypeError, ValueError):
                continue
            if status != _REPORT_STATUS_AWAITING:
                continue
            report_id = r.get("id")
            if report_id is None:
                continue
            messages = r.get("messages")
            evidence = self._extract_evidence(messages)
            worker_id = r.get("worker_id")
            results.append(
                ExternalSubmission(
                    external_submission_id=str(report_id),
                    executor_hint=str(worker_id) if worker_id is not None else None,
                    evidence=evidence,
                )
            )
        return results

    async def accept_submission(self, external_order_id: str, external_submission_id: str) -> None:
        # UNU's approve_report takes only report_id; external_order_id is accepted
        # for interface compatibility but not sent (the report row already references it).
        await self._post("approve_report", {"report_id": str(external_submission_id)})

    async def reject_submission(
        self, external_order_id: str, external_submission_id: str, reason: str
    ) -> None:
        # reject_type=1 -> "to revision" (rework). reject_type=2 would refuse outright.
        await self._post(
            "reject_report",
            {
                "report_id": str(external_submission_id),
                "comment": reason or "rework requested",
                "reject_type": "1",
            },
        )

    # --- internal helpers -----------------------------------------------------

    async def _post(self, action: str, params: dict[str, Any]) -> dict[str, Any]:
        body = {"api_key": self._api_key, "action": action, **params}
        response = await self._client.post(_BASE_URL, data=body)
        response.raise_for_status()
        payload = response.json()
        try:
            success = int(payload.get("success", 0))
        except (TypeError, ValueError):
            success = 0
        if success != 1:
            # Strip api_key from the diagnostic; surface the rest.
            safe = {k: v for k, v in payload.items() if k != "api_key"}
            raise RuntimeError(f"unu {action} returned non-success payload: {safe!r}")
        return payload

    async def _tariff_price(self, tarif_id: str) -> float:
        tariffs = await self._get_tariffs()
        tariff = tariffs.get(str(tarif_id))
        if tariff is None:
            raise ValueError(f"unu: tarif_id {tarif_id} not in catalogue")
        return float(tariff["price"])

    async def _get_tariffs(self) -> dict[str, dict[str, Any]]:
        if (
            self._tariffs_cache is None
            or time.monotonic() - self._tariffs_cache_at > _TARIFFS_TTL_SECONDS
        ):
            data = await self._post("get_tariffs", {})
            raw = data.get("tariffs") or []
            flat: dict[str, dict[str, Any]] = {}
            for t in raw:
                if isinstance(t, dict) and "id" in t and "price" in t:
                    flat[str(t["id"])] = t
            self._tariffs_cache = flat
            self._tariffs_cache_at = time.monotonic()
        return self._tariffs_cache

    @staticmethod
    def _extract_evidence(messages: Any) -> str | None:
        """Pull the worker's evidence text from UNU's `messages[]` payload.

        The contract is loose (text snippet, URL, screenshot id, ...); the
        verifier consumes the raw blob. None when the report carries no message.
        """
        if not isinstance(messages, list) or not messages:
            return None
        first = messages[0]
        if isinstance(first, dict):
            text = first.get("text") or first.get("message") or first.get("body")
            if text:
                return str(text)
        if isinstance(first, str):
            return first
        return None
