"""Real adapter for the advego.com XML-RPC API.

API form: XML-RPC POST to `https://api.advego.com/xml`.
Auth: `token` parameter on every method call (in `.env` as `ADVEGO_API_TOKEN`).
Docs reference: `docs/api/advego.md`.

Notable behaviours:

- **Estimate-first cost check** (HIGH-c invariant). Before placing, the adapter fetches
  order types (cached), computes `cost = cost_per_unit * quantity`, and refuses if `max_cost`
  is exceeded.
- **XML-RPC via httpx.** We build XML request bodies with `xml.etree.ElementTree` and
  parse responses manually to stay async-native without the blocking `xmlrpc.client`.
- **TaskExchangeAdapter.** Jobs-level accept/return/decline lifecycle.
- **No client order id support.** `client_order_uuid` (C1) lives only in our SQLite.
"""

from __future__ import annotations

import time
import xml.etree.ElementTree as ET
from typing import Any

import httpx

from adapters.base import Capability, TaskExchangeAdapter
from models import ExternalSubmission, OrderSpec

_BASE_URL = "https://api.advego.com/xml"
_TYPES_TTL_SECONDS = 300.0

# advego.ordersGetState status -> normalized order status
_ORDER_STATUS_MAP: dict[str, str] = {
    "active": "in_progress",
    "paused": "in_progress",
    "completed": "completed",
    "canceled": "failed",
    "moderation": "in_progress",
    "draft": "in_progress",
}


# Helper to build XML-RPC request body
def _build_xmlrpc_call(method: str, params: dict[str, Any]) -> str:
    root = ET.Element("methodCall")
    method_name = ET.SubElement(root, "methodName")
    method_name.text = method
    params_el = ET.SubElement(root, "params")
    param_el = ET.SubElement(params_el, "param")
    value_el = ET.SubElement(param_el, "value")
    struct_el = ET.SubElement(value_el, "struct")
    for key, val in params.items():
        member = ET.SubElement(struct_el, "member")
        name = ET.SubElement(member, "name")
        name.text = str(key)
        v = ET.SubElement(member, "value")
        if isinstance(val, bool):
            v.text = "1" if val else "0"
        elif isinstance(val, int):
            v.text = str(val)
        elif isinstance(val, float):
            v.text = str(val)
        else:
            v.text = str(val)
    return ET.tostring(root, encoding="unicode")


def _parse_xmlrpc_response(xml_text: str) -> Any:
    """Minimal XML-RPC response parser for advego scalar / struct / array responses."""
    root = ET.fromstring(xml_text)
    params = root.find("params")
    if params is None:
        fault = root.find("fault")
        if fault is not None:
            raise RuntimeError(f"advego XML-RPC fault: {ET.tostring(fault, encoding='unicode')}")
        raise RuntimeError("advego XML-RPC response missing params")
    param = params.find("param")
    if param is None:
        raise RuntimeError("advego XML-RPC response missing param")
    value = param.find("value")
    if value is None:
        raise RuntimeError("advego XML-RPC response missing value")
    return _parse_value(value)


def _parse_value(value_el: ET.Element) -> Any:
    scalar = value_el.text
    for child in value_el:
        tag = child.tag.lower()
        if tag == "struct":
            result: dict[str, Any] = {}
            for member in child.findall("member"):
                name_el = member.find("name")
                val_el = member.find("value")
                if name_el is not None and val_el is not None:
                    result[name_el.text or ""] = _parse_value(val_el)
            return result
        elif tag == "array":
            data = child.find("data")
            if data is None:
                return []
            return [_parse_value(v) for v in data.findall("value")]
        elif tag == "boolean":
            return (child.text or "0") == "1"
        elif tag == "int" or tag == "i4":
            return int(child.text or 0)
        elif tag == "double":
            return float(child.text or 0.0)
        elif tag == "string":
            return child.text or ""
        else:
            return child.text or ""
    return scalar or ""


class AdvegoAdapter(TaskExchangeAdapter):
    """advego.com XML-RPC adapter."""

    name = "advego"

    def __init__(self, api_token: str, http_client: httpx.AsyncClient) -> None:
        if not api_token:
            raise ValueError("advego api_token is required (set ADVEGO_API_TOKEN in .env)")
        self._api_token = api_token
        self._client = http_client
        self._types_cache: dict[str, dict[str, Any]] | None = None
        self._types_cache_at: float = 0.0

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
        # Advego does not expose a direct balance endpoint in the public XML-RPC docs.
        # We return 0.0 and let the caller handle it via configuration or external tracking.
        return 0.0

    async def create_order(self, spec: OrderSpec, client_order_uuid: str) -> tuple[str, float]:
        # Estimate-first cost check.
        cost_per_unit = await self._order_type_cost(spec.service_id or "18")
        cost = cost_per_unit * spec.quantity
        if cost > spec.max_cost:
            raise ValueError(f"computed cost {cost:.2f} exceeds spec.max_cost {spec.max_cost:.2f}")

        # Create a campaign first if none provided (we don't have a campaign_id in spec).
        # For MVP: we create a new campaign per order; production should reuse campaigns.
        campaign_id = await self._create_campaign(title=f"Bot order {spec.scenario}")

        body = {
            "token": self._api_token,
            "campaign_id": str(campaign_id),
            "title": spec.scenario,
            "description": f"Automated order via bot for {spec.target}",
            "order_type": str(spec.service_id or "18"),  # 18 = Лайки в соцсетях
            "length": "1",
            "cost": str(cost_per_unit),
            "jobs_total": str(spec.quantity),
            "link": spec.target,
        }
        data = await self._call("advego.addOrder", body)
        external_id = str(data.get("ID") or data.get("id_order") or data.get("order_id"))
        if not external_id:
            raise RuntimeError(f"advego addOrder returned no ID: {data!r}")
        # Start the order immediately.
        await self._call("advego.startOrder", {"token": self._api_token, "ID": external_id})
        return external_id, cost

    async def get_order_status(self, external_order_id: str) -> str:
        data = await self._call(
            "advego.ordersGetState",
            {"token": self._api_token, "orders": [str(external_order_id)]},
        )
        orders = data.get("orders") if isinstance(data, dict) else data
        if isinstance(orders, list) and orders:
            raw_status = str(orders[0].get("state", orders[0].get("status", ""))).lower()
        elif isinstance(orders, dict):
            raw_status = str(orders.get("state", orders.get("status", ""))).lower()
        else:
            raw_status = ""
        return _ORDER_STATUS_MAP.get(raw_status, "failed")

    async def list_submissions(self, external_order_id: str) -> list[ExternalSubmission]:
        data = await self._call(
            "advego.getJobsCompleted",
            {
                "token": self._api_token,
                "id_order": str(external_order_id),
                "date_type": "complete",
            },
        )
        jobs = data if isinstance(data, list) else data.get("jobs", [])
        if not isinstance(jobs, list):
            return []
        results: list[ExternalSubmission] = []
        for job in jobs:
            if not isinstance(job, dict):
                continue
            job_id = str(job.get("ID") or job.get("id"))
            results.append(
                ExternalSubmission(
                    external_submission_id=job_id,
                    executor_hint=str(job.get("worker_id", job.get("author", ""))) or None,
                    evidence=str(job.get("text", job.get("result", ""))) or None,
                )
            )
        return results

    async def accept_submission(self, external_order_id: str, external_submission_id: str) -> None:
        await self._call(
            "advego.acceptJob",
            {"token": self._api_token, "ID": str(external_submission_id)},
        )

    async def reject_submission(
        self, external_order_id: str, external_submission_id: str, reason: str
    ) -> None:
        comment = reason if len(reason) >= 10 else f"{reason} (revision)"
        await self._call(
            "advego.returnJob",
            {"token": self._api_token, "ID": str(external_submission_id), "comment": comment},
        )

    # --- internal helpers --------------------------------------------------

    async def _call(self, method: str, params: dict[str, Any]) -> Any:
        xml_body = _build_xmlrpc_call(method, params)
        response = await self._client.post(
            _BASE_URL,
            content=xml_body,
            headers={"Content-Type": "text/xml"},
        )
        response.raise_for_status()
        return _parse_xmlrpc_response(response.text)

    async def _create_campaign(self, title: str) -> str:
        data = await self._call(
            "advego.addCampaign",
            {"token": self._api_token, "title": title},
        )
        campaign_id = str(data.get("campaign_id") or data.get("id") or data.get("ID"))
        if not campaign_id:
            raise RuntimeError(f"advego addCampaign returned no id: {data!r}")
        return campaign_id

    async def _order_type_cost(self, order_type: str) -> float:
        types = await self._get_order_types()
        ot = types.get(str(order_type))
        if ot is None:
            # Fallback: use a conservative default if unknown type.
            return 6.0
        return float(ot.get("min_cost", ot.get("cost", 6.0)))

    async def _get_order_types(self) -> dict[str, dict[str, Any]]:
        if (
            self._types_cache is None
            or time.monotonic() - self._types_cache_at > _TYPES_TTL_SECONDS
        ):
            data = await self._call("advego.getOrderTypes", {"token": self._api_token})
            raw = data if isinstance(data, list) else data.get("types", [])
            flat: dict[str, dict[str, Any]] = {}
            if isinstance(raw, list):
                for t in raw:
                    if isinstance(t, dict) and "id" in t:
                        flat[str(t["id"])] = t
                    elif isinstance(t, dict) and "order_type" in t:
                        flat[str(t["order_type"])] = t
            elif isinstance(raw, dict):
                for k, v in raw.items():
                    if isinstance(v, dict):
                        flat[str(k)] = v
            self._types_cache = flat
            self._types_cache_at = time.monotonic()
        return self._types_cache
