from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from py_raspberry_qq_agent.config import AppConfig
from py_raspberry_qq_agent.contacts import ContactVault
from py_raspberry_qq_agent.digest import GroupDigestService
from py_raspberry_qq_agent.todos import TodoStore


class FakeHistory:
    async def get(
        self,
        chat_type: str,
        encrypted_target_id: str,
        *,
        limit: int,
        start_time: float | None,
        end_time: float | None,
    ) -> list[dict[str, Any]]:
        return [
            {
                "sender": "qq-user:test",
                "content": "a useful discussion",
                "timestamp": 1_700_000_000,
            }
        ]


@pytest.mark.asyncio
async def test_group_digest_adds_encrypted_markdown_todo(
    config: AppConfig,
    tmp_path: Path,
) -> None:
    config.digest_enabled = True
    config.digest_groups = ["123456789"]
    config.digest_min_messages = 1
    vault = ContactVault(tmp_path / "contacts.json", config.encryption_key)
    alias = vault.encrypt("group", "123456789")
    todos = TodoStore(tmp_path / "todos.json")
    notifications: list[bool] = []
    service = GroupDigestService(
        config=config,
        vault=vault,
        history=FakeHistory(),  # type: ignore[arg-type]
        todos=todos,
        notify_worker=lambda: notifications.append(True),
        state_path=tmp_path / "digest_state.json",
    )

    await service._enqueue_digest(alias)
    items = await todos.list()
    assert len(items) == 1
    assert items[0].metadata["kind"] == "group_digest"
    assert items[0].encrypted_target_id == alias
    assert "123456789" not in items[0].context
    assert notifications == [True]
