"""Real adapter for the ipgold.ru microtask-exchange API.

**Status: API methods are NOT publicly documented.**
This adapter is implemented based on the most probable pattern for Russian
microtask exchanges (similar to UNU / Advego), with a generic REST-like form-
encoded interface. It will be updated as soon as real API documentation or
reverse-engineered endpoints become available.

Expected API pattern (hypothesised):
- POST form-encoded to a base endpoint.
- Auth via `api_key` parameter.
- Actions: `create_order`, `order_status`, `list_submissions`, `accept`, `reject`.

Until confirmed, `get_balance` returns 0.0 with a warning, and `create_order`
raises `RuntimeError` to prevent accidental placement until the write API is verified.

Docs reference: `docs/api/ipgold.md`.
"""

from __future__ import annotations

import warnings
from typing import Any

import httpx

from adapters.base import Capability, TaskExchangeAdapter
from models import ExternalSubmission, OrderSpec

_BASE_URL = "https://ipgold.ru/api"


class IpgoldAdapter(TaskExchangeAdapter):
    """ipgold.ru adapter — provisional, pending real API confirmation."""

    name = "ipgold"

    def __init__(self, api_key: str, http_client: httpx.AsyncClient) -> None:
        if not api_key:
            raise ValueError("ipgold api_key is required (set IPGOLD_API_KEY in .env)")
        self._api_key = api_key
        self._client = http_client

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
        warnings.warn(
            "ipgold API is not yet confirmed; get_balance returns 0.0. "
            "Update the adapter once real endpoints are known.",
            stacklevel=2,
        )
        return 0.0

    async def create_order(self, spec: OrderSpec, client_order_uuid: str) -> tuple[str, float]:
        # HIGH-c invariant: even provisional adapters enforce max_cost.
        # We use a placeholder cost because the real price endpoint is unknown.
        placeholder_cost_per_unit = 5.0
        cost = placeholder_cost_per_unit * spec.quantity
        if cost > spec.max_cost:
            raise ValueError(f"computed cost {cost:.2f} exceeds spec.max_cost {spec.max_cost:.2f}")
        # Prevent accidental live placement when API is unconfirmed.
        raise RuntimeError(
            "ipgold adapter is provisional: real API endpoints are not confirmed. "
            "Create-order support for ipgold needs confirmed live API docs before use."
        )

    async def get_order_status(self, external_order_id: str) -> str:
        raise RuntimeError("ipgold adapter is provisional: real API endpoints are not confirmed.")

    async def list_submissions(self, external_order_id: str) -> list[ExternalSubmission]:
        raise RuntimeError("ipgold adapter is provisional: real API endpoints are not confirmed.")

    async def accept_submission(self, external_order_id: str, external_submission_id: str) -> None:
        raise RuntimeError("ipgold adapter is provisional: real API endpoints are not confirmed.")

    async def reject_submission(
        self, external_order_id: str, external_submission_id: str, reason: str
    ) -> None:
        raise RuntimeError("ipgold adapter is provisional: real API endpoints are not confirmed.")

    # --- internal helpers (stubs for future real implementation) ------------

    async def _post(self, params: dict[str, Any]) -> dict[str, Any]:
        body = {"api_key": self._api_key, **params}
        response = await self._client.post(_BASE_URL, data=body)
        response.raise_for_status()
        payload = response.json()
        if isinstance(payload, dict) and payload.get("error"):
            raise RuntimeError(f"ipgold error: {payload['error']!r}")
        return payload
