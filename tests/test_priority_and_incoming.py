from __future__ import annotations

from typing import Any

import pytest

from py_raspberry_qq_agent.allowlist import ChatAllowlist
from py_raspberry_qq_agent.config import AppConfig
from py_raspberry_qq_agent.contacts import ContactDirectory, ContactVault, RoleStore
from py_raspberry_qq_agent.incoming import IncomingService
from py_raspberry_qq_agent.llm import AssistantReply
from py_raspberry_qq_agent.messages import MessageStore
from py_raspberry_qq_agent.napcat import IncomingMessage
from py_raspberry_qq_agent.priority import ImportanceGate
from py_raspberry_qq_agent.todos import TodoStore


class FakeClassifier:
    def __init__(self, important: bool) -> None:
        self.calls = 0
        self.important = important

    async def complete(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        force_json: bool = False,
    ) -> AssistantReply:
        self.calls += 1
        return AssistantReply(
            content=(
                '{"important": %s, "confidence": 0.95, '
                '"reason": "needs assistance"}'
            )
            % ("true" if self.important else "false"),
            tool_calls=[],
            usage={},
        )


def _services(
    config: AppConfig,
    classifier: FakeClassifier,
) -> tuple[
    IncomingService,
    TodoStore,
    ContactVault,
    RoleStore,
    list[int],
]:
    vault = ContactVault(config.data_dir / "contacts.json", config.encryption_key)
    roles = RoleStore(config.data_dir / "roles.json")
    contacts = ContactDirectory(config.data_dir / "profiles.json")
    messages = MessageStore(config.data_dir / "messages.jsonl")
    todos = TodoStore(config.data_dir / "todos.json")
    allowlist = ChatAllowlist(
        enabled=config.allowlist_enabled,
        private_qq_ids=config.allowlist_private_qq_ids,
        group_ids=config.allowlist_group_ids,
        vault=vault,
    )
    gate = ImportanceGate(
        config,
        classifier,  # type: ignore[arg-type]
        allowlist,
        roles,
        contacts,
        messages,
    )
    notifications = [0]

    def notify() -> None:
        notifications[0] += 1

    incoming = IncomingService(
        vault=vault,
        contacts=contacts,
        messages=messages,
        gate=gate,
        todos=todos,
        notify_worker=notify,
    )
    return incoming, todos, vault, roles, notifications


def _message(raw_user: str, text: str, message_id: str) -> IncomingMessage:
    return IncomingMessage(
        chat_type="private",
        raw_chat_id=raw_user,
        raw_sender_id=raw_user,
        sender_name="Tester",
        content=text,
        message_id=message_id,
        timestamp=1_700_000_000,
        mentioned_bot=False,
    )


@pytest.mark.asyncio
async def test_admin_bypasses_classifier(config: AppConfig) -> None:
    classifier = FakeClassifier(important=False)
    incoming, todos, vault, roles, notifications = _services(config, classifier)
    alias = vault.encrypt("user", "20001")
    roles.set_admin(alias, True)

    await incoming.handle(_message("20001", "please help", "m1"))

    assert classifier.calls == 0
    assert await todos.length() == 1
    assert notifications[0] == 1
    item = (await todos.list())[0]
    assert item.encrypted_sender_id == alias
    assert "20001" not in str(item)


@pytest.mark.asyncio
async def test_non_admin_uses_classifier(config: AppConfig) -> None:
    classifier = FakeClassifier(important=True)
    incoming, todos, _, _, _ = _services(config, classifier)

    await incoming.handle(_message("20002", "please help", "m2"))
    assert classifier.calls == 1
    assert await todos.length() == 1


@pytest.mark.asyncio
async def test_unimportant_message_does_not_enter_todo(config: AppConfig) -> None:
    classifier = FakeClassifier(important=False)
    incoming, todos, _, _, notifications = _services(config, classifier)

    await incoming.handle(_message("20003", "hello", "m3"))
    assert classifier.calls == 1
    assert await todos.length() == 0
    assert notifications[0] == 0


@pytest.mark.asyncio
async def test_unmentioned_group_message_skips_classifier(config: AppConfig) -> None:
    classifier = FakeClassifier(important=True)
    incoming, todos, _, _, _ = _services(config, classifier)
    message = _message("20004", "group chatter", "m4")
    message.chat_type = "group"
    message.raw_chat_id = "30001"

    await incoming.handle(message)
    assert classifier.calls == 0
    assert await todos.length() == 0


@pytest.mark.asyncio
async def test_allowlist_overrides_admin_for_unlisted_private_chat(
    config: AppConfig,
) -> None:
    config.allowlist_enabled = True
    config.allowlist_private_qq_ids = ["20010"]
    classifier = FakeClassifier(important=True)
    incoming, todos, vault, roles, notifications = _services(config, classifier)
    admin_alias = vault.encrypt("user", "20011")
    roles.set_admin(admin_alias, True)

    await incoming.handle(_message("20011", "please help", "m10"))

    assert classifier.calls == 0
    assert await todos.length() == 0
    assert notifications[0] == 0


@pytest.mark.asyncio
async def test_allowlisted_private_chat_uses_normal_importance_flow(
    config: AppConfig,
) -> None:
    config.allowlist_enabled = True
    config.allowlist_private_qq_ids = ["20012"]
    classifier = FakeClassifier(important=True)
    incoming, todos, _, _, notifications = _services(config, classifier)

    await incoming.handle(_message("20012", "please help", "m11"))

    assert classifier.calls == 1
    assert await todos.length() == 1
    assert notifications[0] == 1


@pytest.mark.asyncio
async def test_allowlist_checks_group_id_before_mention_or_admin(
    config: AppConfig,
) -> None:
    config.allowlist_enabled = True
    config.allowlist_group_ids = ["30012"]
    classifier = FakeClassifier(important=True)
    incoming, todos, vault, roles, _ = _services(config, classifier)
    admin_alias = vault.encrypt("user", "20013")
    roles.set_admin(admin_alias, True)
    message = _message("20013", "@bot please help", "m12")
    message.chat_type = "group"
    message.raw_chat_id = "30013"
    message.mentioned_bot = True

    await incoming.handle(message)

    assert classifier.calls == 0
    assert await todos.length() == 0
