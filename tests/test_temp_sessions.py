from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace
from typing import Any

import pytest

import py_raspberry_qq_agent.napcat as napcat_module
from py_raspberry_qq_agent.config import AppConfig, ConfigError
from py_raspberry_qq_agent.napcat import NapCatClient, NapCatError


class StubNapCat(NapCatClient):
    def __init__(self, config: AppConfig) -> None:
        async def on_message(message: Any) -> None:
            pass

        super().__init__(config, on_message)
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.friends: Any = []
        self.groups: Any = [{"group_id": 111}, {"group_id": 222}]
        self.member_lists: dict[int, Any] = {
            111: [{"user_id": 99999}],
            222: [
                {"user_id": user_id}
                for user_id in [12345, 54321, 22334, 33445, 44556]
            ],
        }
        self.member_info: dict[tuple[int, int], Any] = {}
        self.user: dict[str, Any] | None = None
        self.send_error: Exception | None = None

    async def call_action(
        self, action: str, params: dict[str, Any], *, timeout: float | None = None
    ) -> dict[str, Any]:
        self.calls.append((action, params.copy()))
        if action in {"get_friend_list", "get_group_list", "get_group_member_list"}:
            if action == "get_friend_list":
                data = self.friends
            elif action == "get_group_list":
                data = self.groups
            else:
                data = self.member_lists[params["group_id"]]
            if isinstance(data, Exception):
                raise data
            return {"data": data}
        if action == "get_group_member_info":
            key = params["group_id"], params["user_id"]
            if key in self.member_info:
                data = self.member_info[key]
                if isinstance(data, Exception):
                    raise data
                return data
            members = self.member_lists.get(key[0], [])
            if isinstance(members, list) and {"user_id": key[1]} in members:
                return {"group_id": key[0], "user_id": key[1]}
            raise NapCatError("member not found")
        if action == "get_stranger_info":
            if self.user is not None:
                return self.user
            return {"user_id": params["user_id"], "uid": f"u_{params['user_id']}"}
        if action == "send_private_msg":
            if self.send_error:
                raise self.send_error
            return {"message_id": 999}
        raise AssertionError(f"Unexpected action: {action}")


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    ticks = [100.0]
    # Replace this module's clock without changing asyncio's monotonic clock.
    monkeypatch.setattr(
        napcat_module, "time",
        SimpleNamespace(monotonic=lambda: ticks[0], time=time.time),
    )
    return ticks


def calls(client: StubNapCat, action: str) -> list[dict[str, Any]]:
    return [params for name, params in client.calls if name == action]


def test_cache_config_defaults_and_overrides(config: AppConfig) -> None:
    assert not config.napcat_allow_temp_session
    assert config.napcat_cache_ttl_seconds == 7200
    with config.config_path.open("a") as handle:
        handle.write('\nnapcat:\n  allow_temp_session: "true"\n  cache_ttl_seconds: 3600\n')
    loaded = AppConfig.load(config.config_path)
    assert loaded.napcat_allow_temp_session
    assert loaded.napcat_cache_ttl_seconds == 3600


@pytest.mark.parametrize("ttl", [0, -1, ".nan", ".inf"])
def test_invalid_cache_ttl_is_rejected(config: AppConfig, ttl: Any) -> None:
    with config.config_path.open("a") as handle:
        handle.write(f"\nnapcat:\n  cache_ttl_seconds: {ttl}\n")
    with pytest.raises(ConfigError, match="positive and finite"):
        AppConfig.load(config.config_path)


@pytest.mark.asyncio
async def test_disabled_preserves_direct_send_without_discovery(config: AppConfig) -> None:
    client = StubNapCat(config)
    await client.send_private("12345", "hello")
    assert client.calls == [
        ("send_private_msg", {"user_id": 12345, "message": "hello"})
    ]


@pytest.mark.asyncio
async def test_friend_cache_has_fixed_two_hour_expiry(
    config: AppConfig, clock: list[float]
) -> None:
    config.napcat_allow_temp_session = True
    client = StubNapCat(config)
    client.friends = [{"user_id": 12345}]
    await client.send_private("12345", "first")
    clock[0] += 7199
    await client.send_private("12345", "still cached")
    assert len(calls(client, "get_friend_list")) == 1
    clock[0] += 1
    await client.send_private("12345", "expired")
    assert len(calls(client, "get_friend_list")) == 2
    assert not calls(client, "get_group_list")
    assert all("group_id" not in p for p in calls(client, "send_private_msg"))


@pytest.mark.asyncio
async def test_new_friendship_is_used_after_cache_expiry(
    config: AppConfig, clock: list[float]
) -> None:
    config.napcat_allow_temp_session = True
    client = StubNapCat(config)
    await client.send_private("12345", "temporary")
    client.friends = [{"user_id": 12345}]
    clock[0] += 7200
    await client.send_private("12345", "now a friend")
    assert "group_id" not in calls(client, "send_private_msg")[-1]


@pytest.mark.asyncio
async def test_five_recipients_share_full_member_snapshots(config: AppConfig) -> None:
    config.napcat_allow_temp_session = True
    client = StubNapCat(config)
    for user_id in ["12345", "54321", "22334", "33445", "44556"]:
        await client.send_private(user_id, "notification")
    assert len(calls(client, "get_friend_list")) == 1
    assert len(calls(client, "get_group_list")) == 1
    assert [p["group_id"] for p in calls(client, "get_group_member_list")] == [111, 222]
    assert len(calls(client, "get_group_member_info")) == 5
    assert all(p["no_cache"] is True for p in calls(client, "get_group_member_info"))
    assert len(calls(client, "get_stranger_info")) == 5
    assert len(calls(client, "send_private_msg")) == 5
    assert all(p["group_id"] == 222 for p in calls(client, "send_private_msg"))


@pytest.mark.asyncio
async def test_known_user_skips_unscanned_groups(config: AppConfig) -> None:
    config.napcat_allow_temp_session = True
    client = StubNapCat(config)
    client.groups.append({"group_id": 333})
    await client.send_private("12345", "first")
    await client.send_private("54321", "second")
    assert [p["group_id"] for p in calls(client, "get_group_member_list")] == [111, 222]


@pytest.mark.asyncio
async def test_unknown_user_continues_from_unscanned_groups(config: AppConfig) -> None:
    config.napcat_allow_temp_session = True
    client = StubNapCat(config)
    client.groups.append({"group_id": 333})
    client.member_lists[333] = [{"user_id": 77777}]
    await client.send_private("12345", "first")
    await client.send_private("77777", "different group")
    assert [p["group_id"] for p in calls(client, "get_group_member_list")] == [111, 222, 333]
    assert calls(client, "send_private_msg")[-1]["group_id"] == 333


@pytest.mark.asyncio
async def test_member_hits_do_not_extend_expiry(
    config: AppConfig, clock: list[float]
) -> None:
    config.napcat_allow_temp_session = True
    client = StubNapCat(config)
    await client.send_private("12345", "first")
    clock[0] += 3600
    await client.send_private("54321", "hit")
    assert len(calls(client, "get_group_member_list")) == 2
    clock[0] += 3600
    await client.send_private("12345", "expired")
    assert len(calls(client, "get_group_list")) == 2
    assert [p["group_id"] for p in calls(client, "get_group_member_list")] == [111, 222, 111, 222]


@pytest.mark.asyncio
async def test_groups_expire_independently(
    config: AppConfig, clock: list[float]
) -> None:
    config.napcat_allow_temp_session = True
    client = StubNapCat(config)
    client.member_lists[111] = [{"user_id": 12345}]
    await client.send_private("12345", "first group")
    clock[0] += 100
    await client.send_private("54321", "second group")
    clock[0] += 7100
    await client.send_private("54321", "second group still valid")
    assert set(client._group_members) == {"222"}
    assert [p["group_id"] for p in calls(client, "get_group_member_list")] == [111, 222]


@pytest.mark.asyncio
async def test_refreshed_group_list_removes_departed_groups(
    config: AppConfig, clock: list[float]
) -> None:
    config.napcat_allow_temp_session = True
    client = StubNapCat(config)
    await client.send_private("12345", "first")
    client.groups = [{"group_id": 111}]
    client._group_ids_expires_at = clock[0]
    with pytest.raises(NapCatError, match="No verified common group"):
        await client.send_private("12345", "left group")
    assert "222" not in client._group_members
    assert len(calls(client, "send_private_msg")) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_groups", [None, {}, [None], [{"group_id": "bad"}], NapCatError("unavailable")])
async def test_failed_group_refresh_does_not_clear_old_snapshot(
    config: AppConfig, clock: list[float], bad_groups: Any
) -> None:
    config.napcat_allow_temp_session = True
    client = StubNapCat(config)
    await client.send_private("12345", "first")
    client.groups = bad_groups
    client._group_ids_expires_at = clock[0]
    with pytest.raises(NapCatError):
        await client.send_private("12345", "refresh failed")
    assert client._group_ids == {"111", "222"}
    assert "222" in client._group_members
    assert len(calls(client, "send_private_msg")) == 1


@pytest.mark.asyncio
async def test_valid_empty_group_list_clears_snapshots(
    config: AppConfig, clock: list[float]
) -> None:
    config.napcat_allow_temp_session = True
    client = StubNapCat(config)
    await client.send_private("12345", "first")
    client.groups = []
    client._group_ids_expires_at = clock[0]
    with pytest.raises(NapCatError, match="No verified common group"):
        await client.send_private("12345", "no groups")
    assert not client._group_members


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_members", [None, {}, [None], [{"user_id": "bad"}], [{"user_id": 12345, "group_id": 999}], NapCatError("unavailable"), TimeoutError()])
async def test_failed_member_load_is_not_cached_and_does_not_block_other_groups(
    config: AppConfig, bad_members: Any
) -> None:
    config.napcat_allow_temp_session = True
    client = StubNapCat(config)
    client.member_lists[111] = bad_members
    await client.send_private("12345", "second group is usable")
    assert "111" not in client._group_members
    assert calls(client, "send_private_msg")[0]["group_id"] == 222
    with pytest.raises(NapCatError, match="lookup incomplete"):
        await client.send_private("66666", "not found; first group retries")
    assert len([p for p in calls(client, "get_group_member_list") if p["group_id"] == 111]) == 2


@pytest.mark.asyncio
async def test_successful_empty_member_list_is_cached(config: AppConfig) -> None:
    config.napcat_allow_temp_session = True
    client = StubNapCat(config)
    client.member_lists[111] = []
    await client.send_private("12345", "first")
    await client.send_private("54321", "second")
    assert client._group_members["111"][1] == set()
    assert len(calls(client, "get_group_member_list")) == 2


@pytest.mark.asyncio
async def test_failed_candidate_verification_tries_another_cached_group(config: AppConfig) -> None:
    config.napcat_allow_temp_session = True
    client = StubNapCat(config)
    client.member_lists[111] = [{"user_id": 12345}]
    client.member_info[(111, 12345)] = NapCatError("verification unavailable")
    await client.send_private("12345", "use second group")
    assert calls(client, "send_private_msg")[0]["group_id"] == 222
    assert len(calls(client, "get_group_member_info")) == 2


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
    assert not calls(client, "send_private_msg")


@pytest.mark.asyncio
async def test_send_failure_never_tries_a_second_delivery(config: AppConfig) -> None:
    config.napcat_allow_temp_session = True
    client = StubNapCat(config)
    client.member_lists[111] = [{"user_id": 12345}]
    client.send_error = NapCatError("retcode=1200 result=16 delivery refused")
    with pytest.raises(NapCatError, match="result=16"):
        await client.send_private("12345", "notification")
    assert len(calls(client, "send_private_msg")) == 1
    assert [p["group_id"] for p in calls(client, "get_group_member_list")] == [111]


@pytest.mark.asyncio
async def test_discovery_timeout_preserves_completed_snapshots(
    config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    config.napcat_allow_temp_session = True
    config.napcat_request_timeout_seconds = 0.02
    client = StubNapCat(config)
    original = client.call_action

    async def stalled(action: str, params: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        if action == "get_group_member_list" and params["group_id"] == 222:
            await asyncio.Event().wait()
        return await original(action, params, **kwargs)

    monkeypatch.setattr(client, "call_action", stalled)
    with pytest.raises(TimeoutError):
        await client.send_private("12345", "notification")
    assert set(client._group_members) == {"111"}
    assert not calls(client, "send_private_msg")
    assert not client._discovery_lock.locked()


@pytest.mark.asyncio
async def test_discovery_has_a_separate_budget_from_delivery(
    config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    config.napcat_allow_temp_session = True
    config.napcat_request_timeout_seconds = 1
    config.outbound_send_timeout_seconds = 0.01
    client = StubNapCat(config)
    original = client.call_action

    async def slow_query(action: str, params: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        if action == "get_group_member_list":
            await asyncio.sleep(0.02)
        return await original(action, params, **kwargs)

    monkeypatch.setattr(client, "call_action", slow_query)
    await client.send_private("12345", "notification")
    assert len(calls(client, "send_private_msg")) == 1


@pytest.mark.asyncio
async def test_concurrent_callers_reuse_completed_loads(
    config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    config.napcat_allow_temp_session = True
    client = StubNapCat(config)
    original = client.call_action

    async def yielding(action: str, params: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        await asyncio.sleep(0)
        return await original(action, params, **kwargs)

    monkeypatch.setattr(client, "call_action", yielding)
    await asyncio.gather(
        client.send_private("12345", "first"),
        client.send_private("54321", "second"),
    )
    assert len(calls(client, "get_friend_list")) == 1
    assert len(calls(client, "get_group_list")) == 1
    assert len(calls(client, "get_group_member_list")) == 2


@pytest.mark.asyncio
async def test_account_change_discards_inflight_results(
    config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    config.napcat_allow_temp_session = True
    client = StubNapCat(config)
    client._self_id = "90001"
    original = client.call_action

    async def changed_account(action: str, params: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        result = await original(action, params, **kwargs)
        if action == "get_group_member_list":
            await client._handle_raw(json.dumps({"post_type": "meta_event", "self_id": 90002}))
        return result

    monkeypatch.setattr(client, "call_action", changed_account)
    with pytest.raises(NapCatError, match="account changed"):
        await client.send_private("12345", "notification")
    assert not client._friend_ids
    assert not client._group_ids
    assert not client._group_members
    assert not calls(client, "send_private_msg")


@pytest.mark.asyncio
async def test_disconnect_clears_caches(
    config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    config.napcat_allow_temp_session = True
    client = StubNapCat(config)
    await client.send_private("12345", "populate caches")

    class Socket:
        def __aiter__(self):
            return self

        async def __anext__(self):
            client._stop.set()
            raise StopAsyncIteration

    class Connection:
        async def __aenter__(self):
            return Socket()

        async def __aexit__(self, *args):
            pass

    monkeypatch.setattr(napcat_module, "connect", lambda *args, **kwargs: Connection())
    await client._run()
    assert not client._friend_ids
    assert not client._group_ids
    assert not client._group_members


@pytest.mark.asyncio
@pytest.mark.parametrize("retcode", [0, 1200])
async def test_call_action_preserves_failed_response_details(
    config: AppConfig, retcode: int
) -> None:
    async def on_message(message: Any) -> None:
        pass

    client = NapCatClient(config, on_message)

    class ResponseSocket:
        async def send(self, raw: str) -> None:
            request = json.loads(raw)
            await client._handle_raw(json.dumps({
                "echo": request["echo"], "retcode": retcode, "status": "failed",
                "message": "", "wording": "result=16: add friend first",
            }))

    client._ws = ResponseSocket()  # type: ignore[assignment]
    client._connected.set()
    with pytest.raises(NapCatError, match=f"retcode={retcode}.*result=16"):
        await client.call_action("send_private_msg", {"user_id": 12345, "message": "test"})
