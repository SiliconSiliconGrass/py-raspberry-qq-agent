from __future__ import annotations

import json
import logging
import re
from contextlib import AsyncExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamablehttp_client

from .config import AppConfig

LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class RemoteMcpTool:
    exposed_name: str
    remote_name: str
    server_name: str
    description: str
    input_schema: dict[str, Any]

    def openai_definition(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.exposed_name,
                "description": self.description,
                "parameters": self.input_schema
                or {"type": "object", "properties": {}},
            },
        }


class McpManager:
    def __init__(self, config: AppConfig) -> None:
        self._config = config
        self._stack = AsyncExitStack()
        self._sessions: dict[str, ClientSession] = {}
        self._tools: dict[str, RemoteMcpTool] = {}

    @property
    def tools(self) -> list[RemoteMcpTool]:
        return list(self._tools.values())

    async def start(self) -> None:
        if not self._config.mcp_enabled:
            return
        for server in self._config.mcp_servers:
            if not isinstance(server, dict):
                LOGGER.warning("ignored invalid MCP server configuration")
                continue
            try:
                await self._start_server(server)
            except Exception:
                LOGGER.exception(
                    "failed to start MCP server name=%s", server.get("name", "?")
                )
        LOGGER.info("MCP ready: %d tools from %d servers", len(self._tools), len(self._sessions))

    async def stop(self) -> None:
        await self._stack.aclose()
        self._sessions.clear()
        self._tools.clear()

    async def _start_server(self, config: dict[str, Any]) -> None:
        server_name = _safe_name(str(config.get("name", "")))
        if not server_name:
            raise ValueError("MCP server name is required")
        transport = str(config.get("transport", "stdio")).lower()
        read_stream: Any
        write_stream: Any
        if transport == "stdio":
            command = str(config.get("command", ""))
            if not command:
                raise ValueError(f"MCP server {server_name}: command is required")
            args = [str(arg) for arg in config.get("args", [])]
            env = {
                str(key): str(value)
                for key, value in (config.get("env") or {}).items()
            }
            raw_cwd = Path(str(config.get("cwd") or self._config.config_dir))
            cwd = (
                raw_cwd
                if raw_cwd.is_absolute()
                else (self._config.config_dir / raw_cwd).resolve()
            )
            params = StdioServerParameters(
                command=command,
                args=args,
                env=env or None,
                cwd=cwd,
            )
            read_stream, write_stream = await self._stack.enter_async_context(
                stdio_client(params)
            )
        elif transport in {"streamable_http", "http"}:
            url = str(config.get("url", ""))
            if not url:
                raise ValueError(f"MCP server {server_name}: url is required")
            headers = {
                str(key): str(value)
                for key, value in (config.get("headers") or {}).items()
            }
            streams = await self._stack.enter_async_context(
                streamablehttp_client(url, headers=headers)
            )
            read_stream, write_stream = streams[0], streams[1]
        else:
            raise ValueError(
                f"MCP server {server_name}: unsupported transport {transport!r}"
            )

        session = await self._stack.enter_async_context(
            ClientSession(read_stream, write_stream)
        )
        await session.initialize()
        self._sessions[server_name] = session
        result = await session.list_tools()
        allowed = {
            str(name)
            for name in config.get("allowed_tools", [])
            if str(name).strip()
        }
        for remote_tool in result.tools:
            if allowed and remote_tool.name not in allowed:
                continue
            exposed = _safe_name(f"mcp__{server_name}__{remote_tool.name}")
            if exposed in self._tools:
                raise ValueError(f"duplicate MCP tool name after normalization: {exposed}")
            schema = getattr(remote_tool, "inputSchema", None)
            self._tools[exposed] = RemoteMcpTool(
                exposed_name=exposed,
                remote_name=remote_tool.name,
                server_name=server_name,
                description=str(remote_tool.description or remote_tool.name),
                input_schema=dict(schema or {}),
            )

    async def call(self, exposed_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        tool = self._tools.get(exposed_name)
        if tool is None:
            raise KeyError(f"unknown MCP tool: {exposed_name}")
        session = self._sessions[tool.server_name]
        result = await session.call_tool(tool.remote_name, arguments)
        content: list[Any] = []
        for item in getattr(result, "content", []):
            if hasattr(item, "model_dump"):
                content.append(item.model_dump(mode="json"))
            else:
                content.append(str(item))
        return {
            "ok": not bool(getattr(result, "isError", False)),
            "server": tool.server_name,
            "tool": tool.remote_name,
            "content": content,
            "structured_content": _json_safe(
                getattr(result, "structuredContent", None)
            ),
        }


def _safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "_", value.strip())[:64]


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    try:
        return json.loads(json.dumps(value, default=str))
    except (TypeError, ValueError):
        return str(value)
