from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta

import psycopg
import pytest
import pytest_asyncio

from py_raspberry_qq_agent.guild import GuildRepository


@pytest_asyncio.fixture
async def database():
    dsn = os.environ.get("TEST_GUILD_DATABASE_URL")
    if not dsn:
        pytest.skip(
            "TEST_GUILD_DATABASE_URL is not set (requires a migrated test database)"
        )
    subject = "bot-test-" + uuid.uuid4().hex
    async with await psycopg.AsyncConnection.connect(dsn, autocommit=True) as conn:
        await conn.execute("INSERT INTO users(subject) VALUES (%s)", (subject,))
        try:
            yield conn, GuildRepository(dsn), subject
        finally:
            await conn.execute("DELETE FROM nodes WHERE created_by=%s", (subject,))
            await conn.execute("DELETE FROM users WHERE subject=%s", (subject,))


@pytest.mark.asyncio
async def test_due_query_is_half_open_and_excludes_completed_members_and_overdue(
    database,
) -> None:
    conn, repository, subject = database
    start = datetime(2026, 10, 25, 12, tzinfo=UTC)
    end = start + timedelta(minutes=1)
    advance = timedelta(days=1)
    ids = []
    for index, (due, completed, role) in enumerate(
        [
            (start + advance, None, "leader"),
            (end + advance, None, "leader"),
            (start + advance, start, "leader"),
            (start + advance, None, "member"),
            (start - timedelta(days=1), None, "leader"),
        ]
    ):
        cursor = await conn.execute(
            "INSERT INTO nodes(title,due_at,completed_at,created_by) VALUES (%s,%s,%s,%s) RETURNING id",
            ("Window test " + str(index), due, completed, subject),
        )
        node_id = (await cursor.fetchone())[0]
        ids.append(node_id)
        await conn.execute(
            "INSERT INTO node_members(node_id,subject,role) VALUES (%s,%s,%s)",
            (node_id, subject, role),
        )
    first = await repository.due(start, end, advance)
    second = await repository.due(end, end + timedelta(minutes=1), advance)
    assert [row["id"] for row in first if row["subject"] == subject] == [ids[0]]
    assert [row["id"] for row in second if row["subject"] == subject] == [ids[1]]
    # Even a delayed query never turns into an overdue reminder.
    delayed = await repository.due(start - timedelta(days=3), end, advance)
    assert ids[4] not in [row["id"] for row in delayed]
