from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from py_raspberry_qq_agent.config import AppConfig
from py_raspberry_qq_agent.digest import GroupDigestService
from py_raspberry_qq_agent.knowledge import KnowledgeBase
from py_raspberry_qq_agent.llm import AssistantReply


@pytest.mark.asyncio
async def test_digest_generates_file_and_explicit_upload_without_chat_agent(
    config: AppConfig,
) -> None:
    config.digest_min_messages = 1
    config.digest_upload_tool = "mcp__upload__upload_markdown"
    history = AsyncMock()
    history.get.return_value = [
        {"sender_id": "123456789", "content": "a useful discussion"}
    ]
    llm = AsyncMock()
    llm.complete.return_value = AssistantReply("# Summary", [], {})
    mcp = AsyncMock()
    mcp.call.return_value = {"ok": True}
    knowledge = KnowledgeBase(config.knowledge_dir, config.notebook_dir)
    service = GroupDigestService(
        config=config, history=history, llm=llm, knowledge=knowledge, mcp=mcp
    )
    end = datetime(2026, 10, 25, 12, tzinfo=UTC)
    await service.generate("987654321", end - timedelta(hours=1), end)
    assert (
        config.notebook_dir / "group-digests/987654321-20261025-120000.md"
    ).read_text() == "# Summary"
    assert "123456789" not in str(llm.complete.call_args)
    mcp.call.assert_awaited_once()
    assert mcp.call.call_args.args[0] == config.digest_upload_tool
