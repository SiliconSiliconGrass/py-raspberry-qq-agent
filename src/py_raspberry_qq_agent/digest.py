from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from .config import AppConfig
from .contacts import ContactVault
from .storage import atomic_write_json, read_json
from .todos import TodoStore
from .toolset import HistoryService

LOGGER = logging.getLogger(__name__)


class GroupDigestService:
    def __init__(
        self,
        *,
        config: AppConfig,
        vault: ContactVault,
        history: HistoryService,
        todos: TodoStore,
        notify_worker: Callable[[], None],
        state_path: Path,
    ) -> None:
        self._config = config
        self._vault = vault
        self._history = history
        self._todos = todos
        self._notify_worker = notify_worker
        self._state_path = state_path
        self._tz = ZoneInfo(config.timezone)
        self._groups: dict[str, str] = {}
        self._cursors: dict[str, float] = {}
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if not self._config.digest_enabled:
            return
        self._groups = {
            self._vault.encrypt("group", raw_group_id): raw_group_id
            for raw_group_id in self._config.digest_groups
        }
        state = read_json(self._state_path, {"format": 1, "cursors": {}})
        if not isinstance(state, dict):
            raise ValueError(  # noqa: TRY004
                f"invalid digest state: {self._state_path}"
            )
        self._cursors = {
            str(key): float(value)
            for key, value in (state.get("cursors") or {}).items()
        }
        now = datetime.now(self._tz)
        for encrypted_group in self._groups:
            self._cursors.setdefault(
                encrypted_group,
                (
                    now
                    - timedelta(minutes=self._config.digest_initial_lookback_minutes)
                ).timestamp(),
            )
        self._save_state()
        self._task = asyncio.create_task(self._run(), name="group-digest")
        LOGGER.info("group digest enabled groups=%d", len(self._groups))

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            await self._task
            self._task = None

    async def _run(self) -> None:
        interval = self._config.digest_interval_minutes * 60
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=interval)
                continue
            except TimeoutError:
                pass
            for encrypted_group in self._groups:
                try:
                    await self._enqueue_digest(encrypted_group)
                except Exception:
                    LOGGER.exception(
                        "group digest failed target=%s", encrypted_group
                    )

    async def _enqueue_digest(self, encrypted_group: str) -> None:
        now = datetime.now(self._tz)
        start_timestamp = self._cursors.get(
            encrypted_group, now.timestamp() - 3600
        )
        messages = await self._history.get(
            "group",
            encrypted_group,
            limit=self._config.history_max_fetch,
            start_time=start_timestamp,
            end_time=now.timestamp(),
        )
        if len(messages) < self._config.digest_min_messages:
            LOGGER.info(
                "group digest skipped target=%s messages=%d",
                encrypted_group,
                len(messages),
            )
            return
        start = datetime.fromtimestamp(start_timestamp, self._tz)
        filename = (
            "group-digests/"
            f"{encrypted_group.replace(':', '-')}-"
            f"{now.strftime('%Y%m%d-%H%M')}.md"
        )
        item = await self._todos.add(
            content=(
                f"总结加密群 {encrypted_group} 在 {start.isoformat()} 至 "
                f"{now.isoformat()} 的消息。{self._config.digest_prompt}"
            ),
            source=f"group-digest:{encrypted_group}",
            context=(
                "任务类型：group_digest\n"
                f"encrypted_group={encrypted_group}\n"
                f"start_time={start.isoformat()}\n"
                f"end_time={now.isoformat()}\n"
                f"message_count={len(messages)}\n"
                f"suggested_notebook_path={filename}\n"
                "请先调用 get_qq_message_history 核对上下文，然后生成结构化 Markdown。"
                "用 write_notebook 保存为建议路径的文件。若工具列表中存在名称以 "
                "mcp__ 开头的 Markdown/文档上传工具，必须调用它上传；"
                "将文档标题、完整 Markdown 和群加密标识传给该工具。"
                "不要为了总结而向群里发送消息。"
            ),
            chat_type="group",
            encrypted_target_id=encrypted_group,
            metadata={
                "kind": "group_digest",
                "start_time": start.isoformat(),
                "end_time": now.isoformat(),
                "message_count": len(messages),
                "suggested_notebook_path": filename,
            },
        )
        self._cursors[encrypted_group] = now.timestamp()
        self._save_state()
        LOGGER.info(
            "group digest queued todo=%s target=%s messages=%d",
            item.id,
            encrypted_group,
            len(messages),
        )
        self._notify_worker()

    def _save_state(self) -> None:
        atomic_write_json(
            self._state_path,
            {"format": 1, "cursors": self._cursors},
        )
