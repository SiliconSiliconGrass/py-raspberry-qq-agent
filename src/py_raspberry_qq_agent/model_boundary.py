from __future__ import annotations

import uuid
from typing import Any


class ModelIds:
    """Translate structured QQ IDs only at the LLM boundary, without encryption."""

    def __init__(self) -> None:
        self._refs: dict[tuple[str, str], str] = {}
        self._ids: dict[str, tuple[str, str]] = {}

    def encode(self, value: Any, chat_type: str = "private") -> Any:
        if isinstance(value, list):
            return [self.encode(item, chat_type) for item in value]
        if not isinstance(value, dict):
            return value
        chat_type = value.get("chat_type") or chat_type
        result = {}
        for key, item in value.items():
            if key in {"target_id", "chat_id", "sender_id", "contact_id"} and item:
                kind = (
                    "group"
                    if key in {"target_id", "chat_id"} and chat_type == "group"
                    else "user"
                )
                pair = (kind, str(item))
                if pair not in self._refs:
                    ref = f"{kind}:{uuid.uuid4().hex}"
                    self._refs[pair] = ref
                    self._ids[ref] = pair
                result[key] = self._refs[pair]
            else:
                result[key] = self.encode(item, chat_type)
        return result

    def decode(self, value: Any) -> Any:
        if isinstance(value, list):
            return [self.decode(item) for item in value]
        if not isinstance(value, dict):
            return value
        result = {}
        for key, item in value.items():
            if key in {"target_id", "chat_id", "sender_id", "contact_id"} and item:
                if not isinstance(item, str) or item not in self._ids:
                    raise ValueError("unknown contact reference")
                kind, raw_id = self._ids[item]
                expected = (
                    "group"
                    if key in {"target_id", "chat_id"}
                    and value.get("chat_type") == "group"
                    else "user"
                )
                if (
                    key in {"sender_id", "contact_id"} or value.get("chat_type")
                ) and kind != expected:
                    raise ValueError("contact reference does not match chat type")
                result[key] = raw_id
            else:
                result[key] = self.decode(item)
        return result
