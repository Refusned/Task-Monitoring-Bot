"""Contract tests for AdvegoAdapter (Day 4).

Tests use `httpx.MockTransport` with XML responses matching the documented API
response shapes (see `docs/api/advego.md`).
No real network calls are made.
"""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest

from adapters.advego import AdvegoAdapter
from adapters.base import Capability
from models import OrderSpec, Scenario


def _make_client(
    handler: Callable[[httpx.Request], httpx.Response],
) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


# --- XML helpers -------------------------------------------------------------


def _xmlrpc_response(value_xml: str) -> str:
    return f"""\u003c?xml version=\"1.0\"?\u003e
\u003cmethodResponse\u003e
  \u003cparams\u003e
    \u003cparam\u003e
      \u003cvalue\u003e{value_xml}\u003c/value\u003e
    \u003c/param\u003e
  \u003c/params\u003e
\u003c/methodResponse\u003e"""


def _xmlrpc_fault(fault_string: str) -> str:
    return f"""\u003c?xml version=\"1.0\"?\u003e
\u003cmethodResponse\u003e
  \u003cfault\u003e
    \u003cvalue\u003e
      \u003cstruct\u003e
        \u003cmember\u003e
          \u003cname\u003efaultCode\u003c/name\u003e
          \u003cvalue\u003e\u003ci4\u003e-1\u003c/i4\u003e\u003c/value\u003e
        \u003c/member\u003e
        \u003cmember\u003e
          \u003cname\u003efaultString\u003c/name\u003e
          \u003cvalue\u003e\u003cstring\u003e{fault_string}\u003c/string\u003e\u003c/value\u003e
        \u003c/member\u003e
      \u003c/struct\u003e
    \u003c/value\u003e
  \u003c/fault\u003e
\u003c/methodResponse\u003e"""


# --- happy paths --------------------------------------------------------------


async def test_advego_get_balance_raises_not_implemented() -> None:
    """Advego has no public balance endpoint. The adapter raises rather than
    returning a fake 0.0 — and capabilities() honestly excludes GET_BALANCE.
    """
    async with _make_client(lambda r: httpx.Response(200, text="")) as http:
        adapter = AdvegoAdapter("test_token", http)
        with pytest.raises(NotImplementedError, match="no public balance"):
            await adapter.get_balance()


async def test_advego_create_order_happy_path() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = request.content.decode()
        if "advego.getOrderTypes" in body:
            xml = _xmlrpc_response("""\u003carray\u003e
                \u003cdata\u003e
                  \u003cvalue\u003e\u003cstruct\u003e
                    \u003cmember\u003e\u003cname\u003eid\u003c/name\u003e\u003cvalue\u003e\u003ci4\u003e18\u003c/i4\u003e\u003c/value\u003e\u003c/member\u003e
                    \u003cmember\u003e\u003cname\u003emin_cost\u003c/name\u003e\u003cvalue\u003e\u003cdouble\u003e6.0\u003c/double\u003e\u003c/value\u003e\u003c/member\u003e
                  \u003c/struct\u003e\u003c/value\u003e
                \u003c/data\u003e
              \u003c/array\u003e""")
            return httpx.Response(200, text=xml, headers={"Content-Type": "text/xml"})
        if "advego.addCampaign" in body:
            xml = _xmlrpc_response("""\u003cstruct\u003e
                \u003cmember\u003e\u003cname\u003ecampaign_id\u003c/name\u003e\u003cvalue\u003e\u003ci4\u003e42\u003c/i4\u003e\u003c/value\u003e\u003c/member\u003e
              \u003c/struct\u003e""")
            return httpx.Response(200, text=xml, headers={"Content-Type": "text/xml"})
        if "advego.addOrder" in body:
            xml = _xmlrpc_response("""\u003cstruct\u003e
                \u003cmember\u003e\u003cname\u003eID\u003c/name\u003e\u003cvalue\u003e\u003ci4\u003e12345\u003c/i4\u003e\u003c/value\u003e\u003c/member\u003e
              \u003c/struct\u003e""")
            return httpx.Response(200, text=xml, headers={"Content-Type": "text/xml"})
        if "advego.startOrder" in body:
            xml = _xmlrpc_response("""\u003cstruct\u003e
                \u003cmember\u003e\u003cname\u003eresult\u003c/name\u003e\u003cvalue\u003e\u003cstring\u003eok\u003c/string\u003e\u003c/value\u003e\u003c/member\u003e
              \u003c/struct\u003e""")
            return httpx.Response(200, text=xml, headers={"Content-Type": "text/xml"})
        return httpx.Response(404, text="not found")

    async with _make_client(handler) as http:
        adapter = AdvegoAdapter("test_token", http)
        spec = OrderSpec(
            scenario=Scenario.ACTIVITY_LIKE,
            exchange="advego",
            target="https://vk.com/wall-1_1",
            quantity=10,
            service_id="18",
            max_cost=200.0,
        )
        external_id, cost = await adapter.create_order(spec, "uuid-1")
        assert external_id == "12345"
        # min_cost=6.0, quantity=10 -> cost=60.0
        assert cost == pytest.approx(60.0)


async def test_advego_get_order_status() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = request.content.decode()
        assert "advego.ordersGetState" in body
        xml = _xmlrpc_response("""\u003cstruct\u003e
            \u003cmember\u003e\u003cname\u003eorders\u003c/name\u003e
              \u003cvalue\u003e\u003carray\u003e\u003cdata\u003e
                \u003cvalue\u003e\u003cstruct\u003e
                  \u003cmember\u003e\u003cname\u003eID\u003c/name\u003e\u003cvalue\u003e\u003ci4\u003e12345\u003c/i4\u003e\u003c/value\u003e\u003c/member\u003e
                  \u003cmember\u003e\u003cname\u003estate\u003c/name\u003e\u003cvalue\u003e\u003cstring\u003eactive\u003c/string\u003e\u003c/value\u003e\u003c/member\u003e
                \u003c/struct\u003e\u003c/value\u003e
              \u003c/data\u003e\u003c/array\u003e\u003c/value\u003e
            \u003c/member\u003e
          \u003c/struct\u003e""")
        return httpx.Response(200, text=xml, headers={"Content-Type": "text/xml"})

    async with _make_client(handler) as http:
        adapter = AdvegoAdapter("test_token", http)
        assert await adapter.get_order_status("12345") == "in_progress"


async def test_advego_list_submissions() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = request.content.decode()
        assert "advego.getJobsCompleted" in body
        xml = _xmlrpc_response("""\u003carray\u003e\u003cdata\u003e
            \u003cvalue\u003e\u003cstruct\u003e
              \u003cmember\u003e\u003cname\u003eID\u003c/name\u003e\u003cvalue\u003e\u003ci4\u003e999\u003c/i4\u003e\u003c/value\u003e\u003c/member\u003e
              \u003cmember\u003e\u003cname\u003eworker_id\u003c/name\u003e\u003cvalue\u003e\u003ci4\u003e77\u003c/i4\u003e\u003c/value\u003e\u003c/member\u003e
              \u003cmember\u003e\u003cname\u003etext\u003c/name\u003e\u003cvalue\u003e\u003cstring\u003e\
like done\u003c/string\u003e\u003c/value\u003e\u003c/member\u003e
            \u003c/struct\u003e\u003c/value\u003e
          \u003c/data\u003e\u003c/array\u003e""")
        return httpx.Response(200, text=xml, headers={"Content-Type": "text/xml"})

    async with _make_client(handler) as http:
        adapter = AdvegoAdapter("test_token", http)
        subs = await adapter.list_submissions("12345")
        assert len(subs) == 1
        assert subs[0].external_submission_id == "999"
        assert subs[0].executor_hint == "77"
        assert subs[0].evidence == "like done"


async def test_advego_accept_submission() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = request.content.decode()
        assert "advego.acceptJob" in body
        xml = _xmlrpc_response("""\u003cstruct\u003e
            \u003cmember\u003e\u003cname\u003eresult\u003c/name\u003e\u003cvalue\u003e\u003cstring\u003eok\u003c/string\u003e\u003c/value\u003e\u003c/member\u003e
          \u003c/struct\u003e""")
        return httpx.Response(200, text=xml, headers={"Content-Type": "text/xml"})

    async with _make_client(handler) as http:
        adapter = AdvegoAdapter("test_token", http)
        await adapter.accept_submission("12345", "999")


async def test_advego_reject_submission() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = request.content.decode()
        assert "advego.returnJob" in body
        # Check comment is at least 10 chars
        assert "low quality" in body
        xml = _xmlrpc_response("""\u003cstruct\u003e
            \u003cmember\u003e\u003cname\u003eresult\u003c/name\u003e\u003cvalue\u003e\u003cstring\u003eok\u003c/string\u003e\u003c/value\u003e\u003c/member\u003e
          \u003c/struct\u003e""")
        return httpx.Response(200, text=xml, headers={"Content-Type": "text/xml"})

    async with _make_client(handler) as http:
        adapter = AdvegoAdapter("test_token", http)
        await adapter.reject_submission("12345", "999", "low quality")


# --- defensive paths ---------------------------------------------------------


async def test_advego_refuses_create_when_cost_exceeds_max() -> None:
    create_called = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal create_called
        body = request.content.decode()
        if "advego.getOrderTypes" in body:
            xml = _xmlrpc_response("""\u003carray\u003e\u003cdata\u003e
                \u003cvalue\u003e\u003cstruct\u003e
                  \u003cmember\u003e\u003cname\u003eid\u003c/name\u003e\u003cvalue\u003e\u003ci4\u003e18\u003c/i4\u003e\u003c/value\u003e\u003c/member\u003e
                  \u003cmember\u003e\u003cname\u003emin_cost\u003c/name\u003e\u003cvalue\u003e\u003cdouble\u003e6.0\u003c/double\u003e\u003c/value\u003e\u003c/member\u003e
                \u003c/struct\u003e\u003c/value\u003e
              \u003c/data\u003e\u003c/array\u003e""")
            return httpx.Response(200, text=xml, headers={"Content-Type": "text/xml"})
        if "advego.addOrder" in body or "advego.addCampaign" in body:
            create_called = True
            return httpx.Response(
                200,
                text=_xmlrpc_response("\u003ci4\u003e1\u003c/i4\u003e"),
                headers={"Content-Type": "text/xml"},
            )
        return httpx.Response(404)

    async with _make_client(handler) as http:
        adapter = AdvegoAdapter("test_token", http)
        spec = OrderSpec(
            scenario=Scenario.ACTIVITY_LIKE,
            exchange="advego",
            target="https://vk.com/wall-1_1",
            quantity=10,
            service_id="18",
            max_cost=1.0,  # cost would be 60.00
        )
        with pytest.raises(ValueError, match=r"exceeds spec\.max_cost"):
            await adapter.create_order(spec, "uuid-1")
        assert not create_called, "addOrder must NOT be called when cost exceeds cap"


async def test_advego_raises_on_xmlrpc_fault() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text=_xmlrpc_fault("Invalid token"),
            headers={"Content-Type": "text/xml"},
        )

    async with _make_client(handler) as http:
        adapter = AdvegoAdapter("test_token", http)
        with pytest.raises(RuntimeError, match="XML-RPC fault"):
            await adapter.get_order_status("1")


async def test_advego_requires_non_empty_token() -> None:
    async with _make_client(lambda r: httpx.Response(500)) as http:
        with pytest.raises(ValueError, match="api_token"):
            AdvegoAdapter("", http)


async def test_advego_capabilities_task_exchange() -> None:
    async with _make_client(lambda r: httpx.Response(500)) as http:
        adapter = AdvegoAdapter("test_token", http)
        caps = adapter.capabilities()
        assert Capability.CREATE_ORDER in caps
        assert Capability.LIST_SUBMISSIONS in caps
        assert Capability.ACCEPT_SUBMISSION in caps
        assert Capability.REJECT_SUBMISSION in caps
        # GET_BALANCE intentionally absent: advego has no public balance API,
        # so we don't pretend in /health output.
        assert Capability.GET_BALANCE not in caps
        assert Capability.SUPPORTS_CLIENT_ORDER_ID not in caps
