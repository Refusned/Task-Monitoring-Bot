"""MCP stdio server. OpenClaw runs this as a child process; the LLM calls
tools by name and gets JSON results.

Architecture:
    OpenClaw ──stdio──► this MCP server ──HTTP──► FastAPI (/api/tools/*)

Each MCP tool is a thin proxy to the matching `/api/tools/<name>` endpoint.
The HTTP layer holds all business logic (validation, idempotency, persistence);
this file only translates the protocol.

Run via: `python -m mcp_server.server`
Env: APP_BASE_URL, AGENT_TOOLS_TOKEN
"""

from __future__ import annotations

import asyncio
import json
import logging
import os

import httpx
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

_LOG = logging.getLogger("smm-mcp")

_BASE_URL = os.environ.get("APP_BASE_URL", "http://127.0.0.1:8765")
_TOKEN = os.environ.get("AGENT_TOOLS_TOKEN", "")
_TIMEOUT = float(os.environ.get("MCP_HTTP_TIMEOUT", "60"))

server = Server("smm-aggregator")


# ---- Tool schemas (mirror FastAPI Pydantic models) ----

_METRIC_VALUES = ["likes", "views", "subscribes", "comments", "shares", "traffic"]
_PLATFORM_VALUES = ["vk", "x", "youtube", "telegram", "dzen", "pinterest"]


_TOOLS: list[Tool] = [
    Tool(
        name="get_quote",
        description=(
            "Параллельно опрашивает все 5 бирж: возвращает массив котировок, "
            "отсортированных по confidence+price, recommended_exchange и "
            "lowest_price_exchange. "
            "Используй ПЕРВЫМ при разборе цели пользователя."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "metric": {"type": "string", "enum": _METRIC_VALUES},
                "platform": {"type": "string", "enum": _PLATFORM_VALUES},
                "quantity": {"type": "integer", "minimum": 1},
                "target_url": {"type": "string"},
            },
            "required": ["metric", "platform", "quantity", "target_url"],
        },
    ),
    Tool(
        name="get_balances",
        description=(
            "Балансы по каждой бирже. Используй ВТОРЫМ — после get_quote, "
            "чтобы выбрать первую биржу, на которой денег хватит. "
            "Перед place_order ставь force_refresh=true."
        ),
        inputSchema={
            "type": "object",
            "properties": {"force_refresh": {"type": "boolean", "default": False}},
        },
    ),
    Tool(
        name="get_topup_info",
        description=(
            "URL для пополнения конкретной биржи + минимум + методы. "
            "Вызывай когда get_balances показал нехватку денег на всех биржах — "
            "берёшь самую дешёвую quote и просишь её topup_info, шлёшь URL пользователю."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "exchange": {"type": "string"},
                "requested_amount": {"type": "number"},
                "user_chat_id": {"type": "integer"},
            },
            "required": ["exchange"],
        },
    ),
    Tool(
        name="capture_snapshot",
        description=(
            "Снимает baseline-значение метрики (likes/views/comments) на платформе ДО заказа. "
            "Возвращает snapshot_id — обязательно нужен для place_order. "
            "Без снапшота нельзя проверить дельту после доставки."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "platform": {"type": "string", "enum": _PLATFORM_VALUES},
                "target_url": {"type": "string"},
                "metric": {"type": "string", "enum": _METRIC_VALUES},
            },
            "required": ["platform", "target_url", "metric"],
        },
    ),
    Tool(
        name="place_order",
        description=(
            "Идемпотентное размещение заказа на бирже. ОБЯЗАТЕЛЬНО передавать snapshot_id "
            "из предыдущего capture_snapshot. max_cost >= total_price из quote. "
            "Биржа сама списывает деньги на своей стороне."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "exchange": {"type": "string"},
                "metric": {"type": "string", "enum": _METRIC_VALUES},
                "platform": {"type": "string", "enum": _PLATFORM_VALUES},
                "quantity": {"type": "integer", "minimum": 1},
                "target_url": {"type": "string"},
                "max_cost": {"type": "number", "exclusiveMinimum": 0},
                "snapshot_id": {"type": "string"},
                "service_id": {"type": "string"},
                "user_chat_id": {"type": "integer"},
                "allow_manual_verification": {"type": "boolean", "default": False},
            },
            "required": [
                "exchange",
                "metric",
                "platform",
                "quantity",
                "target_url",
                "max_cost",
                "snapshot_id",
            ],
        },
    ),
    Tool(
        name="check_order_status",
        description="Текущий статус заказа (заполнено биржей).",
        inputSchema={
            "type": "object",
            "properties": {"order_uuid": {"type": "string"}},
            "required": ["order_uuid"],
        },
    ),
    Tool(
        name="check_delta",
        description=(
            "Сравнить текущее значение метрики с baseline для заказа. "
            "Обычно вызывает scheduler сам — агенту нужно только если пользователь "
            "явно спросил «проверь сейчас»."
        ),
        inputSchema={
            "type": "object",
            "properties": {"order_uuid": {"type": "string"}},
            "required": ["order_uuid"],
        },
    ),
    Tool(
        name="report",
        description=(
            "Записать финальный отчёт в ленту дашборда (и опционально пушнуть в Telegram). "
            "Вызывай ПОСЛЕДНИМ в каждом успешном сценарии."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "order_uuid": {"type": "string"},
                "summary_md": {"type": "string"},
                "user_chat_id": {"type": "integer"},
            },
            "required": ["summary_md"],
        },
    ),
]


@server.list_tools()
async def list_tools() -> list[Tool]:
    return _TOOLS


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    """Proxy: POST {APP_BASE_URL}/api/tools/{name} with Bearer auth."""
    if not _TOKEN:
        return [
            TextContent(
                type="text",
                text=json.dumps({"error": "AGENT_TOOLS_TOKEN not set in MCP server env"}),
            )
        ]
    url = f"{_BASE_URL}/api/tools/{name}"
    headers = {"Authorization": f"Bearer {_TOKEN}", "Content-Type": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            response = await client.post(url, json=arguments or {}, headers=headers)
    except httpx.HTTPError as exc:
        return [
            TextContent(
                type="text",
                text=json.dumps({"error": f"http transport error: {exc!r}"}),
            )
        ]
    body_text = response.text
    if response.status_code >= 400:
        return [
            TextContent(
                type="text",
                text=json.dumps(
                    {"error": f"backend returned {response.status_code}", "body": body_text},
                    ensure_ascii=False,
                ),
            )
        ]
    return [TextContent(type="text", text=body_text)]


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    _LOG.info("smm-aggregator MCP server starting; backend=%s", _BASE_URL)
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
