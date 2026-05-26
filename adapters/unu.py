"""Real adapter for the unu.im microtask-exchange API (v1).

API form: POST form-encoded per-action endpoint, JSON responses,
auth via `api_key` body param + `action`.
Docs reference: `docs/api/unu.md`.

Notable behaviours:

- **Estimate-first cost check** (HIGH-c invariant). Before placing, the adapter fetches
  tariffs (cached), computes `cost = price * quantity`, and refuses if `max_cost` exceeded.
- **Status mapping.** Raw numeric status codes are mapped per the tables in `docs/api/unu.md`.
- **No client order id support.** The native v1 API does not accept client-side identifiers;
  `client_order_uuid` (C1) lives only in our SQLite.
- **add_task_start** is used for atomic create + limit set (preferred over `add_task` +
  `task_limit_add` per docs).
"""

from __future__ import annotations

import time
from typing import Any

import httpx

from adapters.base import Capability, ServiceOption, TaskExchangeAdapter, keywords_for_scenario
from models import ExternalSubmission, OrderSpec, Scenario

_BASE_URL = "https://unu.im/api"
_SERVICES_TTL_SECONDS = 300.0


def _extract_unu_evidence(messages: Any) -> str | None:
    """Pull worker evidence text out of UNU's `messages` payload.

    UNU returns messages as a list of dicts like `{"text": "...", ...}`. We
    join the text fields into a single human-readable string. Returns `None`
    if there are no messages — the verifier treats that as "no evidence",
    which routes to NEEDS_HUMAN_REVIEW (safer than auto-pass).
    """
    if not isinstance(messages, list) or not messages:
        return None
    parts: list[str] = []
    for m in messages:
        if isinstance(m, dict):
            text = m.get("text") or m.get("message") or m.get("body")
            if text:
                parts.append(str(text))
        elif isinstance(m, str):
            parts.append(m)
    return "\n".join(parts) if parts else None


# Task status (from get_tasks) raw -> normalized order status
_TASK_STATUS_MAP: dict[int, str] = {
    1: "in_progress",  # New, needs pay (limit=0) — should not happen with add_task_start
    2: "completed",  # Limit reached (all executions done)
    3: "failed",  # Stopped
    4: "in_progress",  # Active
    5: "failed",  # Rejected by moderator
    6: "in_progress",  # On moderation
}

# Report status (from get_reports) raw -> normalized submission status
_REPORT_STATUS_MAP: dict[int, str] = {
    1: "new",  # In work
    2: "awaiting_admin",  # On review
    3: "rework_requested",  # On revision (we sent back)
    6: "accepted",  # Paid
}


class UnuAdapter(TaskExchangeAdapter):
    """unu.im microtask-exchange adapter."""

    name = "unu"

    def __init__(self, api_key: str, http_client: httpx.AsyncClient) -> None:
        if not api_key:
            raise ValueError("unu api_key is required (set UNU_API_KEY in .env)")
        self._api_key = api_key
        self._client = http_client
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
        }

    async def get_balance(self) -> float:
        data = await self._post({"action": "get_balance"})
        return float(data.get("balance", 0.0))

    async def create_order(self, spec: OrderSpec, client_order_uuid: str) -> tuple[str, float]:
        # Estimate-first: fetch tariffs, compute cost, enforce max_cost.
        price_per_unit = await self._tariff_price(spec.service_id or "1")
        cost = price_per_unit * spec.quantity
        if cost > spec.max_cost:
            raise ValueError(f"computed cost {cost:.2f} exceeds spec.max_cost {spec.max_cost:.2f}")

        # For MVP: simple mapping. We pick a default folder_id=1; in production
        # the orchestrator should pass folder_id via spec.service_id or config.
        # We use add_task_start to create and fund the task atomically.
        body: dict[str, Any] = {
            "action": "add_task_start",
            "name": spec.scenario,
            "descr": f"Order via bot for {spec.target}",
            "need_for_report": 1,
            "price": str(price_per_unit),
            "tarif_id": str(spec.service_id or "1"),
            "folder_id": "1",
            "add_to_limit": str(spec.quantity),
            "link": spec.target,
        }
        data = await self._post(body)
        external_id = str(data.get("task_id"))
        if not external_id:
            raise RuntimeError(f"unu add_task_start returned no task_id: {data!r}")
        return external_id, cost

    async def get_order_status(self, external_order_id: str) -> str:
        data = await self._post({"action": "get_tasks", "task_id": str(external_order_id)})
        tasks = data.get("tasks")
        if not isinstance(tasks, list) or not tasks:
            return "failed"
        task = tasks[0]
        raw_status = int(task.get("status", 0))
        return _TASK_STATUS_MAP.get(raw_status, "failed")

    async def list_submissions(self, external_order_id: str) -> list[ExternalSubmission]:
        data = await self._post({"action": "get_reports", "task_id": str(external_order_id)})
        reports = data.get("reports")
        if not isinstance(reports, list):
            return []
        results: list[ExternalSubmission] = []
        for report in reports:
            if not isinstance(report, dict):
                continue
            raw_status = int(report.get("status", 0))
            # Only include reports that are still pending a decision (status 1 or 2).
            if raw_status not in (1, 2):
                continue
            results.append(
                ExternalSubmission(
                    external_submission_id=str(report.get("id")),
                    executor_hint=str(report.get("worker_id")) if report.get("worker_id") else None,
                    evidence=_extract_unu_evidence(report.get("messages")),
                )
            )
        return results

    async def accept_submission(self, external_order_id: str, external_submission_id: str) -> None:
        await self._post(
            {
                "action": "approve_report",
                "report_id": str(external_submission_id),
            }
        )

    async def reject_submission(
        self, external_order_id: str, external_submission_id: str, reason: str
    ) -> None:
        await self._post(
            {
                "action": "reject_report",
                "report_id": str(external_submission_id),
                "comment": reason,
                "reject_type": "1",  # to revision
            }
        )

    async def list_services_for_scenario(
        self,
        scenario: Scenario,
        limit: int = 8,
    ) -> list[ServiceOption]:
        """Pick UNU tariffs whose name matches scenario keywords. UNU uses
        `min_price_rub` as the price floor; we treat it as the assumed
        per-unit price for the picker label.
        """
        tariffs = await self._get_tariffs()
        keywords = keywords_for_scenario(scenario)
        if not keywords:
            return []
        candidates: list[ServiceOption] = []
        for tariff in tariffs.values():
            name = str(tariff.get("name", ""))
            haystack = name.lower()
            if not any(kw in haystack for kw in keywords):
                continue
            price = None
            for key in ("min_price_rub", "price", "cost"):
                raw = tariff.get(key)
                if raw is None:
                    continue
                try:
                    val = float(raw)
                except (TypeError, ValueError):
                    continue
                if val > 0:
                    price = val
                    break
            if price is None:
                continue  # no usable price → skip (money-safety)
            candidates.append(
                ServiceOption(
                    service_id=str(tariff.get("id", "")),
                    name=name,
                    price_per_unit=price,
                )
            )
        candidates.sort(key=lambda s: (s.price_per_unit or 0.0, s.name))
        return candidates[:limit]

    # --- internal helpers --------------------------------------------------

    async def _post(self, params: dict[str, Any]) -> dict[str, Any]:
        url = _BASE_URL
        body = {"api_key": self._api_key, **params}
        response = await self._client.post(url, data=body)
        response.raise_for_status()
        payload = response.json()
        # UNU envelope: {"success": int, "errors": text, ...payload}
        if payload.get("success") not in (1, "1", True):
            errors = payload.get("errors", payload.get("error", "unknown error"))
            safe = {k: v for k, v in payload.items() if k != "api_key"}
            raise RuntimeError(f"unu error ({errors!r}): {safe!r}")
        return payload

    async def _tariff_price(self, tariff_id: str) -> float:
        """Estimate-first price-per-execution for the given tariff.

        UNU exposes tariffs with a `min_price_rub` field (the floor — minimum
        accepted price per unit). For estimate-first cost capping we treat the
        floor as the assumed price: if even the floor times quantity exceeds
        spec.max_cost, placement is refused. This prevents the worst-case
        (CRITICAL money-safety): a missing/renamed field returning 0.0, which
        would silently bypass the cost cap and let the bot place an order at
        whatever price UNU assigns server-side.

        Probe order (first non-zero wins):
          1. `min_price_rub` — current UNU field (verified live, 2026-05).
          2. `price`         — historic field, kept for compat.
          3. `cost`          — historic field, kept for compat.

        If none of these is positive, we raise loudly — better a refused order
        than a $$-leak.
        """
        tariffs = await self._get_tariffs()
        tariff = tariffs.get(str(tariff_id))
        if tariff is None:
            raise ValueError(f"unu: tariff_id {tariff_id} not found")
        for key in ("min_price_rub", "price", "cost"):
            raw = tariff.get(key)
            if raw is None:
                continue
            try:
                price = float(raw)
            except (TypeError, ValueError):
                continue
            if price > 0:
                return price
        raise ValueError(
            f"unu: tariff_id {tariff_id} has no usable price field "
            f"(tried min_price_rub/price/cost); refusing to place an order "
            f"without a real price — UNU API contract may have changed"
        )

    async def _get_tariffs(self) -> dict[str, dict[str, Any]]:
        if (
            self._tariffs_cache is None
            or time.monotonic() - self._tariffs_cache_at > _SERVICES_TTL_SECONDS
        ):
            data = await self._post({"action": "get_tariffs"})
            raw = data.get("tariffs") or data.get("data") or []
            flat: dict[str, dict[str, Any]] = {}
            if isinstance(raw, list):
                for t in raw:
                    if isinstance(t, dict) and "id" in t:
                        flat[str(t["id"])] = t
            elif isinstance(raw, dict):
                for k, v in raw.items():
                    if isinstance(v, dict):
                        flat[str(k)] = v
            self._tariffs_cache = flat
            self._tariffs_cache_at = time.monotonic()
        return self._tariffs_cache
