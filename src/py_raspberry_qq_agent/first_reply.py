from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

from .napcat import OutboundQueue
from .tasks import AgentTask

LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class _Armed:
    task_id: str
    reply_baseline: float
    handle: asyncio.TimerHandle


class FirstReplyNotice:
    """给等待中的聊天先发一条占位消息。

    任务进入队列时开始计时；如果在设定时长内没有为该聊天排队任何 QQ
    消息，就自动发送一条提示。任务结束（包括不需要回复的情况）时撤销。
    """

    def __init__(
        self,
        *,
        outbound: OutboundQueue,
        delay_seconds: float,
        message: str,
    ) -> None:
        self._outbound = outbound
        self._delay_seconds = delay_seconds
        self._message = message
        self._armed: dict[tuple[str, str], _Armed] = {}

    @property
    def enabled(self) -> bool:
        return self._delay_seconds > 0 and bool(self._message.strip())

    def arm(self, task: AgentTask) -> None:
        if not self.enabled or not task.chat_type or not task.target_id:
            return
        key = (task.chat_type, task.target_id)
        self._cancel(key)
        armed = _Armed(
            task_id=task.id,
            reply_baseline=time.monotonic(),
            handle=asyncio.get_running_loop().call_later(
                self._delay_seconds, self._notify, key, task.id
            ),
        )
        self._armed[key] = armed
        LOGGER.debug(
            "first reply notice armed task=%s chat=%s target=%s delay=%.1fs",
            task.id,
            key[0],
            key[1],
            self._delay_seconds,
        )

    def disarm(self, task: AgentTask) -> None:
        key = (task.chat_type, task.target_id)
        armed = self._armed.get(key)
        if armed is not None and armed.task_id == task.id:
            self._cancel(key)

    def _cancel(self, key: tuple[str, str]) -> None:
        armed = self._armed.pop(key, None)
        if armed is not None:
            armed.handle.cancel()

    def _notify(self, key: tuple[str, str], task_id: str) -> None:
        armed = self._armed.pop(key, None)
        if armed is None or armed.task_id != task_id:
            return
        chat_type, target_id = key
        if self._outbound.last_enqueue_at(chat_type, target_id) >= armed.reply_baseline:
            LOGGER.debug(
                "first reply notice skipped task=%s chat=%s target=%s",
                task_id,
                chat_type,
                target_id,
            )
            return
        try:
            self._outbound.enqueue(chat_type, target_id, self._message)
        except (PermissionError, RuntimeError, ValueError) as exc:
            LOGGER.warning(
                "first reply notice not sent task=%s chat=%s target=%s error=%s",
                task_id,
                chat_type,
                target_id,
                type(exc).__name__,
            )
            return
        LOGGER.info(
            "first reply notice sent task=%s chat=%s target=%s delay=%.1fs",
            task_id,
            chat_type,
            target_id,
            self._delay_seconds,
        )
