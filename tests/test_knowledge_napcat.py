from pathlib import Path
from types import SimpleNamespace

import pytest

from py_raspberry_qq_agent.knowledge import KnowledgeBase
from py_raspberry_qq_agent.napcat import IncomingMessage, NapCatClient


def test_knowledge_paths_are_sandboxed(tmp_path: Path) -> None:
    knowledge_root = tmp_path / "knowledge"
    notebook_root = tmp_path / "notebooks"
    knowledge_root.mkdir()
    notebook_root.mkdir()
    (knowledge_root / "guide.md").write_text("alpha\nbeta\n", encoding="utf-8")
    base = KnowledgeBase(knowledge_root, notebook_root)

    assert base.search("beta")[0]["path"] == "guide.md"
    base.write("digests/one.md", "# One\n")
    assert (notebook_root / "digests" / "one.md").is_file()
    read = base.read_lines("notebook", "digests/one.md")
    assert read["lines"] == ["# One"]
    with pytest.raises(ValueError):
        base.read_lines("knowledge", "../outside.txt")


def test_napcat_parser_omits_raw_at_target_and_detects_mention() -> None:
    async def handler(message: IncomingMessage) -> None:
        return None

    config = SimpleNamespace(
        napcat_request_timeout_seconds=30,
        outbound_send_timeout_seconds=30,
        napcat_access_token="",
        napcat_ws_url="ws://127.0.0.1:3001",
        napcat_reconnect_seconds=1,
    )
    client = NapCatClient(config, handler)  # type: ignore[arg-type]
    parsed = client._parse_message(
        {
            "self_id": 90001,
            "post_type": "message",
            "message_type": "group",
            "group_id": 88888,
            "user_id": 77777,
            "message_id": 1,
            "time": 1700000000,
            "sender": {"nickname": "Alice"},
            "message": [
                {"type": "at", "data": {"qq": "90001"}},
                {"type": "text", "data": {"text": " hello"}},
            ],
        }
    )
    assert parsed is not None
    assert parsed.content == "hello"
    assert parsed.mentioned_bot is True
    assert "90001" not in parsed.content

    cq_parsed = client._parse_message(
        {
            "self_id": 90001,
            "post_type": "message",
            "message_type": "group",
            "group_id": 88888,
            "user_id": 77777,
            "message_id": 2,
            "time": 1700000001,
            "sender": {"nickname": "Alice"},
            "message": "[CQ:at,qq=90001] CQ string",
        }
    )
    assert cq_parsed is not None
    assert cq_parsed.content == "CQ string"
    assert cq_parsed.mentioned_bot is True
