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

from adapters.base import Capability, PanelAdapter, quote_from_mock
from models import OrderSpec, Quote, SourcePlatform, TaskType, TopupInfo

_BASE_URL = "https://smmcode.shop/api/reseller"
_SERVICES_TTL_SECONDS = 60.0

# Real service-id catalog for known (platform, metric) tuples. Picked from the
# live smmcode.shop catalogue (cheapest stable option per category as of demo,
# discovered 2026-05-27 via POST /api/reseller/services). When a tuple is
# missing here, get_quote() falls back to mock_quotes_csv from settings — the
# architectural slot stays, the demo keeps working.
_SMMCODE_SERVICE_ID_MAP: dict[tuple[SourcePlatform, TaskType], str] = {
    # YouTube — original three.
    (SourcePlatform.YOUTUBE, TaskType.LIKES): "9824",         # ~₽0.97/шт
    (SourcePlatform.YOUTUBE, TaskType.VIEWS): "5744",         # ~₽0.25/шт
    (SourcePlatform.YOUTUBE, TaskType.SUBSCRIBES): "10391",   # ~₽6.28/шт
    # VKontakte.
    (SourcePlatform.VK, TaskType.LIKES): "7042",              # ~₽0.14/шт
    (SourcePlatform.VK, TaskType.SUBSCRIBES): "43",           # ~₽0.89/шт (группа/паблик)
    (SourcePlatform.VK, TaskType.VIEWS): "74",                # ~₽0.003/шт
    # Telegram.
    (SourcePlatform.TELEGRAM, TaskType.SUBSCRIBES): "10077",  # ~₽0.05/шт
    (SourcePlatform.TELEGRAM, TaskType.VIEWS): "9918",        # ~₽0.007/шт
    # X (Twitter).
    (SourcePlatform.X, TaskType.LIKES): "2998",               # ~₽0.36/шт
    (SourcePlatform.X, TaskType.SUBSCRIBES): "10082",         # ~₽1.22/шт (followers)
    (SourcePlatform.X, TaskType.VIEWS): "7819",               # ~₽0.003/шт
    # Pinterest.
    (SourcePlatform.PINTEREST, TaskType.LIKES): "6984",       # ~₽1.0/шт
    (SourcePlatform.PINTEREST, TaskType.SUBSCRIBES): "1562",  # ~₽4.19/шт
    # Yandex Дзен.
    (SourcePlatform.DZEN, TaskType.LIKES): "1223",            # ~₽0.70/шт
    (SourcePlatform.DZEN, TaskType.SUBSCRIBES): "8875",       # ~₽1.13/шт
    (SourcePlatform.DZEN, TaskType.VIEWS): "8642",            # ~₽0.27/шт
}

# Day 3 audit MEDIUM-7: shorter TTL = less stale price risk.
_SMMCODE_TOPUP_URL = "https://smmcode.shop/?do=funds"

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
            Capability.GET_QUOTE,
            Capability.GET_TOPUP_INFO,
            # NB: reseller API does not accept client-side order ids.
        }

    async def get_quote(
        self, metric: TaskType, platform: SourcePlatform, quantity: int
    ) -> Quote | None:
        """Return a price quote. Tries real catalog via the hardcoded service-id
        map (operator-curated); falls back to config mock if the tuple isn't mapped.
        """
        service_id = _SMMCODE_SERVICE_ID_MAP.get((platform, metric))
        if service_id is not None:
            try:
                price_per_unit = await self._service_price(service_id)
            except (KeyError, ValueError, RuntimeError, httpx.HTTPError):
                price_per_unit = None
            if price_per_unit is not None:
                return Quote(
                    exchange=self.name,
                    service_id=service_id,
                    metric=metric,
                    platform=platform,
                    quantity=quantity,
                    price_per_unit=price_per_unit,
                    total_price=price_per_unit * quantity,
                    currency="RUB",
                    eta_minutes_min=5,
                    eta_minutes_max=60,
                    confidence=0.9,
                    raw={"source": "smmcode_catalog"},
                )
        # Fallback path: config-driven mock for the demo before service ids are wired.
        from config import get_settings

        return quote_from_mock(
            self.name, metric, platform, quantity, get_settings().mock_quotes_csv,
            confidence=0.4,
        )

    async def get_topup_info(self) -> TopupInfo:
        return TopupInfo(
            exchange=self.name,
            topup_url=_SMMCODE_TOPUP_URL,
            min_amount=100.0,
            currency="RUB",
            payment_methods=["card", "yoomoney", "usdt", "qiwi"],
            notes=(
                "Reseller cabinet; кошелёк биржи, не наш. "
                "После пополнения баланс обновится в течение минуты."
            ),
        )

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
