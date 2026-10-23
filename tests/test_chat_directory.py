from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from py_raspberry_qq_agent.config import AppConfig
from py_raspberry_qq_agent.contacts import ContactVault
from py_raspberry_qq_agent.toolset import ChatDirectoryService


class FakeDirectoryNapCat:
    async def fetch_friend_list(self) -> list[dict[str, Any]]:
        return [
            {"user_id": 10001, "nickname": "Alice", "remark": "Boss"},
            {"user_id": 10002, "nickname": "Bob", "remark": ""},
            {"user_id": 10003, "nickname": "Carol", "remark": ""},
            {"user_id": 10004, "nickname": "Dave", "remark": ""},
            {"user_id": 10005, "nickname": "Eve", "remark": ""},
        ]

    async def fetch_group_list(self) -> list[dict[str, Any]]:
        return [
            {
                "group_id": 20001,
                "group_name": "Alpha",
                "group_memo": "one",
                "member_count": 10,
                "max_member_count": 200,
            },
            {
                "group_id": 20002,
                "group_name": "Beta",
                "group_memo": "",
                "member_count": 20,
                "max_member_count": 500,
            },
            {
                "group_id": 20003,
                "group_name": "Gamma",
                "group_memo": "three",
                "member_count": 30,
                "max_member_count": 1000,
            },
        ]


def _service(config: AppConfig, tmp_path: Path) -> tuple[
    ChatDirectoryService,
    ContactVault,
]:
    config.directory_default_page_size = 2
    config.directory_max_page_size = 3
    vault = ContactVault(tmp_path / "contacts.json", config.encryption_key)
    return (
        ChatDirectoryService(
            config,
            FakeDirectoryNapCat(),  # type: ignore[arg-type]
            vault,
        ),
        vault,
    )


@pytest.mark.asyncio
async def test_friend_list_is_encrypted_and_paginated(
    config: AppConfig,
    tmp_path: Path,
) -> None:
    service, vault = _service(config, tmp_path)

    first = await service.friends(page=1)
    second = await service.friends(page=2, page_size=100)

    assert first["total"] == 5
    assert first["total_pages"] == 3
    assert first["has_more"] is True
    assert first["page_size"] == 2
    assert len(first["items"]) == 2
    assert second["page_size"] == 3
    assert len(second["items"]) == 2
    assert second["has_more"] is False
    assert all(item["encrypted_target_id"].startswith("qq-user:") for item in first["items"])
    assert all(item["encrypted_target_id"].startswith("qq-user:") for item in second["items"])
    assert "10001" not in json.dumps(first, ensure_ascii=False)
    alias = first["items"][0]["encrypted_target_id"]
    assert vault.resolve(alias)[0] == "user"


@pytest.mark.asyncio
async def test_group_list_is_encrypted_and_paginated(
    config: AppConfig,
    tmp_path: Path,
) -> None:
    service, vault = _service(config, tmp_path)

    result = await service.groups(page=1, page_size=2)

    assert result["total"] == 3
    assert result["total_pages"] == 2
    assert result["items"][0]["group_name"] == "Alpha"
    assert result["items"][0]["encrypted_target_id"].startswith("qq-group:")
    assert "20001" not in json.dumps(result, ensure_ascii=False)
    alias = result["items"][0]["encrypted_target_id"]
    assert vault.resolve(alias)[0] == "group"
