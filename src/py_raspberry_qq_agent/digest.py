from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .config import AppConfig
from .knowledge import KnowledgeBase
from .llm import OpenAICompatibleClient
from .mcp_runtime import McpManager
from .reports import publish, summarize
from .toolset import HistoryService

LOGGER = logging.getLogger(__name__)


class GroupDigestService:
    def __init__(
        self,
        *,
        config: AppConfig,
        history: HistoryService,
        llm: OpenAICompatibleClient,
        knowledge: KnowledgeBase,
        mcp: McpManager,
    ) -> None:
        self._config = config
        self._history = history
        self._llm = llm
        self._knowledge = knowledge
        self._mcp = mcp
        self._tz = ZoneInfo(config.timezone)
        self._cursors: dict[str, datetime] = {}
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._config.digest_enabled:
            now = datetime.now(self._tz)
            self._cursors = {
                group: now
                - timedelta(minutes=self._config.digest_initial_lookback_minutes)
                for group in self._config.digest_groups
            }
            self._task = asyncio.create_task(self._run(), name="group-digest")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self._config.digest_interval_minutes * 60)
            end = datetime.now(self._tz)
            for group, start in list(self._cursors.items()):
                try:
                    await self.generate(group, start, end)
                except Exception:
                    LOGGER.exception("group digest failed")
                finally:
                    self._cursors[group] = end

    async def generate(self, group: str, start: datetime, end: datetime) -> None:
        messages = await self._history.get(
            "group",
            group,
            limit=self._config.history_max_fetch,
            start_time=start.timestamp(),
            end_time=end.timestamp(),
        )
        if len(messages) < self._config.digest_min_messages:
            return
        title = f"群聊总结 {start.isoformat()} 至 {end.isoformat()}"
        markdown = await summarize(
            self._llm, title + "\n" + self._config.digest_prompt, messages
        )
        await publish(
            self._knowledge,
            self._mcp,
            path=f"group-digests/{group}-{end:%Y%m%d-%H%M%S}.md",
            title=title,
            markdown=markdown,
            upload_tool=self._config.digest_upload_tool,
        )
