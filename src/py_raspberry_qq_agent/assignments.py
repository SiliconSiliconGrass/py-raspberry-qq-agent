from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from .guild import notify

if TYPE_CHECKING:
    from .guild import GuildConfig, GuildRepository
    from .keycloak import KeycloakDirectory
    from .napcat import OutboundQueue


class AssignmentNotifications:
    """Notify current direct assignments, independently of due reminders and reports."""

    def __init__(
        self,
        config: GuildConfig,
        repository: GuildRepository,
        directory: KeycloakDirectory,
        outbound: OutboundQueue,
    ) -> None:
        self._config = config
        self._repository = repository
        self._directory = directory
        self._outbound = outbound

    async def check(self, start: datetime, end: datetime) -> None:
        rows = await self._repository.query(
            """
            SELECT n.id,n.title,m.subject,m.role,m.created_at
            FROM node_members m JOIN nodes n ON n.id=m.node_id
            WHERE m.created_at >= %s AND m.created_at < %s
            ORDER BY m.created_at,n.id,m.subject
        """,
            (start, end),
        )
        for row in rows:
            role = "负责人" if row["role"] == "leader" else "成员"
            content = f"你已被指定为任务的{role}：{row['title']}"
            if self._config.public_url:
                content += f"\n{self._config.public_url.rstrip('/')}/nodes/{row['id']}"
            await notify(self._directory, self._outbound, row["subject"], content)
