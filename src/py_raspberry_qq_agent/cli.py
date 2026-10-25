from __future__ import annotations

import argparse
import asyncio
import json
import logging
import signal
from pathlib import Path

from .app import Application
from .config import AppConfig, ConfigError
from .contacts import ContactDirectory, RoleStore
from .logging_setup import configure_logging


def main() -> None:
    parser = argparse.ArgumentParser(prog="py-raspberry-qq-agent")
    parser.add_argument(
        "--config",
        default="config.yaml",
        help="YAML configuration path (default: config.yaml)",
    )
    subparsers = parser.add_subparsers(dest="command")

    subparsers.add_parser("run", help="Run the QQ agent")
    subparsers.add_parser("config-check", help="Validate the configuration")

    admin = subparsers.add_parser("admin", help="Manage admin authorization")
    admin_sub = admin.add_subparsers(dest="admin_action", required=True)
    add_parser = admin_sub.add_parser("add", help="Grant admin to a QQ number")
    add_parser.add_argument("--qq", required=True)
    remove_parser = admin_sub.add_parser(
        "remove", help="Revoke admin from a QQ number"
    )
    remove_parser.add_argument("--qq", required=True)
    admin_sub.add_parser("list", help="List admin QQ numbers")

    profile = subparsers.add_parser(
        "profile", help="Manage contact profiles, including non-friends"
    )
    profile_sub = profile.add_subparsers(dest="profile_action", required=True)
    profile_set = profile_sub.add_parser("set", help="Create or update a profile")
    profile_set.add_argument("--qq", required=True)
    profile_set.add_argument("--name")
    profile_set.add_argument("--notes")
    profile_get = profile_sub.add_parser("get", help="Show a profile")
    profile_get.add_argument("--qq", required=True)
    profile_remove = profile_sub.add_parser("remove", help="Remove a profile")
    profile_remove.add_argument("--qq", required=True)
    profile_list = profile_sub.add_parser("list", help="List profiles with pagination")
    profile_list.add_argument("--search", default="")
    profile_list.add_argument("--page", type=int, default=1)
    profile_list.add_argument("--page-size", type=int)

    args = parser.parse_args()
    try:
        config = AppConfig.load(Path(args.config))
        config.prepare_directories()
    except ConfigError as exc:
        parser.error(str(exc))
        return

    command = args.command or "run"
    if command == "config-check":
        configure_logging(config)
        print(f"configuration OK: {config.config_path}")
        return
    if command == "admin":
        configure_logging(config)
        roles = RoleStore(config.data_dir / "roles.json")
        if args.admin_action in {"add", "remove"}:
            qq_id = args.qq
            roles.set_admin(qq_id, args.admin_action == "add")
            action = "granted" if args.admin_action == "add" else "revoked"
            print(f"{action} admin for {qq_id}")
        else:
            for role in roles.list_roles():
                print(role["contact_id"])
        return
    if command == "profile":
        contacts = ContactDirectory(config.data_dir / "profiles.json")
        if args.profile_action == "set":
            if args.name is None and args.notes is None:
                parser.error("profile set requires --name and/or --notes")
            qq_id = args.qq
            profile = contacts.update(qq_id, args.name, args.notes)
            print(json.dumps(profile, ensure_ascii=False, indent=2))
        elif args.profile_action == "get":
            qq_id = args.qq
            print(json.dumps(contacts.get(qq_id), ensure_ascii=False, indent=2))
        elif args.profile_action == "remove":
            qq_id = args.qq
            removed = contacts.remove(qq_id)
            print(f"profile removed={str(removed).lower()} {qq_id}")
        else:
            page_size = args.page_size or config.directory_default_page_size
            page_size = min(
                max(page_size, 1),
                config.directory_max_page_size,
            )
            result = contacts.page(
                query=args.search,
                page=args.page,
                page_size=page_size,
            )
            print(json.dumps(result, ensure_ascii=False, indent=2))
        return

    try:
        asyncio.run(_run(config))
    except KeyboardInterrupt:
        logging.getLogger(__name__).info("interrupted")


async def _run(config: AppConfig) -> None:
    application = Application.build(config)
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signal_name in ("SIGINT", "SIGTERM"):
        signum = getattr(signal, signal_name, None)
        if signum is None:
            continue
        try:
            loop.add_signal_handler(signum, stop_event.set)
        except (NotImplementedError, RuntimeError):
            pass
    try:
        await application.start()
        await stop_event.wait()
    finally:
        await application.stop()


if __name__ == "__main__":
    main()
