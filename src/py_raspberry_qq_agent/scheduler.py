from __future__ import annotations

import asyncio
import calendar
import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .storage import atomic_write_json, read_json

LOGGER = logging.getLogger(__name__)
WEEKDAYS = {
    "mon": 0,
    "monday": 0,
    "周一": 0,
    "tue": 1,
    "tuesday": 1,
    "周二": 1,
    "wed": 2,
    "wednesday": 2,
    "周三": 2,
    "thu": 3,
    "thursday": 3,
    "周四": 3,
    "fri": 4,
    "friday": 4,
    "周五": 4,
    "sat": 5,
    "saturday": 5,
    "周六": 5,
    "sun": 6,
    "sunday": 6,
    "周日": 6,
    "周天": 6,
}


@dataclass(slots=True)
class ScheduledTask:
    id: str
    title: str
    content: str
    schedule: dict[str, Any]
    next_run_at: str
    enabled: bool = True
    created_at: str = ""
    updated_at: str = ""
    target_id: str = ""
    chat_type: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


class ScheduleStore:
    def __init__(self, path: Path, timezone_name: str) -> None:
        self._path = path
        self._tz = ZoneInfo(timezone_name)
        raw = read_json(path, {"format": 1, "tasks": []})
        if not isinstance(raw, dict) or not isinstance(raw.get("tasks"), list):
            raise ValueError(f"invalid schedule store: {path}")  # noqa: TRY004
        self._tasks = [ScheduledTask(**item) for item in raw["tasks"]]
        self._lock = asyncio.Lock()

    @property
    def timezone(self) -> ZoneInfo:
        return self._tz

    def now(self) -> datetime:
        return datetime.now(self._tz)

    async def create(
        self,
        *,
        title: str,
        content: str,
        schedule: dict[str, Any],
        target_id: str = "",
        chat_type: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> ScheduledTask:
        if not title.strip() or not content.strip():
            raise ValueError("scheduled task title and content are required")
        now = self.now()
        next_run = compute_next_run(schedule, now, self._tz)
        task = ScheduledTask(
            id=f"task-{uuid.uuid4().hex[:10]}",
            title=title.strip()[:120],
            content=content.strip(),
            schedule=schedule,
            next_run_at=next_run.isoformat(),
            created_at=now.isoformat(),
            updated_at=now.isoformat(),
            target_id=target_id,
            chat_type=chat_type,
            metadata=metadata or {},
        )
        async with self._lock:
            self._tasks.append(task)
            self._save()
        return task

    async def list(self) -> list[ScheduledTask]:
        async with self._lock:
            return sorted(self._tasks, key=lambda item: item.next_run_at)

    async def get(self, task_id: str) -> ScheduledTask | None:
        async with self._lock:
            return next((task for task in self._tasks if task.id == task_id), None)

    async def update(
        self,
        task_id: str,
        *,
        title: str | None = None,
        content: str | None = None,
        schedule: dict[str, Any] | None = None,
        enabled: bool | None = None,
        target_id: str | None = None,
        chat_type: str | None = None,
    ) -> ScheduledTask | None:
        async with self._lock:
            task = next((item for item in self._tasks if item.id == task_id), None)
            if task is None:
                return None
            now = self.now()
            if title is not None:
                if not title.strip():
                    raise ValueError("scheduled task title cannot be empty")
                task.title = title.strip()[:120]
            if content is not None:
                if not content.strip():
                    raise ValueError("scheduled task content cannot be empty")
                task.content = content.strip()
            if target_id is not None:
                task.target_id = target_id
            if chat_type is not None:
                task.chat_type = chat_type
            if enabled is not None:
                task.enabled = enabled
            if schedule is not None:
                task.schedule = schedule
                task.next_run_at = compute_next_run(schedule, now, self._tz).isoformat()
            elif enabled is True and parse_datetime(task.next_run_at) <= now:
                task.next_run_at = compute_next_run(
                    task.schedule, now, self._tz
                ).isoformat()
            task.updated_at = now.isoformat()
            self._save()
            return task

    async def delete(self, task_id: str) -> ScheduledTask | None:
        async with self._lock:
            for index, task in enumerate(self._tasks):
                if task.id == task_id:
                    del self._tasks[index]
                    self._save()
                    return task
            return None

    async def claim_due(self, now: datetime) -> list[ScheduledTask]:
        due: list[ScheduledTask] = []
        async with self._lock:
            for task in list(self._tasks):
                if not task.enabled or parse_datetime(task.next_run_at) > now:
                    continue
                fired = ScheduledTask(**asdict(task))
                due.append(fired)
                if task.schedule.get("kind") == "once":
                    self._tasks.remove(task)
                else:
                    task.next_run_at = compute_next_run(
                        task.schedule, now, self._tz
                    ).isoformat()
                    task.updated_at = now.isoformat()
            if due:
                self._save()
        return due

    def _save(self) -> None:
        atomic_write_json(
            self._path,
            {"format": 1, "tasks": [asdict(task) for task in self._tasks]},
        )


class Scheduler:
    def __init__(
        self,
        store: ScheduleStore,
        on_due: Callable[[ScheduledTask], Awaitable[None]],
        tick_seconds: float,
    ) -> None:
        self.store = store
        self._on_due = on_due
        self._tick_seconds = tick_seconds
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="scheduler")

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            await self._task
            self._task = None

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                for task in await self.store.claim_due(self.store.now()):
                    try:
                        LOGGER.info(
                            "scheduled task fired id=%s target=%s",
                            task.id,
                            task.target_id or "-",
                        )
                        await self._on_due(task)
                    except Exception:
                        LOGGER.exception(
                            "scheduled task callback failed id=%s", task.id
                        )
            except asyncio.CancelledError:
                raise
            except Exception:
                LOGGER.exception("scheduler tick failed")
            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=self._tick_seconds
                )
            except TimeoutError:
                pass


def parse_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def compute_next_run(
    schedule: dict[str, Any],
    after: datetime,
    tz: ZoneInfo,
) -> datetime:
    after = after.astimezone(tz)
    kind = str(schedule.get("kind", "")).lower()

    if kind == "once":
        at = datetime.fromisoformat(str(schedule.get("at", "")))
        if at.tzinfo is None:
            at = at.replace(tzinfo=tz)
        at = at.astimezone(tz)
        if at <= after:
            raise ValueError("one-time schedule must be in the future")
        return at

    if kind == "interval":
        seconds = _interval_seconds(schedule)
        return after + timedelta(seconds=seconds)

    hour, minute = _parse_time(str(schedule.get("time", "")))

    if kind == "daily":
        candidate = after.replace(hour=hour, minute=minute, second=0, microsecond=0)
        return candidate if candidate > after else candidate + timedelta(days=1)

    if kind == "weekly":
        weekdays = _parse_weekdays(schedule.get("weekdays"))
        for offset in range(8):
            day = (after + timedelta(days=offset)).date()
            candidate = datetime.combine(day, datetime.min.time(), tz).replace(
                hour=hour, minute=minute
            )
            if day.weekday() in weekdays and candidate > after:
                return candidate

    if kind == "monthly":
        days = sorted(
            {
                int(value)
                for value in schedule.get("days_of_month", [])
                if 1 <= int(value) <= 31
            }
        )
        if not days:
            raise ValueError("monthly schedule requires days_of_month")
        year, month = after.year, after.month
        for month_offset in range(25):
            candidate_month = month + month_offset
            candidate_year = year + (candidate_month - 1) // 12
            candidate_month = (candidate_month - 1) % 12 + 1
            month_last = calendar.monthrange(candidate_year, candidate_month)[1]
            for day_number in days:
                if day_number > month_last:
                    continue
                candidate = datetime(
                    candidate_year,
                    candidate_month,
                    day_number,
                    hour,
                    minute,
                    tzinfo=tz,
                )
                if candidate > after:
                    return candidate

    if kind == "yearly":
        month = int(schedule.get("month", 0))
        day = int(schedule.get("day", 0))
        if not 1 <= month <= 12 or not 1 <= day <= 31:
            raise ValueError("yearly schedule requires valid month and day")
        for year_offset in range(9):
            try:
                candidate = datetime(
                    after.year + year_offset,
                    month,
                    day,
                    hour,
                    minute,
                    tzinfo=tz,
                )
            except ValueError:
                continue
            if candidate > after:
                return candidate

    raise ValueError(
        "schedule.kind must be once, interval, daily, weekly, monthly, or yearly"
    )


def _interval_seconds(schedule: dict[str, Any]) -> float:
    seconds = float(schedule.get("every_seconds", 0))
    seconds += float(schedule.get("every_minutes", 0)) * 60
    seconds += float(schedule.get("every_hours", 0)) * 3600
    seconds += float(schedule.get("every_days", 0)) * 86400
    if seconds < 1:
        raise ValueError("interval schedule requires a positive interval")
    return seconds


def _parse_time(value: str) -> tuple[int, int]:
    try:
        hour_text, minute_text = value.split(":", 1)
        hour, minute = int(hour_text), int(minute_text)
    except (ValueError, TypeError) as exc:
        raise ValueError("schedule time must use HH:MM") from exc
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError("schedule time is out of range")
    return hour, minute


def _parse_weekdays(value: Any) -> set[int]:
    if not isinstance(value, list) or not value:
        raise ValueError("weekly schedule requires a non-empty weekdays list")
    output: set[int] = set()
    for raw in value:
        if isinstance(raw, int) and 0 <= raw <= 6:
            output.add(raw)
            continue
        text = str(raw).strip().lower()
        if text in WEEKDAYS:
            output.add(WEEKDAYS[text])
            continue
        raise ValueError(f"unknown weekday: {raw!r}")
    return output
