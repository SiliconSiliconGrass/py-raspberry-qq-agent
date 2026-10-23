from __future__ import annotations

import json
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from .config import AppConfig
from .contacts import ContactDirectory, ContactVault
from .knowledge import KnowledgeBase
from .mcp_runtime import McpManager
from .messages import MessageStore
from .napcat import NapCatClient, OutboundQueue
from .scheduler import ScheduleStore
from .todos import TodoStore

LOGGER = logging.getLogger(__name__)
ToolHandler = Callable[[dict[str, Any]], Awaitable[Any]]


@dataclass(slots=True)
class Tool:
    name: str
    description: str
    input_schema: dict[str, Any]
    handler: ToolHandler

    def openai_definition(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.input_schema,
            },
        }


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"duplicate tool name: {tool.name}")
        self._tools[tool.name] = tool

    def definitions(self) -> list[dict[str, Any]]:
        return [tool.openai_definition() for tool in self._tools.values()]

    async def execute(self, name: str, arguments: dict[str, Any]) -> Any:
        tool = self._tools.get(name)
        if tool is None:
            raise KeyError(f"unknown tool: {name}")
        return await tool.handler(arguments)


class HistoryService:
    def __init__(
        self,
        config: AppConfig,
        messages: MessageStore,
        napcat: NapCatClient,
        vault: ContactVault,
    ) -> None:
        self._config = config
        self._messages = messages
        self._napcat = napcat
        self._vault = vault

    async def get(
        self,
        chat_type: str,
        encrypted_target_id: str,
        *,
        limit: int,
        start_time: float | None,
        end_time: float | None,
    ) -> list[dict[str, Any]]:
        kind, raw_target = self._vault.resolve(encrypted_target_id)
        expected = "user" if chat_type == "private" else "group"
        if kind != expected:
            raise ValueError("encrypted target does not match chat_type")

        selected = await self._messages.history(
            chat_type,
            encrypted_target_id,
            limit=limit,
            start_time=start_time,
            end_time=end_time,
        )
        try:
            remote = await self._napcat.fetch_history(chat_type, raw_target, limit)
            selected = self._merge(
                selected,
                self._sanitize_remote(remote, chat_type, encrypted_target_id),
                start_time,
                end_time,
                limit,
            )
        except Exception as exc:  # noqa: BLE001 - local history remains usable
            LOGGER.warning(
                "NapCat history unavailable chat=%s target=%s error=%s",
                chat_type,
                encrypted_target_id,
                type(exc).__name__,
            )

        output = []
        for item in selected:
            output.append(
                {
                    "timestamp": datetime.fromtimestamp(
                        float(item.get("timestamp", 0)),
                        self._timezone(),
                    ).isoformat(),
                    "sender": item.get("encrypted_sender_id", ""),
                    "sender_name": item.get("sender_name", ""),
                    "content": item.get("content", ""),
                    "message_id": item.get("message_id", ""),
                    "mentioned_bot": bool(item.get("mentioned_bot", False)),
                }
            )
        return output

    def _timezone(self) -> ZoneInfo:
        return ZoneInfo(self._config.timezone)

    def _sanitize_remote(
        self,
        messages: list[dict[str, Any]],
        chat_type: str,
        encrypted_chat_id: str,
    ) -> list[dict[str, Any]]:
        output: list[dict[str, Any]] = []
        for item in messages:
            if not isinstance(item, dict):
                continue
            raw_sender = str(item.get("user_id", ""))
            if not raw_sender:
                continue
            sender = item.get("sender") or {}
            output.append(
                {
                    "chat_type": chat_type,
                    "encrypted_chat_id": encrypted_chat_id,
                    "encrypted_sender_id": self._vault.encrypt("user", raw_sender),
                    "sender_name": str(
                        sender.get("card") or sender.get("nickname") or ""
                    ),
                    "content": _extract_text(item.get("message"))[
                        : self._config.history_content_chars
                    ],
                    "timestamp": float(item.get("time", 0)),
                    "message_id": str(
                        item.get("message_id") or item.get("message_seq") or ""
                    ),
                    "mentioned_bot": False,
                }
            )
        return output

    @staticmethod
    def _merge(
        local: list[dict[str, Any]],
        remote: list[dict[str, Any]],
        start_time: float | None,
        end_time: float | None,
        limit: int,
    ) -> list[dict[str, Any]]:
        by_key: dict[str, dict[str, Any]] = {}
        for item in [*local, *remote]:
            timestamp = float(item.get("timestamp", 0))
            if start_time is not None and timestamp < start_time:
                continue
            if end_time is not None and timestamp > end_time:
                continue
            key = str(
                item.get("message_id")
                or (
                    f"{timestamp}:"
                    f"{item.get('encrypted_sender_id', '')}:"
                    f"{item.get('content', '')}"
                )
            )
            by_key[key] = item
        return sorted(
            by_key.values(), key=lambda item: float(item.get("timestamp", 0))
        )[-limit:]


class ChatDirectoryService:
    """Read the bot's friends and joined groups without exposing raw QQ IDs."""

    def __init__(
        self,
        config: AppConfig,
        napcat: NapCatClient,
        vault: ContactVault,
    ) -> None:
        self._config = config
        self._napcat = napcat
        self._vault = vault

    async def friends(
        self,
        page: int = 1,
        page_size: int | None = None,
    ) -> dict[str, Any]:
        items: list[dict[str, Any]] = []
        for friend in await self._napcat.fetch_friend_list():
            raw_id = friend.get("user_id")
            if raw_id in {None, ""}:
                continue
            nickname = str(friend.get("nickname") or "")
            remark = str(friend.get("remark") or "")
            items.append(
                {
                    "encrypted_target_id": self._vault.encrypt("user", str(raw_id)),
                    "nickname": nickname,
                    "remark": remark,
                    "display_name": remark or nickname,
                }
            )
        items.sort(
            key=lambda item: (
                str(item["display_name"]).casefold(),
                str(item["encrypted_target_id"]),
            )
        )
        return self._page("friend", items, page, page_size)

    async def groups(
        self,
        page: int = 1,
        page_size: int | None = None,
    ) -> dict[str, Any]:
        items: list[dict[str, Any]] = []
        for group in await self._napcat.fetch_group_list():
            raw_id = group.get("group_id")
            if raw_id in {None, ""}:
                continue
            items.append(
                {
                    "encrypted_target_id": self._vault.encrypt("group", str(raw_id)),
                    "group_name": str(group.get("group_name") or ""),
                    "group_memo": str(group.get("group_memo") or ""),
                    "member_count": _optional_int(group.get("member_count")),
                    "max_member_count": _optional_int(
                        group.get("max_member_count")
                    ),
                }
            )
        items.sort(
            key=lambda item: (
                str(item["group_name"]).casefold(),
                str(item["encrypted_target_id"]),
            )
        )
        return self._page("group", items, page, page_size)

    def _page(
        self,
        kind: str,
        items: list[dict[str, Any]],
        page: int,
        page_size: int | None,
    ) -> dict[str, Any]:
        if page < 1:
            raise ValueError("page must be at least 1")
        size = page_size or self._config.directory_default_page_size
        if size < 1:
            raise ValueError("page_size must be at least 1")
        size = min(size, self._config.directory_max_page_size)
        total = len(items)
        total_pages = (total + size - 1) // size if total else 0
        start = (page - 1) * size
        end = start + size
        return {
            "kind": kind,
            "page": page,
            "page_size": size,
            "total": total,
            "total_pages": total_pages,
            "has_more": end < total,
            "items": items[start:end],
        }


def build_tool_registry(
    *,
    config: AppConfig,
    todos: TodoStore,
    schedules: ScheduleStore,
    knowledge: KnowledgeBase,
    contacts: ContactDirectory,
    outbound: OutboundQueue,
    history: HistoryService,
    chat_directory: ChatDirectoryService,
    mcp: McpManager,
) -> ToolRegistry:
    registry = ToolRegistry()

    async def send_qq_message(args: dict[str, Any]) -> dict[str, Any]:
        outbound.enqueue(
            _required_str(args, "chat_type"),
            _required_str(args, "encrypted_target_id"),
            _required_str(args, "content"),
        )
        return {"ok": True, "queued": True}

    registry.register(
        Tool(
            name="send_qq_message",
            description=(
                "把一条 QQ 消息放入安全发送队列。普通模型文本不会被发送，"
                "回复用户必须调用此工具。encrypted_target_id 必须来自消息上下文。"
            ),
            input_schema=_object_schema(
                {
                    "chat_type": {
                        "type": "string",
                        "enum": ["private", "group"],
                    },
                    "encrypted_target_id": {"type": "string"},
                    "content": {"type": "string"},
                },
                required=["chat_type", "encrypted_target_id", "content"],
            ),
            handler=send_qq_message,
        )
    )

    async def get_qq_message_history(args: dict[str, Any]) -> dict[str, Any]:
        chat_type = _required_str(args, "chat_type")
        encrypted_target_id = _required_str(args, "encrypted_target_id")
        limit = min(
            max(int(args.get("limit", config.history_limit)), 1),
            config.history_max_fetch,
        )
        timezone = ZoneInfo(config.timezone)
        start_time = _parse_time_arg(args.get("start_time"), timezone)
        end_time = _parse_time_arg(args.get("end_time"), timezone)
        if (
            start_time is not None
            and end_time is not None
            and start_time > end_time
        ):
            raise ValueError("start_time must not be later than end_time")
        messages = await history.get(
            chat_type,
            encrypted_target_id,
            limit=limit,
            start_time=start_time,
            end_time=end_time,
        )
        return {"messages": messages, "count": len(messages)}

    registry.register(
        Tool(
            name="get_qq_message_history",
            description=(
                "读取私聊或群聊的文本消息历史，按时间升序返回加密发送者与内容。"
                "时间参数可使用 ISO 8601 或 Unix 秒。"
            ),
            input_schema=_object_schema(
                {
                    "chat_type": {
                        "type": "string",
                        "enum": ["private", "group"],
                    },
                    "encrypted_target_id": {"type": "string"},
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": config.history_max_fetch,
                    },
                    "start_time": {
                        "anyOf": [{"type": "string"}, {"type": "number"}]
                    },
                    "end_time": {
                        "anyOf": [{"type": "string"}, {"type": "number"}]
                    },
                },
                required=["chat_type", "encrypted_target_id"],
            ),
            handler=get_qq_message_history,
        )
    )

    async def list_qq_friends(args: dict[str, Any]) -> dict[str, Any]:
        return await chat_directory.friends(
            page=int(args.get("page", 1)),
            page_size=_optional_page_size(args),
        )

    async def list_qq_groups(args: dict[str, Any]) -> dict[str, Any]:
        return await chat_directory.groups(
            page=int(args.get("page", 1)),
            page_size=_optional_page_size(args),
        )

    for name, description, handler in (
        (
            "list_qq_friends",
            (
                "列出机器人当前 QQ 好友。返回加密好友标识、昵称和备注，"
                "并使用 page、page_size 分页；不要暴露原始 QQ 号。"
            ),
            list_qq_friends,
        ),
        (
            "list_qq_groups",
            (
                "列出机器人当前加入的 QQ 群。返回加密群标识、群名、群备注和人数，"
                "并使用 page、page_size 分页；不要暴露原始群号。"
            ),
            list_qq_groups,
        ),
    ):
        registry.register(
            Tool(
                name=name,
                description=description,
                input_schema=_object_schema(
                    {
                        "page": {"type": "integer", "minimum": 1},
                        "page_size": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": config.directory_max_page_size,
                        },
                    }
                ),
                handler=handler,
            )
        )

    async def get_todo_list_length(_: dict[str, Any]) -> dict[str, Any]:
        return {"length": await todos.length()}

    async def get_todo_item(args: dict[str, Any]) -> dict[str, Any]:
        item = await todos.get(_required_str(args, "id"))
        return {"found": item is not None, "item": asdict(item) if item else None}

    async def add_todo_item(args: dict[str, Any]) -> dict[str, Any]:
        item = await todos.add(
            _required_str(args, "content"),
            source="agent",
            context=str(args.get("context", "")),
            metadata={"created_by": "agent"},
        )
        return {"ok": True, "item": asdict(item)}

    async def delete_todo_item(args: dict[str, Any]) -> dict[str, Any]:
        item = await todos.delete(_required_str(args, "id"))
        return {"deleted": item is not None, "id": args["id"]}

    for name, description, schema, handler in (
        (
            "get_todo_list_length",
            "获取当前 TODO 列表长度。",
            _object_schema({}),
            get_todo_list_length,
        ),
        (
            "get_todo_item",
            "按 id 获取一个 TODO 的完整值。",
            _object_schema({"id": {"type": "string"}}, ["id"]),
            get_todo_item,
        ),
        (
            "add_todo_item",
            "向 TODO 列表添加一项当前需要立即处理的工作。",
            _object_schema(
                {
                    "content": {"type": "string"},
                    "context": {"type": "string"},
                },
                ["content"],
            ),
            add_todo_item,
        ),
        (
            "delete_todo_item",
            "处理完成后永久删除指定的 TODO 项。",
            _object_schema({"id": {"type": "string"}}, ["id"]),
            delete_todo_item,
        ),
    ):
        registry.register(Tool(name, description, schema, handler))

    async def manage_scheduled_tasks(args: dict[str, Any]) -> dict[str, Any]:
        action = _required_str(args, "action")
        if action == "list":
            return {"tasks": [asdict(task) for task in await schedules.list()]}
        if action == "create":
            task = await schedules.create(
                title=_required_str(args, "title"),
                content=_required_str(args, "content"),
                schedule=_required_object(args, "schedule"),
                encrypted_target_id=str(args.get("encrypted_target_id", "")),
                chat_type=str(args.get("chat_type", "")),
            )
            return {"ok": True, "task": asdict(task)}
        if action == "update":
            schedule = args.get("schedule")
            if schedule is not None and not isinstance(schedule, dict):
                raise ValueError("schedule must be an object")
            task = await schedules.update(
                _required_str(args, "task_id"),
                title=_optional_str(args, "title"),
                content=_optional_str(args, "content"),
                schedule=schedule,
                enabled=args.get("enabled")
                if isinstance(args.get("enabled"), bool)
                else None,
                encrypted_target_id=_optional_str(args, "encrypted_target_id"),
                chat_type=_optional_str(args, "chat_type"),
            )
            return {
                "updated": task is not None,
                "task": asdict(task) if task else None,
            }
        if action == "delete":
            task = await schedules.delete(_required_str(args, "task_id"))
            return {
                "deleted": task is not None,
                "task": asdict(task) if task else None,
            }
        raise ValueError("action must be list, create, update, or delete")

    registry.register(
        Tool(
            name="manage_scheduled_tasks",
            description=(
                "读取、创建、修改或删除持久化定时任务。schedule.kind 支持 "
                "once、interval、daily、weekly、monthly、yearly。"
            ),
            input_schema=_object_schema(
                {
                    "action": {
                        "type": "string",
                        "enum": ["list", "create", "update", "delete"],
                    },
                    "task_id": {"type": "string"},
                    "title": {"type": "string"},
                    "content": {"type": "string"},
                    "schedule": {
                        "type": "object",
                        "description": (
                            "once:{kind,at}; interval:{kind,every_seconds|"
                            "every_minutes|every_hours|every_days}; daily:{kind,time}; "
                            "weekly:{kind,weekdays,time}; monthly:{kind,days_of_month,time}; "
                            "yearly:{kind,month,day,time}"
                        ),
                    },
                    "enabled": {"type": "boolean"},
                    "chat_type": {
                        "type": "string",
                        "enum": ["private", "group"],
                    },
                    "encrypted_target_id": {"type": "string"},
                },
                ["action"],
            ),
            handler=manage_scheduled_tasks,
        )
    )

    async def current_time(_: dict[str, Any]) -> dict[str, Any]:
        now = schedules.now()
        return {
            "iso": now.isoformat(),
            "timezone": config.timezone,
            "weekday": now.strftime("%A"),
        }

    if config.time_tool:
        registry.register(
            Tool(
                name="get_current_time",
                description="获取配置时区下的当前时间。",
                input_schema=_object_schema({}),
                handler=current_time,
            )
        )

    async def get_contact_profile(args: dict[str, Any]) -> dict[str, Any]:
        return contacts.get(_required_str(args, "encrypted_id"))

    async def update_contact_profile(args: dict[str, Any]) -> dict[str, Any]:
        encrypted_id = _required_str(args, "encrypted_id")
        display_name = _optional_str(args, "display_name")
        notes = _optional_str(args, "notes")
        if display_name is None and notes is None:
            raise ValueError("display_name or notes is required")
        return contacts.update(encrypted_id, display_name, notes)

    for name, description, schema, handler in (
        (
            "get_contact_profile",
            "读取加密联系人的昵称和备注。",
            _object_schema({"encrypted_id": {"type": "string"}}, ["encrypted_id"]),
            get_contact_profile,
        ),
        (
            "update_contact_profile",
            (
                "更新加密联系人的昵称或备注，用于逐渐认识陌生用户。"
                "此工具不能修改 admin 权限。"
            ),
            _object_schema(
                {
                    "encrypted_id": {"type": "string"},
                    "display_name": {"type": "string"},
                    "notes": {"type": "string"},
                },
                ["encrypted_id"],
            ),
            update_contact_profile,
        ),
    ):
        registry.register(Tool(name, description, schema, handler))

    async def list_knowledge_files(args: dict[str, Any]) -> dict[str, Any]:
        return {
            "files": knowledge.list_files(
                str(args.get("scope", "all")),  # type: ignore[arg-type]
                str(args.get("prefix", "")),
            )
        }

    async def read_knowledge_lines(args: dict[str, Any]) -> dict[str, Any]:
        return knowledge.read_lines(
            str(args.get("scope", "knowledge")),  # type: ignore[arg-type]
            _required_str(args, "path"),
            int(args.get("start_line", 1)),
            int(args.get("end_line", 80)),
        )

    async def search_knowledge(args: dict[str, Any]) -> dict[str, Any]:
        return {
            "matches": knowledge.search(
                _required_str(args, "query"),
                scope=str(args.get("scope", "all")),  # type: ignore[arg-type]
                path=str(args.get("path", "")),
                max_results=int(args.get("max_results", 20)),
            )
        }

    async def write_notebook(args: dict[str, Any]) -> dict[str, Any]:
        return knowledge.write(
            _required_str(args, "path"),
            _required_str(args, "content"),
            mode=str(args.get("mode", "replace")),  # type: ignore[arg-type]
        )

    async def delete_notebook(args: dict[str, Any]) -> dict[str, Any]:
        return knowledge.delete(_required_str(args, "path"))

    tools = (
        (
            "list_knowledge_files",
            "列出知识库或记事本中的文件，先调用它再读取文件。",
            _object_schema(
                {
                    "scope": {
                        "type": "string",
                        "enum": ["all", "knowledge", "notebook"],
                    },
                    "prefix": {"type": "string"},
                }
            ),
            list_knowledge_files,
        ),
        (
            "read_knowledge_lines",
            "读取一个知识库或记事本文件的指定行范围。",
            _object_schema(
                {
                    "scope": {
                        "type": "string",
                        "enum": ["knowledge", "notebook"],
                    },
                    "path": {"type": "string"},
                    "start_line": {"type": "integer", "minimum": 1},
                    "end_line": {"type": "integer", "minimum": 1},
                },
                ["scope", "path"],
            ),
            read_knowledge_lines,
        ),
        (
            "search_knowledge",
            "在知识库和记事本中按字面内容搜索。",
            _object_schema(
                {
                    "query": {"type": "string"},
                    "scope": {
                        "type": "string",
                        "enum": ["all", "knowledge", "notebook"],
                    },
                    "path": {"type": "string"},
                    "max_results": {"type": "integer", "minimum": 1},
                },
                ["query"],
            ),
            search_knowledge,
        ),
        (
            "write_notebook",
            "新建、替换或追加记事本中的 Markdown/文本文件。",
            _object_schema(
                {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                    "mode": {
                        "type": "string",
                        "enum": ["replace", "append"],
                    },
                },
                ["path", "content"],
            ),
            write_notebook,
        ),
        (
            "delete_notebook",
            "删除记事本中的一个文件。只允许操作 notebook 目录。",
            _object_schema({"path": {"type": "string"}}, ["path"]),
            delete_notebook,
        ),
    )
    for name, description, schema, handler in tools:
        registry.register(Tool(name, description, schema, handler))

    for remote_tool in mcp.tools:
        async def call_remote(
            args: dict[str, Any],
            exposed_name: str = remote_tool.exposed_name,
        ) -> dict[str, Any]:
            return await mcp.call(exposed_name, args)

        registry.register(
            Tool(
                name=remote_tool.exposed_name,
                description=remote_tool.description,
                input_schema=remote_tool.input_schema
                or {"type": "object", "properties": {}},
                handler=call_remote,
            )
        )
    return registry


def _extract_text(message: Any) -> str:
    if isinstance(message, str):
        return re.sub(r"\[CQ:[^\]]+\]", "", message)
    if not isinstance(message, list):
        return ""
    return "".join(
        str((segment.get("data") or {}).get("text", ""))
        for segment in message
        if isinstance(segment, dict) and segment.get("type") == "text"
    )


def _object_schema(
    properties: dict[str, Any],
    required: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": required or [],
        "additionalProperties": False,
    }


def _required_str(args: dict[str, Any], key: str) -> str:
    value = str(args.get(key, "")).strip()
    if not value:
        raise ValueError(f"{key} is required")
    return value


def _optional_str(args: dict[str, Any], key: str) -> str | None:
    if key not in args:
        return None
    return str(args.get(key, ""))


def _required_object(args: dict[str, Any], key: str) -> dict[str, Any]:
    value = args.get(key)
    if not isinstance(value, dict) or not value:
        raise ValueError(f"{key} must be a non-empty object")
    return value


def _parse_time_arg(value: Any, timezone: ZoneInfo) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    try:
        return float(text)
    except ValueError:
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone)
        return parsed.timestamp()


def _optional_page_size(args: dict[str, Any]) -> int | None:
    if "page_size" not in args:
        return None
    return int(args["page_size"])


def _optional_int(value: Any) -> int | None:
    if value in {None, ""}:
        return None
    return int(value)


def json_tool_result(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
