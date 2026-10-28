from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class AgentTask:
    content: str
    source: str = ""
    context: str = ""
    chat_type: str = ""
    target_id: str = ""
    sender_id: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    id: str = field(default_factory=lambda: uuid.uuid4().hex)


class TaskQueue(asyncio.Queue[AgentTask]):
    """Best-effort work queue; pending work is intentionally lost on restart."""

    async def add(self, content: str, **kwargs: Any) -> AgentTask:
        if not content.strip():
            raise ValueError("task content cannot be empty")
        task = AgentTask(content=content, **kwargs)
        await self.put(task)
        return task
