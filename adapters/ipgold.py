"""Real adapter for the ipgold.ru Advertiser API (v1).

Confirmed endpoint via the official OpenAPI spec (2026-05):

    POST https://ipgold.ru/api/v1/                Content-Type: application/json
    Body: {"key": <token>, "action": "<method>", ...params}

The method name can also be appended to the path (`/api/v1/<method>`); we use
the path form because it makes server logs easier to read.

Success envelope: ``{"status": "OK", "results": ...}``
Error envelope:   ``{"status": "BAD", "errors": [{"message": ..., "code": ...}]}``
Errors come back on HTTP 200, **400** (bad params), **403** (bad token), or
**429** (rate limit). The adapter parses the body before raising on status.

## Domain mapping

IPGold's primitive is the **advertising campaign**, NOT a one-shot order:

1. ``create_campaign(type_id, url, actions_per_day)`` — creates a campaign,
   spends NO money. Returns ``{id, type_id}``.
2. ``refill_campaign(type_id, id, actions_number=N)`` — credits N executions
   from the account balance to the campaign. **This is the money step.**
3. Campaign runs autonomously; remaining balance decreases as executions land.
4. ``get_campaign_info`` reports ``status`` (start/stop), ``moderation_status``
   (success/reject/wait) and remaining ``balance``.

We expose this as a `PanelAdapter`: each Telegram "order" → one IPGold
campaign whose lifetime balance is exactly ``spec.quantity`` executions.

## No account-level balance

Unlike smmcode/prskill, IPGold's API does NOT expose the account balance
directly — only per-campaign balances and the implicit error 42
("Insufficient funds") at refill time. We honestly omit ``GET_BALANCE`` from
``capabilities()`` so the bot's ``/balance`` reply shows
"баланс не отдаётся API" instead of a fake `0.00`.

## External order id shape

We need both ``type_id`` and ``id`` to query a campaign later, but our
domain model only stores a single ``external_order_id`` string. To bridge
that, ``create_order`` returns ``"<type_id>:<id>"`` (e.g. ``"24:123456"``).
``get_order_status`` parses it back.
"""

from __future__ import annotations

import time
from typing import Any

import httpx

from adapters.base import Capability, PanelAdapter, ServiceOption, keywords_for_scenario
from models import OrderSpec, Scenario

_BASE_URL = "https://ipgold.ru/api/v1"
_TYPES_TTL_SECONDS = 300.0


class IpgoldAdapter(PanelAdapter):
    """ipgold.ru advertiser API adapter (campaign-based)."""

    name = "ipgold"

    def __init__(self, api_key: str, http_client: httpx.AsyncClient) -> None:
        if not api_key:
            raise ValueError("ipgold api_key is required (set IPGOLD_API_KEY in .env)")
        self._key = api_key
        self._client = http_client
        self._types_cache: dict[str, dict[str, Any]] | None = None
        self._types_cache_at: float = 0.0

    def capabilities(self) -> set[Capability]:
        # NB: GET_BALANCE intentionally absent — IPGold has no account-level
        # balance endpoint, only per-campaign balances. `/balance` will say
        # "баланс не отдаётся API", consistent with advego.
        return {
            Capability.CREATE_ORDER,
            Capability.GET_ORDER_STATUS,
        }

    async def get_balance(self) -> float:
        raise NotImplementedError(
            "ipgold API does not expose an account-level balance; "
            "balances are per-campaign and surface only via refill_campaign "
            "(error 42 'Insufficient funds') or in the web cabinet"
        )

    async def list_services_for_scenario(
        self,
        scenario: Scenario,
        limit: int = 8,
    ) -> list[ServiceOption]:
        """Filter the cached campaign-types catalogue by scenario keywords.

        IPGold price per action is tiny (e.g. ``"0.00015"`` USD per simple
        visit), so values are kept verbatim — the bot's button label uses
        them as-is.
        """
        types = await self._get_campaign_types()
        keywords = keywords_for_scenario(scenario)
        if not keywords:
            return []
        candidates: list[ServiceOption] = []
        for ctype in types.values():
            name = " · ".join(
                filter(None, [str(ctype.get("category", "")), str(ctype.get("name", ""))])
            )
            haystack = name.lower()
            if not any(kw in haystack for kw in keywords):
                continue
            try:
                price = float(ctype.get("price", 0))
            except (TypeError, ValueError):
                price = 0.0
            if price <= 0:
                continue
            candidates.append(
                ServiceOption(
                    service_id=str(ctype.get("id", "")),
                    name=name[:64],
                    price_per_unit=price,
                )
            )
        candidates.sort(key=lambda s: (s.price_per_unit or 0.0, s.name))
        return candidates[:limit]

    async def create_order(self, spec: OrderSpec, client_order_uuid: str) -> tuple[str, float]:
        """Two-step IPGold flow:

        1. ``create_campaign`` (no money) — registers the campaign.
        2. ``refill_campaign`` (money step) — funds it for ``spec.quantity``
           executions.

        Estimate-first cost cap fires BEFORE step 1 so an over-budget order
        never reaches IPGold's servers at all. If step 1 succeeds but step 2
        fails, we surface a clear RuntimeError naming the orphan campaign id
        so an admin can refill it from the web cabinet — we never leave the
        bot's audit log unclear about whether money was spent.
        """
        if not spec.service_id:
            raise ValueError("ipgold requires spec.service_id (campaign type_id)")
        try:
            type_id = int(spec.service_id)
        except ValueError as exc:
            raise ValueError(
                f"ipgold service_id must be numeric (campaign type_id), got {spec.service_id!r}"
            ) from exc

        # Estimate-first: read live catalogue, compute cost, enforce cap.
        types = await self._get_campaign_types()
        ctype = types.get(str(type_id))
        if ctype is None:
            raise ValueError(f"ipgold: type_id {type_id} not in campaign-types catalogue")
        try:
            price_per_action = float(ctype["price"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(
                f"ipgold: type_id {type_id} has no usable 'price' field: {ctype!r}"
            ) from exc
        cost = price_per_action * spec.quantity
        if cost > spec.max_cost:
            raise ValueError(f"computed cost {cost:.6f} exceeds spec.max_cost {spec.max_cost:.6f}")

        # Step 1: create the campaign (no money yet).
        create_response = await self._post(
            "create_campaign",
            {
                "type_id": type_id,
                "url": spec.target,
                "actions_per_day": spec.quantity,
            },
        )
        results = create_response.get("results") or {}
        campaign_id = results.get("id")
        if campaign_id is None:
            raise RuntimeError(f"ipgold create_campaign: no campaign id in response: {results!r}")

        # Step 2: refill (the money-spending step).
        try:
            await self._post(
                "refill_campaign",
                {
                    "type_id": type_id,
                    "id": int(campaign_id),
                    "actions_number": spec.quantity,
                },
            )
        except Exception as exc:
            # Step 1 succeeded but step 2 failed → orphan campaign on IPGold.
            # Surface a clear error so the operator can decide whether to
            # refill from the web cabinet or leave it dormant. We do NOT swallow.
            raise RuntimeError(
                f"ipgold campaign {campaign_id} created (type_id={type_id}) "
                f"but refill_campaign failed: {exc}. "
                f"Campaign exists with zero balance — refill manually or delete "
                f"via the web cabinet."
            ) from exc

        # Compound external id so get_order_status can find the campaign later.
        external_id = f"{type_id}:{campaign_id}"
        return external_id, cost

    async def get_order_status(self, external_order_id: str) -> str:
        """Map IPGold's (status, moderation_status, balance) tuple to our
        normalised lifecycle status.

        - moderation_status='reject' → failed (campaign was rejected)
        - moderation_status='wait'   → in_progress (still under moderation)
        - moderation_status='success' AND status='stop' → failed (stopped by user)
        - moderation_status='success' AND balance>0    → in_progress (running)
        - moderation_status='success' AND balance==0   → completed (drained)
        """
        type_id_raw, _, campaign_id_raw = external_order_id.partition(":")
        if not campaign_id_raw:
            # Old-format id without type_id prefix → can't query; treat as failed.
            return "failed"
        try:
            type_id = int(type_id_raw)
            campaign_id = int(campaign_id_raw)
        except ValueError:
            return "failed"

        data = await self._post(
            "get_campaign_info",
            {"type_id": type_id, "id": campaign_id},
        )
        info = data.get("results") or {}
        moderation = str(info.get("moderation_status", "")).lower()
        if moderation == "reject":
            return "failed"
        if moderation == "wait":
            return "in_progress"
        # Moderation passed → check operational state.
        status = str(info.get("status", "")).lower()
        if status == "stop":
            return "failed"
        try:
            balance = int(info.get("balance", 0))
        except (TypeError, ValueError):
            balance = 0
        return "in_progress" if balance > 0 else "completed"

    # --- internal helpers --------------------------------------------------

    async def _post(self, action: str, params: dict[str, Any]) -> Any:
        """POST JSON to /api/v1/<action>.

        IPGold's error envelope ships on HTTP 400/403/429 alongside HTTP 200.
        We parse the body **before** ``raise_for_status`` so the API's own
        message (e.g. ``"Access denied" code 403``) reaches the operator
        verbatim. The api token is stripped from any error echo.
        """
        url = f"{_BASE_URL}/{action}"
        body = {"key": self._key, "action": action, **params}
        response = await self._client.post(url, json=body)

        try:
            payload = response.json()
        except Exception as exc:
            response.raise_for_status()
            raise RuntimeError(
                f"ipgold {action!r}: HTTP {response.status_code} with non-JSON body: "
                f"{response.text[:120]!r}"
            ) from exc

        if isinstance(payload, dict) and payload.get("status") == "BAD":
            errors = payload.get("errors") or []
            msgs = ", ".join(
                f"{e.get('message', '?')} (code {e.get('code', '?')})"
                for e in errors
                if isinstance(e, dict)
            )
            raise RuntimeError(f"ipgold {action!r} rejected: {msgs or payload!r}")

        # 4xx / 5xx with parseable JSON but no recognisable BAD envelope.
        if response.status_code >= 400:
            response.raise_for_status()

        return payload

    async def _get_campaign_types(self) -> dict[str, dict[str, Any]]:
        """Cache the campaign-types catalogue (`get_campaign_types`)."""
        if (
            self._types_cache is None
            or time.monotonic() - self._types_cache_at > _TYPES_TTL_SECONDS
        ):
            data = await self._post("get_campaign_types", {})
            raw = data.get("results") or []
            flat: dict[str, dict[str, Any]] = {}
            if isinstance(raw, list):
                for ctype in raw:
                    if isinstance(ctype, dict) and "id" in ctype:
                        flat[str(ctype["id"])] = ctype
            self._types_cache = flat
            self._types_cache_at = time.monotonic()
        return self._types_cache
