from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

import pytest

from py_raspberry_qq_agent.agent import AgentWorker
from py_raspberry_qq_agent.allowlist import ChatAllowlist
from py_raspberry_qq_agent.config import AppConfig, ConfigError
from py_raspberry_qq_agent.contacts import ContactDirectory, RoleStore
from py_raspberry_qq_agent.first_reply import FirstReplyNotice
from py_raspberry_qq_agent.incoming import IncomingService
from py_raspberry_qq_agent.llm import AssistantReply
from py_raspberry_qq_agent.messages import MessageStore
from py_raspberry_qq_agent.napcat import IncomingMessage
from py_raspberry_qq_agent.priority import ImportanceGate
from py_raspberry_qq_agent.tasks import AgentTask, TaskQueue


class RecordingOutbound:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str]] = []
        self.enqueued_at: dict[tuple[str, str], float] = {}

    def enqueue(self, chat_type: str, target_id: str, content: str) -> None:
        self.sent.append((chat_type, target_id, content))
        self.enqueued_at[(chat_type, target_id)] = time.monotonic()

    def last_enqueue_at(self, chat_type: str, target_id: str) -> float:
        return self.enqueued_at.get((chat_type, target_id), 0.0)


def _task(target_id: str = "12345") -> AgentTask:
    return AgentTask(content="处理消息", chat_type="private", target_id=target_id)


def _notice(outbound: RecordingOutbound, delay: float = 0.05) -> FirstReplyNotice:
    return FirstReplyNotice(
        outbound=outbound,  # type: ignore[arg-type]
        delay_seconds=delay,
        message="这个问题有点复杂呢，让我好好想想……",
    )


@pytest.mark.asyncio
async def test_notice_sends_placeholder_after_delay() -> None:
    outbound = RecordingOutbound()
    notice = _notice(outbound)

    notice.arm(_task())
    await asyncio.sleep(0.15)

    assert outbound.sent == [
        ("private", "12345", "这个问题有点复杂呢，让我好好想想……")
    ]


@pytest.mark.asyncio
async def test_notice_stays_silent_when_reply_is_queued() -> None:
    outbound = RecordingOutbound()
    notice = _notice(outbound)

    notice.arm(_task())
    outbound.enqueue("private", "12345", "真正的回复")
    await asyncio.sleep(0.15)

    assert outbound.sent == [("private", "12345", "真正的回复")]


@pytest.mark.asyncio
async def test_notice_cancelled_when_task_finishes() -> None:
    outbound = RecordingOutbound()
    notice = _notice(outbound)
    task = _task()

    notice.arm(task)
    notice.disarm(task)
    await asyncio.sleep(0.15)

    assert outbound.sent == []


@pytest.mark.asyncio
async def test_notice_disabled_with_zero_delay() -> None:
    outbound = RecordingOutbound()
    notice = _notice(outbound, delay=0)

    notice.arm(_task())
    await asyncio.sleep(0.1)

    assert not notice.enabled
    assert outbound.sent == []


@pytest.mark.asyncio
async def test_new_message_in_same_chat_replaces_pending_notice() -> None:
    outbound = RecordingOutbound()
    notice = _notice(outbound)

    notice.arm(_task())
    notice.arm(_task())
    await asyncio.sleep(0.15)

    assert outbound.sent == [
        ("private", "12345", "这个问题有点复杂呢，让我好好想想……")
    ]


@pytest.mark.asyncio
async def test_worker_disarms_notice_for_quick_task() -> None:
    class QuickRunner:
        def __init__(self) -> None:
            self.seen: list[AgentTask] = []

        async def run_task(self, task: AgentTask) -> None:
            self.seen.append(task)

    outbound = RecordingOutbound()
    notice = _notice(outbound)
    tasks = TaskQueue()
    worker = AgentWorker(QuickRunner(), tasks, first_reply=notice)  # type: ignore[arg-type]
    worker.start()
    try:
        await tasks.add(content="处理消息", chat_type="private", target_id="12345")
        await asyncio.wait_for(tasks.join(), timeout=1)
    finally:
        await worker.stop()
    await asyncio.sleep(0.15)

    assert outbound.sent == []


class FakeClassifier:
    async def complete(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        force_json: bool = False,
    ) -> AssistantReply:
        return AssistantReply(
            content='{"important": true, "confidence": 0.95, "reason": "needs help"}',
            tool_calls=[],
            usage={},
        )


@pytest.mark.asyncio
async def test_incoming_message_arms_notice(config: AppConfig) -> None:
    config.first_reply_delay_seconds = 0.05
    config.first_reply_message = "稍等，我想想"
    roles = RoleStore(config.data_dir / "roles.json")
    contacts = ContactDirectory(config.data_dir / "profiles.json")
    messages = MessageStore(config.data_dir / "messages.jsonl")
    tasks = TaskQueue()
    allowlist = ChatAllowlist(
        enabled=config.allowlist_enabled,
        private_qq_ids=config.allowlist_private_qq_ids,
        group_ids=config.allowlist_group_ids,
    )
    gate = ImportanceGate(config, FakeClassifier(), allowlist, roles, contacts, messages)
    outbound = RecordingOutbound()
    incoming = IncomingService(
        contacts=contacts,
        messages=messages,
        gate=gate,
        tasks=tasks,
        first_reply=FirstReplyNotice(
            outbound=outbound,  # type: ignore[arg-type]
            delay_seconds=config.first_reply_delay_seconds,
            message=config.first_reply_message,
        ),
    )

    await incoming.handle(
        IncomingMessage(
            chat_type="private",
            raw_chat_id="20010",
            raw_sender_id="20010",
            sender_name="Tester",
            content="帮我看看这个问题",
            message_id="m1",
            timestamp=1_700_000_000,
            mentioned_bot=False,
        )
    )
    assert tasks.qsize() == 1
    await asyncio.sleep(0.15)

    assert outbound.sent == [("private", "20010", "稍等，我想想")]


def _config_yaml(extra: str) -> str:
    return (
        """
app:
  timezone: Asia/Shanghai
llm:
  main:
    base_url: https://example.invalid/v1
    api_key: main-key
    model: main-model
  classifier:
    base_url: https://example.invalid/v1
    api_key: classifier-key
    model: classifier-model
message_gate:
  outbound_min_interval_seconds: 0
  outbound_max_interval_seconds: 0
"""
        + extra
    )


def test_config_defaults_and_overrides(tmp_path: Path) -> None:
    default_path = tmp_path / "default.yaml"
    default_path.write_text(_config_yaml(""), encoding="utf-8")
    config = AppConfig.load(default_path)
    assert config.first_reply_delay_seconds == 3
    assert "让我好好想想" in config.first_reply_message

    custom_path = tmp_path / "custom.yaml"
    custom_path.write_text(
        _config_yaml("  first_reply_delay_seconds: 5\n  first_reply_message: 马上来\n"),
        encoding="utf-8",
    )
    custom = AppConfig.load(custom_path)
    assert custom.first_reply_delay_seconds == 5
    assert custom.first_reply_message == "马上来"


def test_config_rejects_negative_delay(tmp_path: Path) -> None:
    path = tmp_path / "bad.yaml"
    path.write_text(_config_yaml("  first_reply_delay_seconds: -1\n"), encoding="utf-8")
    with pytest.raises(ConfigError):
        AppConfig.load(path)
