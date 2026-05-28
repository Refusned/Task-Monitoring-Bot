"""Real adapter for the prskill.ru SMM-panel reseller API.

API form: POST `https://prskill.ru/api/v2` with body params `key=<api_key>` +
`action=<balance|services|add|status>` (the "Perfect Panel" reseller dispatch
shape, widely used across Russian SMM panels). Responses JSON.

Status: NOT yet exercised against a live key — PRSKILL_API_KEY hasn't been
provisioned at this stage. The adapter ships with the Perfect-Panel shape (this
is what the doc summary in CLAUDE.md describes: "reseller API, key+action") and
will be smoke-tested when credentials land. Capabilities are declared
honestly per Day 4 plan: panel scope only, no submission cycle.

Notable behaviours mirror smmcode:
- Estimate-first cost check against the cached `services` catalogue.
- Conservative status map (anything we don't recognize -> failed).
- The reseller API does NOT accept client-side order ids.
"""

from __future__ import annotations

import time
from typing import Any

import httpx

from adapters.base import Capability, PanelAdapter, quote_from_mock
from models import OrderSpec, Quote, SourcePlatform, TaskType, TopupInfo

_BASE_URL = "https://prskill.ru/api"  # docs at https://prskill.ru/api confirm: no /v2 suffix
_SERVICES_TTL_SECONDS = 60.0

# Raw `status` string (lowercase) -> normalized status. The Perfect Panel
# convention; conservative ("unknown -> failed") so we never silently finalize.
_STATUS_MAP: dict[str, str] = {
    "pending": "in_progress",
    "in progress": "in_progress",
    "in_progress": "in_progress",
    "processing": "in_progress",
    "completed": "completed",
    "partial": "completed",
    "canceled": "failed",
    "cancelled": "failed",
    "refunded": "failed",
    "error": "failed",
}


class PrSkillAdapter(PanelAdapter):
    """prskill.ru reseller adapter (Perfect Panel form: key + action)."""

    name = "prskill"

    def __init__(self, api_key: str, http_client: httpx.AsyncClient) -> None:
        if not api_key:
            raise ValueError("prskill api_key is required (set PRSKILL_API_KEY in .env)")
        self._api_key = api_key
        self._client = http_client
        self._services_cache: dict[str, dict[str, Any]] | None = None
        self._services_cache_at: float = 0.0

    def capabilities(self) -> set[Capability]:
        return {
            Capability.CREATE_ORDER,
            Capability.GET_ORDER_STATUS,
            # GET_BALANCE intentionally NOT here: prskill public API doesn't expose it.
            Capability.GET_QUOTE,
            Capability.GET_TOPUP_INFO,
            # No client-side ids on reseller APIs.
        }

    async def get_quote(
        self, metric: TaskType, platform: SourcePlatform, quantity: int
    ) -> Quote | None:
        """Mock-only for MVP — prskill credentials weren't provisioned at the time
        of writing. Falls back to config mock_quotes_csv. To go real: hardcode a
        service_id map (same pattern as smmcode) and call _service_price."""
        from config import get_settings

        return quote_from_mock(
            self.name, metric, platform, quantity, get_settings().mock_quotes_csv,
            confidence=0.4,
        )

    async def get_topup_info(self) -> TopupInfo:
        return TopupInfo(
            exchange=self.name,
            topup_url="https://prskill.ru/payments",
            min_amount=10.0,
            currency="RUB",
            payment_methods=["card", "yoomoney", "qiwi", "usdt"],
            notes="Кошелёк prskill, не наш. Пополни через личный кабинет.",
        )

    async def get_balance(self) -> float | None:
        # prskill's public API has no `balance` method — confirmed against
        # docs at https://prskill.ru/api on 2026-05-28 (only `services`,
        # `add`, `status` exist). Return None so the dashboard renders
        # "нет API" instead of a fake "устарел" badge.
        return None

    async def create_order(self, spec: OrderSpec, client_order_uuid: str) -> tuple[str, float]:
        if not spec.service_id:
            raise ValueError("prskill requires spec.service_id (service from /services)")
        price_per_unit = await self._service_price(spec.service_id)
        cost = price_per_unit * spec.quantity
        if cost > spec.max_cost:
            raise ValueError(f"computed cost {cost:.2f} exceeds spec.max_cost {spec.max_cost:.2f}")
        data = await self._post(
            {
                "action": "add",
                "service": str(spec.service_id),
                "link": spec.target,
                "quantity": str(spec.quantity),
            }
        )
        external_id = str(data["order"])
        return external_id, cost

    async def get_order_status(self, external_order_id: str) -> str:
        data = await self._post({"action": "status", "order": str(external_order_id)})
        status_raw = data.get("status") if isinstance(data, dict) else None
        if not isinstance(status_raw, str):
            return "failed"
        return _STATUS_MAP.get(status_raw.strip().lower(), "failed")

    # --- internal helpers --------------------------------------------------

    async def _post(self, params: dict[str, Any]) -> Any:
        """Perfect Panel returns dicts for most actions and a bare list for
        `services`; we surface both shapes and let callers branch.
        """
        body = {"key": self._api_key, **params}
        response = await self._client.post(_BASE_URL, data=body)
        response.raise_for_status()
        payload = response.json()
        if isinstance(payload, dict) and "error" in payload:
            safe = {k: v for k, v in payload.items() if k != "key"}
            raise RuntimeError(f"prskill {params.get('action')!r} returned error: {safe!r}")
        return payload

    async def _service_price(self, service_id: str) -> float:
        services = await self._get_services()
        service = services.get(str(service_id))
        if service is None:
            raise ValueError(f"prskill: service_id {service_id} not in catalogue")
        # Perfect Panel exposes per-1000 rates; price-per-unit = rate / 1000.
        rate = float(service["rate"])
        return rate / 1000.0

    async def _get_services(self) -> dict[str, dict[str, Any]]:
        if (
            self._services_cache is None
            or time.monotonic() - self._services_cache_at > _SERVICES_TTL_SECONDS
        ):
            data = await self._post({"action": "services"})
            # Perfect Panel `services` returns a bare JSON list; tolerate dict-
            # wrapped variants in case a particular panel echoes differently.
            if isinstance(data, list):
                raw = data
            elif isinstance(data, dict):
                raw = data.get("services") or data.get("data") or []
            else:
                raw = []
            flat: dict[str, dict[str, Any]] = {}
            for service_obj in raw:
                if isinstance(service_obj, dict) and "service" in service_obj:
                    flat[str(service_obj["service"])] = service_obj
            self._services_cache = flat
            self._services_cache_at = time.monotonic()
        return self._services_cache
