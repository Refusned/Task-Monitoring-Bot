"""Real adapter for the ipgold.ru advertiser API.

API form: POST form-encoded to `https://ipgold.ru/api/v2`, JSON responses.
Auth: body param `api_key`. Response envelope:

    {"status": "OK", ...payload}                 # success
    {"status": "BAD", "errors": [{"message": ..., "code": ...}, ...]}   # error

This matches the Perfect Panel / SMM-panel reseller shape seen on other Russian
exchanges (prskill, smmcode), with one important difference: IPGold's `/api/v2`
returns `"Access denied" (403)` until the account explicitly activates API
access (and, on some plans, whitelists the caller's IP). The adapter surfaces
that error verbatim rather than masking it — the bot operator gets the real
reason in `/balance` and `/health`.

Notable behaviours:

- **Estimate-first cost check.** Before placing, the adapter fetches the
  catalogue (`action=services`), finds the requested `service_id`, computes
  `cost = rate * quantity`, and refuses if it exceeds `spec.max_cost`. The
  `add` action is NOT called when the cap is exceeded.
- **Status mapping.** Raw Perfect-Panel style status strings are mapped to our
  normalised `'in_progress' | 'completed' | 'failed'`.
- **Submission lifecycle is panel-style.** IPGold processes orders centrally
  (no per-submission accept/reject); like smmcode/prskill, accept/reject calls
  are NOT exposed via capabilities.

Docs reference: `docs/api/ipgold.md`.
"""

from __future__ import annotations

import time
from typing import Any

import httpx

from adapters.base import Capability, PanelAdapter, ServiceOption, keywords_for_scenario
from models import OrderSpec, Scenario

_BASE_URL = "https://ipgold.ru/api/v2"
_SERVICES_TTL_SECONDS = 300.0

# Raw status string -> normalised. Matches the Perfect Panel convention; the
# table is conservative — anything we don't recognise maps to `failed` so we
# never silently auto-finalise on an unknown state.
_STATUS_MAP: dict[str, str] = {
    "pending": "in_progress",
    "in progress": "in_progress",
    "in_progress": "in_progress",
    "processing": "in_progress",
    "started": "in_progress",
    "completed": "completed",
    "complete": "completed",
    "partial": "completed",
    "canceled": "failed",
    "cancelled": "failed",
    "refunded": "failed",
    "error": "failed",
    "failed": "failed",
}


class IpgoldAdapter(PanelAdapter):
    """ipgold.ru advertiser API adapter (Perfect Panel form: api_key + action)."""

    name = "ipgold"

    def __init__(self, api_key: str, http_client: httpx.AsyncClient) -> None:
        if not api_key:
            raise ValueError("ipgold api_key is required (set IPGOLD_API_KEY in .env)")
        self._api_key = api_key
        self._client = http_client
        self._services_cache: dict[str, dict[str, Any]] | None = None
        self._services_cache_at: float = 0.0

    def capabilities(self) -> set[Capability]:
        return {
            Capability.CREATE_ORDER,
            Capability.GET_ORDER_STATUS,
            Capability.GET_BALANCE,
            # NB: ipgold is panel-style — no per-submission accept/reject.
        }

    async def get_balance(self) -> float:
        data = await self._post({"action": "balance"})
        # Tolerate both `{balance: float}` and `{balance: str}` shapes; the
        # advertiser API returns numbers as strings on some endpoints.
        raw = data.get("balance")
        if raw is None:
            raise RuntimeError(f"ipgold balance: no 'balance' field in payload: {data!r}")
        return float(raw)

    async def create_order(self, spec: OrderSpec, client_order_uuid: str) -> tuple[str, float]:
        if not spec.service_id:
            raise ValueError("ipgold requires spec.service_id (catalogue id from /services)")
        price_per_unit = await self._service_rate(spec.service_id)
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
        external_id = data.get("order") or data.get("order_id") or data.get("id")
        if external_id is None:
            raise RuntimeError(f"ipgold add: no order id in payload: {data!r}")
        return str(external_id), cost

    async def get_order_status(self, external_order_id: str) -> str:
        data = await self._post({"action": "status", "order": str(external_order_id)})
        status_raw = data.get("status") if isinstance(data, dict) else None
        if not isinstance(status_raw, str):
            return "failed"
        return _STATUS_MAP.get(status_raw.strip().lower(), "failed")

    async def list_services_for_scenario(
        self,
        scenario: Scenario,
        limit: int = 8,
    ) -> list[ServiceOption]:
        """Filter cached `/services` by name keywords for the chosen scenario.
        Sorted by `rate` ascending (cheapest first).
        """
        services = await self._get_services()
        keywords = keywords_for_scenario(scenario)
        if not keywords:
            return []
        candidates: list[ServiceOption] = []
        for svc in services.values():
            name = str(svc.get("name", ""))
            if not any(kw in name.lower() for kw in keywords):
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

    # --- internal helpers --------------------------------------------------

    async def _post(self, params: dict[str, Any]) -> Any:
        """Perfect-Panel-style POST. Returns the parsed payload OR raises
        `RuntimeError` with the API's own message — the api_key is stripped
        from any error echo so it never leaks into bot replies or logs.

        IPGold's error envelope is `{"status": "BAD", "errors": [{message, code}]}`.
        We surface the first error message verbatim so the operator sees
        exactly why the API rejected the call (typical reasons: "Access
        denied" 403 when API isn't activated; "Action is not specified" 4).
        """
        body = {"api_key": self._api_key, **params}
        response = await self._client.post(_BASE_URL, data=body)
        response.raise_for_status()
        payload = response.json()
        if isinstance(payload, dict) and payload.get("status") == "BAD":
            errors = payload.get("errors") or []
            msgs = ", ".join(
                f"{e.get('message', '?')} (code {e.get('code', '?')})"
                for e in errors
                if isinstance(e, dict)
            )
            raise RuntimeError(f"ipgold {params.get('action')!r} rejected: {msgs or payload!r}")
        return payload

    async def _service_rate(self, service_id: str) -> float:
        """Per-unit price. Perfect-Panel `rate` is per 1000 units; we convert."""
        services = await self._get_services()
        service = services.get(str(service_id))
        if service is None:
            raise ValueError(f"ipgold: service_id {service_id} not in catalogue")
        try:
            rate = float(service["rate"])
        except (TypeError, ValueError, KeyError) as exc:
            raise ValueError(
                f"ipgold: service_id {service_id} has no usable 'rate' field: {service!r}"
            ) from exc
        return rate / 1000.0

    async def _get_services(self) -> dict[str, dict[str, Any]]:
        if (
            self._services_cache is None
            or time.monotonic() - self._services_cache_at > _SERVICES_TTL_SECONDS
        ):
            data = await self._post({"action": "services"})
            if isinstance(data, list):
                raw = data
            elif isinstance(data, dict):
                raw = data.get("services") or data.get("data") or []
            else:
                raw = []
            flat: dict[str, dict[str, Any]] = {}
            for svc in raw:
                if isinstance(svc, dict):
                    sid = svc.get("service") or svc.get("id")
                    if sid is not None:
                        flat[str(sid)] = svc
            self._services_cache = flat
            self._services_cache_at = time.monotonic()
        return self._services_cache
