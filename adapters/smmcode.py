"""Real adapter for the smmcode.shop reseller API.

API form: POST per-method endpoints, JSON responses, auth via `api_token` body param.
Docs reference: `docs/api/smmcode.md`.

Notable behaviours:

- **Estimate-first cost check** (HIGH-c invariant from Day 1 audit). Before placing,
  the adapter fetches the services catalogue (cached for `_SERVICES_TTL_SECONDS`),
  finds the requested `service_id`, computes `cost = price * quantity`, and refuses
  if it exceeds `spec.max_cost`. The `/create_order` endpoint is NOT called when the
  cap is exceeded - tests assert this.
- **Status mapping.** Raw `status_id` from the API is mapped to our normalized
  `'in_progress' | 'completed' | 'failed'` per the table in `docs/api/smmcode.md`.
- **No client order id support.** The reseller API does not accept a client-side
  identifier; `client_order_uuid` (C1) lives only in our SQLite. Identity on the
  exchange side = the returned `order_id`.
"""

from __future__ import annotations

import time
from typing import Any

import httpx

from adapters.base import Capability, PanelAdapter, ServiceOption, keywords_for_scenario
from models import OrderSpec, Scenario

_BASE_URL = "https://smmcode.shop/api/reseller"
_SERVICES_TTL_SECONDS = 300.0

# Raw `status_id` -> normalized status. Source: docs/api/smmcode.md.
_STATUS_MAP: dict[int, str] = {
    1: "in_progress",  # В обработке
    2: "failed",  # Не оплачено
    3: "completed",  # Выполнено
    4: "completed",  # Частично выполнено (raw retained in raw_exchange_status)
    5: "failed",  # Отменено
    6: "failed",  # Ошибка
    7: "in_progress",  # Выполняется
    8: "failed",  # Возврат платежа
    9: "in_progress",  # Неизвестно - don't conclude
    10: "in_progress",  # В очереди
}


class SmmcodeAdapter(PanelAdapter):
    """smmcode.shop reseller adapter."""

    name = "smmcode"

    def __init__(self, api_token: str, http_client: httpx.AsyncClient) -> None:
        if not api_token:
            raise ValueError("smmcode api_token is required (set SMMCODE_API_KEY in .env)")
        self._api_token = api_token
        self._client = http_client
        self._services_cache: dict[str, dict[str, Any]] | None = None
        self._services_cache_at: float = 0.0

    def capabilities(self) -> set[Capability]:
        return {
            Capability.CREATE_ORDER,
            Capability.GET_ORDER_STATUS,
            Capability.GET_BALANCE,
            # NB: reseller API does not accept client-side order ids.
        }

    async def get_balance(self) -> float:
        data = await self._post("balance", {})
        return float(data["balance"])

    async def create_order(self, spec: OrderSpec, client_order_uuid: str) -> tuple[str, float]:
        if not spec.service_id:
            raise ValueError("smmcode requires spec.service_id (which catalogue service to order)")
        # Estimate-first: fetch the services catalogue (cached), compute cost,
        # enforce max_cost BEFORE we touch /create_order.
        price_per_unit = await self._service_price(spec.service_id)
        cost = price_per_unit * spec.quantity
        if cost > spec.max_cost:
            raise ValueError(f"computed cost {cost:.2f} exceeds spec.max_cost {spec.max_cost:.2f}")
        data = await self._post(
            "create_order",
            {
                "service_id": str(spec.service_id),
                "count": str(spec.quantity),
                "link": spec.target,
            },
        )
        external_id = str(data["order_id"])
        return external_id, cost

    async def get_order_status(self, external_order_id: str) -> str:
        data = await self._post("order_status", {"order_id": str(external_order_id)})
        order = data.get("order")
        if not isinstance(order, dict):
            return "failed"
        try:
            status_id = int(order.get("status_id", 0))
        except (TypeError, ValueError):
            return "failed"
        return _STATUS_MAP.get(status_id, "failed")

    # --- internal helpers --------------------------------------------------

    async def _post(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        url = f"{_BASE_URL}/{method}"
        body = {"api_token": self._api_token, **params}
        response = await self._client.post(url, data=body)
        response.raise_for_status()
        payload = response.json()
        status_code = payload.get("status")
        if status_code != 200:
            # Avoid leaking the api_token in the error string.
            safe_payload = {k: v for k, v in payload.items() if k != "api_token"}
            raise RuntimeError(f"smmcode {method} returned non-200 status: {safe_payload!r}")
        return payload

    async def list_services_for_scenario(
        self,
        scenario: Scenario,
        limit: int = 8,
    ) -> list[ServiceOption]:
        """Filter the cached `/services` catalogue by name keywords matching the
        scenario. Sorted by price-per-unit ascending so the cheapest choice is
        on top (typical demo flow wants the cheapest viable option).
        """
        services = await self._get_services()
        keywords = keywords_for_scenario(scenario)
        if not keywords:
            return []
        candidates: list[ServiceOption] = []
        for svc in services.values():
            name = str(svc.get("name", ""))
            haystack = name.lower()
            if not any(kw in haystack for kw in keywords):
                continue
            try:
                price = float(svc.get("price", 0))
            except (TypeError, ValueError):
                price = 0.0
            if price <= 0:
                continue  # skip entries we can't cost-cap reliably
            try:
                min_q = int(svc.get("min", 0)) or None
            except (TypeError, ValueError):
                min_q = None
            try:
                max_q = int(svc.get("max", 0)) or None
            except (TypeError, ValueError):
                max_q = None
            candidates.append(
                ServiceOption(
                    service_id=str(svc.get("service_id", svc.get("id", ""))),
                    name=name,
                    price_per_unit=price,
                    min_quantity=min_q,
                    max_quantity=max_q,
                )
            )
        candidates.sort(key=lambda s: (s.price_per_unit or 0.0, s.name))
        return candidates[:limit]

    async def _service_price(self, service_id: str) -> float:
        services = await self._get_services()
        service = services.get(str(service_id))
        if service is None:
            raise ValueError(f"smmcode: service_id {service_id} not in catalogue")
        return float(service["price"])

    async def _get_services(self) -> dict[str, dict[str, Any]]:
        if (
            self._services_cache is None
            or time.monotonic() - self._services_cache_at > _SERVICES_TTL_SECONDS
        ):
            data = await self._post("services", {})
            raw = data.get("services") or {}
            flat: dict[str, dict[str, Any]] = {}
            # Catalogue nesting: platform -> action -> service_id_key -> service_dict
            for platform_obj in raw.values():
                if not isinstance(platform_obj, dict):
                    continue
                for action_obj in platform_obj.values():
                    if not isinstance(action_obj, dict):
                        continue
                    for service_obj in action_obj.values():
                        if isinstance(service_obj, dict) and "service_id" in service_obj:
                            flat[str(service_obj["service_id"])] = service_obj
            self._services_cache = flat
            self._services_cache_at = time.monotonic()
        return self._services_cache
