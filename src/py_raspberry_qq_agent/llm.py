from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Any

import httpx

from .config import LlmConfig

LOGGER = logging.getLogger(__name__)


class LlmError(RuntimeError):
    pass


@dataclass(slots=True)
class AssistantReply:
    content: str
    tool_calls: list[dict[str, Any]]
    usage: dict[str, Any]


class OpenAICompatibleClient:
    def __init__(self, config: LlmConfig, *, retry_count: int = 2) -> None:
        self.config = config
        self._retry_count = max(0, retry_count)
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(config.timeout_seconds),
            headers={
                "Authorization": f"Bearer {config.api_key}",
                "Content-Type": "application/json",
            },
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def complete(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        force_json: bool = False,
    ) -> AssistantReply:
        payload: dict[str, Any] = {
            "model": self.config.model,
            "messages": messages,
            "temperature": self.config.temperature,
            "max_tokens": self.config.max_tokens,
            **self.config.extra_body,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        if force_json:
            payload.setdefault("response_format", {"type": "json_object"})

        last_error: Exception | None = None
        for attempt in range(self._retry_count + 1):
            try:
                response = await self._client.post(
                    f"{self.config.base_url}/chat/completions",
                    json=payload,
                )
                if response.status_code in {408, 429, 500, 502, 503, 504}:
                    raise httpx.HTTPStatusError(
                        f"temporary HTTP {response.status_code}",
                        request=response.request,
                        response=response,
                    )
                response.raise_for_status()
                data = response.json()
                choices = data.get("choices") or []
                if not choices:
                    raise LlmError("LLM response contained no choices")
                message = choices[0].get("message") or {}
                return AssistantReply(
                    content=_normalize_content(message.get("content")),
                    tool_calls=list(message.get("tool_calls") or []),
                    usage=dict(data.get("usage") or {}),
                )
            except (httpx.HTTPError, ValueError, LlmError) as exc:
                last_error = exc
                if attempt >= self._retry_count:
                    break
                delay = min(2**attempt, 8)
                LOGGER.warning(
                    "LLM request failed attempt=%d, retrying in %ss: %s",
                    attempt + 1,
                    delay,
                    type(exc).__name__,
                )
                await asyncio.sleep(delay)
        raise LlmError(f"LLM request failed: {last_error}") from last_error


def _normalize_content(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
        return "".join(parts)
    return str(value)


def extract_json_object(text: str) -> dict[str, Any]:
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        stripped = "\n".join(lines).strip()
    try:
        value = json.loads(stripped)
        if isinstance(value, dict):
            return value
    except json.JSONDecodeError:
        pass

    start = stripped.find("{")
    end = stripped.rfind("}")
    if start >= 0 and end > start:
        value = json.loads(stripped[start : end + 1])
        if isinstance(value, dict):
            return value
    raise LlmError("LLM did not return a JSON object")
