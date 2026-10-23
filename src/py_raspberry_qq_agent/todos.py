from __future__ import annotations

import asyncio
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .storage import atomic_write_json, read_json


@dataclass(slots=True)
class TodoItem:
    id: str
    content: str
    created_at: str
    source: str = ""
    context: str = ""
    chat_type: str = ""
    encrypted_target_id: str = ""
    encrypted_sender_id: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


class TodoStore:
    def __init__(self, path: Path) -> None:
        self._path = path
        raw = read_json(path, {"format": 1, "items": []})
        if not isinstance(raw, dict) or not isinstance(raw.get("items"), list):
            raise ValueError(f"invalid TODO store: {path}")  # noqa: TRY004
        self._items = [self._from_dict(item) for item in raw["items"]]
        self._lock = asyncio.Lock()

    @staticmethod
    def _from_dict(value: dict[str, Any]) -> TodoItem:
        return TodoItem(
            id=str(value.get("id", "")),
            content=str(value.get("content", "")),
            created_at=str(value.get("created_at", "")),
            source=str(value.get("source", "")),
            context=str(value.get("context", "")),
            chat_type=str(value.get("chat_type", value.get("chatt_type", ""))),
            encrypted_target_id=str(value.get("encrypted_target_id", "")),
            encrypted_sender_id=str(value.get("encrypted_sender_id", "")),
            metadata={
                **(value.get("metadata") or {}),
                **{
                    key: item
                    for key, item in value.items()
                    if key
                    not in {
                        "id",
                        "content",
                        "created_at",
                        "source",
                        "context",
                        "chat_type",
                        "chatt_type",
                        "encrypted_target_id",
                        "encrypted_sender_id",
                        "metadata",
                    }
                },
            },
        )

    async def add(
        self,
        content: str,
        *,
        source: str = "",
        context: str = "",
        chat_type: str = "",
        encrypted_target_id: str = "",
        encrypted_sender_id: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> TodoItem:
        if not content.strip():
            raise ValueError("TODO content cannot be empty")
        async with self._lock:
            item = TodoItem(
                id=f"todo-{uuid.uuid4().hex[:12]}",
                content=content.strip(),
                created_at=datetime.now(UTC).isoformat(),
                source=source,
                context=context,
                chat_type=chat_type,
                encrypted_target_id=encrypted_target_id,
                encrypted_sender_id=encrypted_sender_id,
                metadata=metadata or {},
            )
            self._items.append(item)
            self._save()
            return item

    async def list(self) -> list[TodoItem]:
        async with self._lock:
            return list(self._items)

    async def length(self) -> int:
        async with self._lock:
            return len(self._items)

    async def get(self, item_id: str) -> TodoItem | None:
        async with self._lock:
            return next((item for item in self._items if item.id == item_id), None)

    async def delete(self, item_id: str) -> TodoItem | None:
        async with self._lock:
            for index, item in enumerate(self._items):
                if item.id == item_id:
                    del self._items[index]
                    self._save()
                    return item
            return None

    def _save(self) -> None:
        atomic_write_json(
            self._path,
            {"format": 1, "items": [asdict(item) for item in self._items]},
        )
