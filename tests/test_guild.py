from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from py_raspberry_qq_agent.guild import DueReminders, GuildConfig, WindowPoller
from py_raspberry_qq_agent.keycloak import KeycloakConfig, KeycloakDirectory, User


@pytest.mark.asyncio
async def test_windows_start_now_and_keep_cursor_after_query_failure() -> None:
    start = datetime(2026, 10, 25, tzinfo=UTC)
    repository = AsyncMock()
    repository.now.side_effect = [start + timedelta(minutes=i) for i in range(4)]
    handler = AsyncMock(side_effect=[RuntimeError("database unavailable"), None, None])
    poller = WindowPoller(repository, 60, handler, "test")
    await poller.tick()
    handler.assert_not_awaited()
    with pytest.raises(RuntimeError):
        await poller.tick()
    await poller.tick()
    await poller.tick()
    assert handler.call_args_list[1].args == (start, start + timedelta(minutes=2))
    assert handler.call_args_list[2].args == (
        start + timedelta(minutes=2),
        start + timedelta(minutes=3),
    )


@pytest.mark.asyncio
async def test_due_uses_one_day_trigger_and_continues_past_unbound_or_failed_send() -> (
    None
):
    start = datetime(2026, 10, 25, tzinfo=UTC)
    repository = AsyncMock()
    repository.due.return_value = [
        {
            "id": i,
            "title": "Task",
            "due_at": start + timedelta(days=1),
            "subject": str(i),
        }
        for i in range(3)
    ]
    directory = AsyncMock()
    directory.user.side_effect = [
        User("0", "Unbound", None),
        User("1", "Broken", "12345"),
        User("2", "Works", "54321"),
    ]
    outbound = Mock()
    outbound.enqueue.side_effect = [RuntimeError("full"), None]
    service = DueReminders(
        GuildConfig(public_url="https://guild.example.com"),
        repository,
        directory,
        outbound,
        UTC,
    )
    end = start + timedelta(minutes=1)
    await service.check(start, end)
    repository.due.assert_awaited_once_with(start, end, timedelta(days=1))
    assert outbound.enqueue.call_count == 2
    assert outbound.enqueue.call_args.args[1] == "54321"
    assert "https://guild.example.com/nodes/2" in outbound.enqueue.call_args.args[2]


@pytest.mark.asyncio
async def test_keycloak_read_only_binding_cache_disabled_user_and_token_refresh() -> (
    None
):
    counts = {"token": 0, "users": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/token"):
            counts["token"] += 1
            return httpx.Response(
                200,
                json={
                    "access_token": "token-" + str(counts["token"]),
                    "expires_in": 300,
                },
            )
        assert request.method == "GET"
        counts["users"] += 1
        if counts["users"] == 1:
            return httpx.Response(401)
        if request.url.path.endswith("/disabled"):
            return httpx.Response(
                200, json={"enabled": False, "attributes": {"qq_number": ["99999"]}}
            )
        if request.url.path.endswith("/ambiguous"):
            return httpx.Response(
                200,
                json={
                    "username": "User",
                    "attributes": {"qq_number": ["12345", "54321"]},
                },
            )
        return httpx.Response(
            200,
            json={
                "username": "Alice",
                "enabled": True,
                "attributes": {"qq_number": ["12345"]},
            },
        )

    directory = KeycloakDirectory(
        KeycloakConfig(
            issuer="https://id.example/realms/test",
            admin_url="https://id.example/admin/realms/test",
        )
    )
    await directory._http.aclose()
    directory._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        assert (await directory.user("alice")).qq == "12345"
        assert (await directory.user("alice")).name == "Alice"
        assert counts == {"token": 2, "users": 2}
        assert await directory.user("disabled") is None
        assert (await directory.user("ambiguous")).qq is None
    finally:
        await directory.close()
