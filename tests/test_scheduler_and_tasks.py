from __future__ import annotations

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from py_raspberry_qq_agent.scheduler import ScheduleStore, compute_next_run
from py_raspberry_qq_agent.tasks import TaskQueue


def test_schedule_types_find_future_occurrences() -> None:
    tz = ZoneInfo("Asia/Shanghai")
    now = datetime(2026, 10, 23, 10, 30, tzinfo=tz)

    daily = compute_next_run({"kind": "daily", "time": "11:00"}, now, tz)
    assert daily == datetime(2026, 10, 23, 11, 0, tzinfo=tz)

    weekly = compute_next_run(
        {"kind": "weekly", "weekdays": ["周日"], "time": "09:00"},
        now,
        tz,
    )
    assert weekly == datetime(2026, 10, 25, 9, 0, tzinfo=tz)

    monthly = compute_next_run(
        {"kind": "monthly", "days_of_month": [31], "time": "08:00"},
        now,
        tz,
    )
    assert monthly == datetime(2026, 10, 31, 8, 0, tzinfo=tz)

    yearly = compute_next_run(
        {"kind": "yearly", "month": 1, "day": 2, "time": "07:00"},
        now,
        tz,
    )
    assert yearly == datetime(2027, 1, 2, 7, 0, tzinfo=tz)

    interval = compute_next_run(
        {"kind": "interval", "every_minutes": 15}, now, tz
    )
    assert interval == now.replace(minute=45)


@pytest.mark.asyncio
async def test_schedule_store_persists_and_allows_modification(tmp_path: Path) -> None:
    path = tmp_path / "schedules.json"
    store = ScheduleStore(path, "Asia/Shanghai")
    task = await store.create(
        title="daily report",
        content="summarize",
        schedule={"kind": "daily", "time": "12:00"},
        chat_type="group",
        target_id="qq-group:test",
    )
    assert task.next_run_at

    reopened = ScheduleStore(path, "Asia/Shanghai")
    loaded = await reopened.get(task.id)
    assert loaded is not None
    assert loaded.title == "daily report"

    updated = await reopened.update(task.id, title="updated")
    assert updated is not None and updated.title == "updated"
    deleted = await reopened.delete(task.id)
    assert deleted is not None
    assert await reopened.list() == []


@pytest.mark.asyncio
async def test_task_can_have_no_chat_and_does_not_survive_queue_recreation() -> None:
    queue = TaskQueue()
    await queue.add("generate a report")
    assert (await queue.get()).target_id == ""
    assert TaskQueue().empty()
