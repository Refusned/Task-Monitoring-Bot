"""Real adapter for the prskill.ru reseller API.

API form: POST form-encoded per-method endpoint, JSON responses, auth via `key`+`action`.
Docs reference: `docs/api/prskill.md`.

Notable behaviours:

- **Estimate-first cost check** (HIGH-c invariant). Before placing, the adapter fetches
  the services catalogue (cached), finds `service_id`, computes `cost = rate * quantity`,
  and refuses if it exceeds `spec.max_cost`. The `/add` endpoint is NOT called when the
  cap is exceeded - tests assert this.
- **Status mapping.** Raw status strings from the API are mapped to our normalized
  `'in_progress' | 'completed' | 'failed'` per the table in `docs/api/prskill.md`.
- **No client order id support.** The reseller API does not accept a client-side
  identifier; `client_order_uuid` (C1) lives only in our SQLite.
- **No balance endpoint** is documented on the public API page. `get_balance` returns
  `0.0` and logs a warning so the orchestrator can continue; real balance checks should
  be done via the web cabinet or by summing active order costs against a known deposit.
"""

from __future__ import annotations

import time
from typing import Any

import httpx

from adapters.base import Capability, PanelAdapter, ServiceOption, keywords_for_scenario
from models import OrderSpec, Scenario

_BASE_URL = "https://prskill.ru/api"
_SERVICES_TTL_SECONDS = 300.0

# Raw string status -> normalized status. Source: docs/api/prskill.md.
_STATUS_MAP: dict[str, str] = {
    "process": "in_progress",
    "success": "completed",
    "cancel": "failed",
    "piece": "completed",
    "fail": "failed",
    "check": "in_progress",
}


class PrskillAdapter(PanelAdapter):
    """prskill.ru reseller adapter."""

    name = "prskill"

    def __init__(self, api_key: str, http_client: httpx.AsyncClient) -> None:
        if not api_key:
            raise ValueError("prskill api_key is required (set PRSKILL_API_KEY in .env)")
        self._api_key = api_key
        self._client = http_client
        self._services_cache: dict[str, dict[str, Any]] | None = None
        self._services_cache_at: float = 0.0

    def capabilities(self) -> set[Capability]:
        # NB: GET_BALANCE intentionally absent — prskill's public API has no
        # `balance` action ("The selected action is invalid" on POST). The bot's
        # /balance reply will honestly show "баланс не отдаётся API" rather than
        # a fake 0.00. Track balance via the web cabinet.
        return {
            Capability.CREATE_ORDER,
            Capability.GET_ORDER_STATUS,
        }

    async def get_balance(self) -> float:
        raise NotImplementedError(
            "prskill API does not expose a balance endpoint; track balance "
            "via the web cabinet"
        )

    async def create_order(self, spec: OrderSpec, client_order_uuid: str) -> tuple[str, float]:
        if not spec.service_id:
            raise ValueError("prskill requires spec.service_id (which catalogue service to order)")
        # Estimate-first: fetch catalogue, compute cost, enforce max_cost BEFORE /add.
        price_per_unit = await self._service_rate(spec.service_id)
        cost = price_per_unit * spec.quantity
        if cost > spec.max_cost:
            raise ValueError(f"computed cost {cost:.2f} exceeds spec.max_cost {spec.max_cost:.2f}")

        # Build the extra fields required by the service (e.g. service_url).
        # The catalogue entry tells us which fields are expected.
        service = await self._get_service(spec.service_id)
        fields = service.get("fields") or []
        params: dict[str, Any] = {
            "key": self._api_key,
            "action": "add",
            "service": str(spec.service_id),
            "quantity": str(spec.quantity),
        }
        # The target URL goes into the field named in the service descriptor.
        for field in fields:
            field_name = field.get("name", "service_url")
            if field.get("required", False) or field_name == "service_url":
                params[field_name] = spec.target
        # If no fields descriptor, default to service_url.
        if "service_url" not in params:
            params["service_url"] = spec.target

        data = await self._post(params)
        external_id = str(data["order"])
        return external_id, cost

    async def get_order_status(self, external_order_id: str) -> str:
        data = await self._post(
            {
                "key": self._api_key,
                "action": "status",
                "order": str(external_order_id),
            }
        )
        orders = data.get("orders")
        if not isinstance(orders, dict):
            return "failed"
        raw_status = orders.get(str(external_order_id))
        if not isinstance(raw_status, str):
            return "failed"
        return _STATUS_MAP.get(raw_status, "failed")

    # --- internal helpers --------------------------------------------------

    async def _post(self, params: dict[str, Any]) -> dict[str, Any]:
        response = await self._client.post(_BASE_URL, data=params)
        response.raise_for_status()
        try:
            payload = response.json()
        except Exception as exc:
            # PRSkill sometimes returns plain text errors.
            raise RuntimeError(f"prskill returned error string: {response.text!r}") from exc
        # PRSkill does not wrap responses in a status envelope like smmcode.
        # Errors are returned as strings or with "error" key.
        if isinstance(payload, str):
            raise RuntimeError(f"prskill returned error string: {payload!r}")
        if isinstance(payload, dict) and "error" in payload:
            raise RuntimeError(f"prskill error: {payload['error']!r}")
        return payload

    async def list_services_for_scenario(
        self,
        scenario: Scenario,
        limit: int = 8,
    ) -> list[ServiceOption]:
        """Filter cached `/services` by name keywords for the chosen scenario.
        Sorted by price-per-unit (here `rate`) ascending.
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
                rate = float(svc.get("rate", 0))
            except (TypeError, ValueError):
                rate = 0.0
            if rate <= 0:
                continue
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
                    service_id=str(svc.get("service", svc.get("id", ""))),
                    name=name,
                    price_per_unit=rate,
                    min_quantity=min_q,
                    max_quantity=max_q,
                )
            )
        candidates.sort(key=lambda s: (s.price_per_unit or 0.0, s.name))
        return candidates[:limit]

    async def _service_rate(self, service_id: str) -> float:
        services = await self._get_services()
        service = services.get(str(service_id))
        if service is None:
            raise ValueError(f"prskill: service_id {service_id} not in catalogue")
        return float(service["rate"])

    async def _get_service(self, service_id: str) -> dict[str, Any]:
        services = await self._get_services()
        service = services.get(str(service_id))
        if service is None:
            raise ValueError(f"prskill: service_id {service_id} not in catalogue")
        return service

    async def _get_services(self) -> dict[str, dict[str, Any]]:
        if (
            self._services_cache is None
            or time.monotonic() - self._services_cache_at > _SERVICES_TTL_SECONDS
        ):
            data = await self._post({"key": self._api_key, "action": "services"})
            raw = data if isinstance(data, list) else data.get("services", [])
            flat: dict[str, dict[str, Any]] = {}
            for svc in raw:
                if isinstance(svc, dict) and "service" in svc:
                    flat[str(svc["service"])] = svc
            self._services_cache = flat
            self._services_cache_at = time.monotonic()
        return self._services_cache
