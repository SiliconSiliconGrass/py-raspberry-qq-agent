from __future__ import annotations

import asyncio
from typing import Any

import pytest

from py_raspberry_qq_agent.agent import AgentRunner, AgentWorker
from py_raspberry_qq_agent.config import AppConfig
from py_raspberry_qq_agent.llm import AssistantReply
from py_raspberry_qq_agent.todos import TodoStore
from py_raspberry_qq_agent.toolset import Tool, ToolRegistry


class ScriptedLlm:
    def __init__(self, replies: list[AssistantReply]) -> None:
        self.replies = replies

    async def complete(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        force_json: bool = False,
    ) -> AssistantReply:
        return self.replies.pop(0)


def _call(name: str, arguments: str, call_id: str) -> dict[str, Any]:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": arguments},
    }


@pytest.mark.asyncio
async def test_agent_must_use_tools_and_can_clear_todo(
    config: AppConfig,
    tmp_path: Any,
) -> None:
    todos = TodoStore(tmp_path / "todos.json")
    item = await todos.add(
        "reply",
        chat_type="private",
        encrypted_target_id="qq-user:abc",
    )
    registry = ToolRegistry()
    sent: list[dict[str, Any]] = []

    async def send(args: dict[str, Any]) -> dict[str, Any]:
        sent.append(args)
        return {"queued": True}

    async def delete(args: dict[str, Any]) -> dict[str, Any]:
        return {"deleted": bool(await todos.delete(str(args["id"])))}

    registry.register(
        Tool(
            "send_qq_message",
            "send",
            {"type": "object", "properties": {}},
            send,
        )
    )
    registry.register(
        Tool("delete_todo_item", "delete", {"type": "object", "properties": {}}, delete)
    )
    llm = ScriptedLlm(
        [
            AssistantReply(
                "",
                [
                    _call(
                        "send_qq_message",
                        '{"chat_type":"private",'
                        '"encrypted_target_id":"qq-user:abc","content":"ok"}',
                        "call-1",
                    )
                ],
                {},
            ),
            AssistantReply(
                "",
                [_call("delete_todo_item", f'{{"id":"{item.id}"}}', "call-2")],
                {},
            ),
            AssistantReply("internal finished text", [], {}),
        ]
    )
    runner = AgentRunner(config, llm, registry)  # type: ignore[arg-type]
    await runner.run_todo_round([item])

    assert sent == [
        {
            "chat_type": "private",
            "encrypted_target_id": "qq-user:abc",
            "content": "ok",
        }
    ]
    assert await todos.length() == 0


@pytest.mark.asyncio
async def test_worker_rewakes_until_todo_is_cleared(
    config: AppConfig,
    tmp_path: Any,
) -> None:
    todos = TodoStore(tmp_path / "todos.json")
    item = await todos.add("keep working")

    class RewakingRunner:
        calls = 0

        async def run_todo_round(self, items: list[Any]) -> None:
            self.calls += 1
            if self.calls == 2:
                await todos.delete(item.id)

    runner = RewakingRunner()
    worker = AgentWorker(config, runner, todos)  # type: ignore[arg-type]
    worker.start()
    try:
        for _ in range(50):
            if await todos.length() == 0:
                break
            await asyncio.sleep(0.01)
        assert runner.calls == 2
        assert await todos.length() == 0
    finally:
        await worker.stop()
