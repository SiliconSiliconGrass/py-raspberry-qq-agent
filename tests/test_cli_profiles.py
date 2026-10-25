from __future__ import annotations

import json
import sys

from py_raspberry_qq_agent.cli import main
from py_raspberry_qq_agent.config import AppConfig


def test_profile_cli_supports_non_friend_accounts(
    config: AppConfig,
    monkeypatch,
    capsys,
) -> None:
    raw_qq = "456789012"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "py-raspberry-qq-agent",
            "--config",
            str(config.config_path),
            "profile",
            "set",
            "--qq",
            raw_qq,
            "--name",
            "Non Friend",
            "--notes",
            "Colleague in another group",
        ],
    )
    main()
    set_output = capsys.readouterr().out
    profile = json.loads(set_output)
    assert profile["display_name"] == "Non Friend"
    assert profile["contact_id"] == raw_qq
    assert raw_qq in set_output

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "py-raspberry-qq-agent",
            "--config",
            str(config.config_path),
            "profile",
            "list",
            "--search",
            "another group",
        ],
    )
    main()
    list_output = capsys.readouterr().out
    page = json.loads(list_output)
    assert page["total"] == 1
    assert page["items"][0]["display_name"] == "Non Friend"
    assert raw_qq in list_output
