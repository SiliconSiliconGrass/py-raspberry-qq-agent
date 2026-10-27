from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import pytest

from py_raspberry_qq_agent.config import AppConfig
from py_raspberry_qq_agent.napcat import NapCatClient, NapCatError


class StubNapCat(NapCatClient):
    def __init__(self, config: AppConfig) -> None:
        async def on_message(message: Any) -> None:
            pass

        super().__init__(config, on_message)
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.friends: list[dict[str, Any]] = []
        self.groups = [{"group_id": 111}, {"group_id": 222}]
        self.members: dict[int, dict[str, Any] | Exception] = {
            111: NapCatError("member not found"),
            222: {"user_id": 12345},
        }
        self.user: dict[str, Any] = {"user_id": 12345, "uid": "u_target"}
        self.send_error: Exception | None = None

    async def call_action(
        self, action: str, params: dict[str, Any], *, timeout: float | None = None
    ) -> dict[str, Any]:
        self.calls.append((action, params.copy()))
        if action == "get_friend_list":
            return {"data": self.friends}
        if action == "get_group_list":
            return {"data": self.groups}
        if action == "get_group_member_info":
            member = self.members.get(params["group_id"], {})
            if isinstance(member, Exception):
                raise member
            return member
        if action == "get_stranger_info":
            return self.user
        if action == "send_private_msg":
            if self.send_error:
                raise self.send_error
            return {"message_id": 999}
        raise AssertionError(f"Unexpected action: {action}")


def private_sends(client: StubNapCat) -> list[dict[str, Any]]:
    return [params for action, params in client.calls if action == "send_private_msg"]


def test_temp_sessions_default_off_and_can_be_enabled(config: AppConfig) -> None:
    assert not config.napcat_allow_temp_session
    with config.config_path.open("a") as handle:
        handle.write('\nnapcat:\n  allow_temp_session: "true"\n')
    assert AppConfig.load(config.config_path).napcat_allow_temp_session


@pytest.mark.asyncio
async def test_disabled_preserves_direct_send_without_discovery(config: AppConfig) -> None:
    client = StubNapCat(config)
    await client.send_private("12345", "hello")
    assert client.calls == [
        ("send_private_msg", {"user_id": 12345, "message": "hello"})
    ]


@pytest.mark.asyncio
async def test_friends_use_normal_private_chat_and_cache_friend_list(config: AppConfig) -> None:
    config.napcat_allow_temp_session = True
    client = StubNapCat(config)
    client.friends = [{"user_id": 12345}]
    await client.send_private("12345", "first")
    await client.send_private("12345", "second")
    assert [action for action, _ in client.calls] == [
        "get_friend_list", "send_private_msg", "send_private_msg"
    ]
    assert all("group_id" not in params for params in private_sends(client))


@pytest.mark.asyncio
async def test_refreshed_friend_list_uses_new_friendship(config: AppConfig) -> None:
    config.napcat_allow_temp_session = True
    client = StubNapCat(config)
    await client.send_private("12345", "temporary")
    client.friends = [{"user_id": 12345}]
    client._friend_ids_expires_at = time.monotonic() - 1
    client.calls.clear()
    await client.send_private("12345", "now a friend")
    assert client.calls == [
        ("get_friend_list", {}),
        ("send_private_msg", {"user_id": 12345, "message": "now a friend"}),
    ]


@pytest.mark.asyncio
async def test_nonfriend_uses_one_verified_common_group(config: AppConfig) -> None:
    config.napcat_allow_temp_session = True
    client = StubNapCat(config)
    response = await client.send_private("12345", "notification")
    assert response == {"message_id": 999}
    assert private_sends(client) == [
        {"user_id": 12345, "group_id": 222, "message": "notification"}
    ]
    assert not any(action == "send_group_msg" for action, _ in client.calls)
    client.calls.clear()
    await client.send_private("12345", "next")
    assert [action for action, _ in client.calls] == [
        "get_group_member_info", "get_stranger_info", "send_private_msg"
    ]


@pytest.mark.asyncio
async def test_recent_group_is_preferred_but_membership_is_checked(config: AppConfig) -> None:
    config.napcat_allow_temp_session = True
    client = StubNapCat(config)
    client.members[333] = {"user_id": 12345}
    client._parse_message({
        "self_id": 90001, "user_id": 12345, "group_id": 333,
        "message_type": "group", "message": "recent group message",
    })
    await client.send_private("12345", "hello")
    assert [action for action, _ in client.calls] == [
        "get_friend_list", "get_group_member_info", "get_stranger_info", "send_private_msg"
    ]
    assert private_sends(client)[0]["group_id"] == 333


@pytest.mark.asyncio
async def test_stale_group_membership_is_not_trusted(config: AppConfig) -> None:
    config.napcat_allow_temp_session = True
    client = StubNapCat(config)
    client._remember_temp_group("12345", "111")
    await client.send_private("12345", "hello")
    assert private_sends(client)[0]["group_id"] == 222
    checked = [params["group_id"] for action, params in client.calls if action == "get_group_member_info"]
    assert checked == [111, 222]


@pytest.mark.asyncio
async def test_no_common_group_does_not_send(config: AppConfig) -> None:
    config.napcat_allow_temp_session = True
    client = StubNapCat(config)
    client.members[222] = {"user_id": 54321}
    with pytest.raises(NapCatError, match="No verified common group"):
        await client.send_private("12345", "private notification")
    assert not private_sends(client)


@pytest.mark.asyncio
@pytest.mark.parametrize("user", [{}, {"user_id": 12345}, {"user_id": 54321, "uid": "u_other"}])
async def test_unresolvable_user_cannot_trigger_group_fallback(
    config: AppConfig, user: dict[str, Any]
) -> None:
    config.napcat_allow_temp_session = True
    client = StubNapCat(config)
    client.user = user
    with pytest.raises(NapCatError, match="Cannot resolve user"):
        await client.send_private("12345", "private notification")
    assert not private_sends(client)


@pytest.mark.asyncio
async def test_send_failure_never_tries_a_second_group(config: AppConfig) -> None:
    config.napcat_allow_temp_session = True
    client = StubNapCat(config)
    client.members[111] = {"user_id": 12345}
    client.send_error = NapCatError("retcode=1200 result=16 delivery refused")
    with pytest.raises(NapCatError, match="result=16"):
        await client.send_private("12345", "notification")
    assert len(private_sends(client)) == 1
    assert not any(params.get("group_id") == 222 for _, params in client.calls)


@pytest.mark.asyncio
async def test_slow_discovery_times_out_without_sending(
    config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    config.napcat_allow_temp_session = True
    config.outbound_send_timeout_seconds = 0.01
    client = StubNapCat(config)

    async def slow_friends() -> list[dict[str, Any]]:
        await asyncio.Event().wait()
        return []

    monkeypatch.setattr(client, "fetch_friend_list", slow_friends)
    with pytest.raises(TimeoutError):
        await client.send_private("12345", "notification")
    assert not private_sends(client)


@pytest.mark.asyncio
async def test_call_action_preserves_wording_when_message_is_empty(config: AppConfig) -> None:
    async def on_message(message: Any) -> None:
        pass

    client = NapCatClient(config, on_message)

    class ResponseSocket:
        async def send(self, raw: str) -> None:
            request = json.loads(raw)
            await client._handle_raw(json.dumps({
                "echo": request["echo"], "retcode": 1200,
                "message": "", "wording": "result=16: add friend first",
            }))

    client._ws = ResponseSocket()  # type: ignore[assignment]
    client._connected.set()
    with pytest.raises(NapCatError, match="retcode=1200.*result=16"):
        await client.call_action("send_private_msg", {"user_id": 12345, "message": "test"})
