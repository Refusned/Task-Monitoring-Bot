"""Ollama-backed planner for turning free text into an AutopilotIntent."""

from __future__ import annotations

import json
from typing import Any

import httpx
from pydantic import ValidationError

from autopilot.models import AutopilotIntent


class OllamaPlannerError(RuntimeError):
    """Raised when Ollama cannot produce a valid structured intent."""


class OllamaPlanner:
    """Thin client for Ollama's `/api/chat` endpoint with structured outputs."""

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        http_client: httpx.AsyncClient,
        timeout_seconds: float = 30.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._client = http_client
        self._timeout_seconds = timeout_seconds

    async def plan_goal(self, goal_text: str) -> AutopilotIntent:
        goal = goal_text.strip()
        if not goal:
            raise OllamaPlannerError("goal text is empty")

        schema = AutopilotIntent.model_json_schema()
        payload = {
            "model": self._model,
            "stream": False,
            "format": schema,
            "options": {"temperature": 0},
            "messages": [
                {
                    "role": "system",
                    "content": _SYSTEM_PROMPT,
                },
                {
                    "role": "user",
                    "content": (
                        "Return only a JSON object matching this schema:\n"
                        f"{json.dumps(schema, ensure_ascii=False)}\n\n"
                        f"User goal: {goal}"
                    ),
                },
            ],
        }
        try:
            response = await self._client.post(
                f"{self._base_url}/api/chat",
                json=payload,
                timeout=self._timeout_seconds,
            )
            response.raise_for_status()
            data: dict[str, Any] = response.json()
            content = data.get("message", {}).get("content")
            if not isinstance(content, str) or not content.strip():
                raise OllamaPlannerError(f"ollama returned no message.content: {data!r}")
            return AutopilotIntent.model_validate_json(_strip_code_fence(content))
        except (httpx.HTTPError, ValueError, ValidationError) as exc:
            raise OllamaPlannerError(f"failed to plan goal via Ollama: {exc}") from exc


def _strip_code_fence(content: str) -> str:
    text = content.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    return text


_SYSTEM_PROMPT = """Ты планировщик заказов для Telegram-бота маркетинговой автоматизации.

Твоя задача: превратить цель пользователя в строгий JSON для дальнейшего детерминированного
исполнения кодом. Не выбирай биржу и service_id: это сделает код по живым каталогам и цене.

Правила классификации:
- подписчики, followers, subscribers -> scenario="activity_subscribe"
- лайки, likes, сердечки -> scenario="activity_like"
- просмотры видео/поста, views, watch -> scenario="activity_view"
- переходы/трафик/визиты/клики на сайт из соцсетей -> scenario="social_traffic"

target должен быть ссылкой или аккаунтом из цели пользователя.
quantity должен быть числом действий. Если пользователь пишет "1к", верни 1000.
max_cost заполняй только если пользователь явно указал бюджет.
source_platform нужен только для social_traffic: vk, x, youtube, telegram, dzen или pinterest.
Если цель неясна, всё равно верни лучший структурированный вариант с низким confidence и notes.
"""
