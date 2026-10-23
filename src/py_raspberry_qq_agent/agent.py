from __future__ import annotations

import asyncio
import json
import logging
import uuid
from dataclasses import asdict
from typing import Any

from .config import AppConfig
from .llm import AssistantReply, OpenAICompatibleClient
from .todos import TodoItem, TodoStore
from .toolset import ToolRegistry, json_tool_result

LOGGER = logging.getLogger(__name__)


class AgentRunner:
    def __init__(
        self,
        config: AppConfig,
        llm: OpenAICompatibleClient,
        tools: ToolRegistry,
    ) -> None:
        self._config = config
        self._llm = llm
        self._tools = tools
        self._history: list[dict[str, str]] = []

    async def run_todo_round(self, items: list[TodoItem]) -> None:
        activation = _activation_prompt(items)
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": self._config.system_prompt},
            *self._history,
            {"role": "user", "content": activation},
        ]
        definitions = self._tools.definitions()

        for round_number in range(self._config.max_tool_rounds):
            reply = await self._llm.complete(messages, tools=definitions)
            if not reply.tool_calls:
                if reply.content:
                    LOGGER.debug(
                        "agent returned text without QQ delivery (length=%d)",
                        len(reply.content),
                    )
                self._remember(activation, reply)
                return

            normalized_calls = [
                _normalize_tool_call(call, index)
                for index, call in enumerate(reply.tool_calls)
            ]
            messages.append(_assistant_message(reply, normalized_calls))
            for call in normalized_calls:
                await self._execute_call(call, messages)
            LOGGER.info(
                "agent tool round=%d calls=%d",
                round_number + 1,
                len(reply.tool_calls),
            )

        LOGGER.warning(
            "agent reached max_tool_rounds=%d; leaving TODO for a later wake",
            self._config.max_tool_rounds,
        )

    async def _execute_call(
        self,
        call: dict[str, Any],
        messages: list[dict[str, Any]],
    ) -> None:
        function = call.get("function") or {}
        tool_name = str(function.get("name", ""))
        tool_call_id = str(call.get("id") or f"call-{uuid.uuid4().hex[:12]}")
        raw_arguments = function.get("arguments") or "{}"
        try:
            if isinstance(raw_arguments, str):
                arguments = json.loads(raw_arguments)
            elif isinstance(raw_arguments, dict):
                arguments = raw_arguments
            else:
                raise TypeError("tool arguments must be JSON")
            if not isinstance(arguments, dict):
                raise TypeError("tool arguments must be a JSON object")
            result = await asyncio.wait_for(
                self._tools.execute(tool_name, arguments),
                timeout=self._config.tool_timeout_seconds,
            )
            payload = {"ok": True, "result": result}
            LOGGER.info("tool succeeded name=%s", tool_name)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - tool failures are returned to the model
            payload = {
                "ok": False,
                "error": f"{type(exc).__name__}: {exc}",
            }
            LOGGER.warning(
                "tool failed name=%s error=%s", tool_name, type(exc).__name__
            )

        messages.append(
            {
                "role": "tool",
                "tool_call_id": tool_call_id,
                "content": json_tool_result(payload),
            }
        )

    def _remember(self, activation: str, reply: AssistantReply) -> None:
        self._history.append({"role": "user", "content": activation})
        self._history.append(
            {
                "role": "assistant",
                "content": reply.content[:4000],
            }
        )
        limit = max(2, self._config.conversation_messages)
        self._history = self._history[-limit:]
        if len(self._history) % 2:
            self._history = self._history[1:]


class AgentWorker:
    def __init__(
        self,
        config: AppConfig,
        runner: AgentRunner,
        todos: TodoStore,
    ) -> None:
        self._config = config
        self._runner = runner
        self._todos = todos
        self._wake = asyncio.Event()
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    def notify(self) -> None:
        self._wake.set()

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="agent-worker")
            self.notify()

    async def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _run(self) -> None:
        while not self._stop.is_set():
            await self._wake.wait()
            self._wake.clear()
            for rewake in range(self._config.max_todo_rewakes + 1):
                items = await self._todos.list()
                if not items:
                    break
                LOGGER.info(
                    "agent activation todo_count=%d rewake=%d", len(items), rewake
                )
                try:
                    await self._runner.run_todo_round(items)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    LOGGER.exception("agent run failed; TODO items are retained")
                    break
                if await self._todos.length() == 0:
                    LOGGER.info("TODO list cleared")
                    break
            if await self._todos.length() > 0:
                LOGGER.warning(
                    "TODO list remains non-empty after this activation; "
                    "it will retry when new work arrives"
                )


def _assistant_message(
    reply: AssistantReply,
    normalized_calls: list[dict[str, Any]],
) -> dict[str, Any]:
    message: dict[str, Any] = {
        "role": "assistant",
        "content": reply.content or None,
        "tool_calls": normalized_calls,
    }
    return message


def _normalize_tool_call(call: dict[str, Any], index: int) -> dict[str, Any]:
    function = call.get("function") or {}
    arguments = function.get("arguments") or "{}"
    if not isinstance(arguments, str):
        arguments = json.dumps(arguments, ensure_ascii=False)
    return {
        "id": str(call.get("id") or f"call-{uuid.uuid4().hex[:12]}-{index}"),
        "type": "function",
        "function": {
            "name": str(function.get("name", "")),
            "arguments": arguments,
        },
    }


def _activation_prompt(items: list[TodoItem]) -> str:
    compact = [
        {
            **asdict(item),
            "content": item.content[:8000],
            "context": item.context[:4000],
        }
        for item in items
    ]
    return (
        "当前 TODO 列表如下。请逐项完成当前需要立即处理的工作。\n"
        "需要回复 QQ 时，必须调用 send_qq_message，并原样使用 item 中的 "
        "chat_type 与 encrypted_target_id；模型文本不会自动发送。\n"
        "每完成一项，必须调用 delete_todo_item 删除对应 id。\n"
        "群总结要先读取历史并写 Markdown；如果任务或系统规则要求上传，"
        "调用已提供的 MCP 上传工具。\n"
        "不要修改角色权限；你只能更新联系人资料。\n\n"
        f"TODO={json.dumps(compact, ensure_ascii=False)}"
    )
