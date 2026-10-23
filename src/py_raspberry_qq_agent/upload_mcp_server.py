from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP

from .config import AppConfig


def main() -> None:
    parser = argparse.ArgumentParser(
        description="MCP stdio bridge for uploading Markdown artifacts"
    )
    parser.add_argument("--config", required=True, help="Path to the agent YAML config")
    args = parser.parse_args()
    config = AppConfig.load(Path(args.config))
    if not config.mcp_upload_endpoint:
        raise SystemExit("mcp.upload.endpoint is not configured")

    server = FastMCP("qq-agent-upload")

    @server.tool()
    async def upload_markdown(
        title: str,
        markdown: str,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Upload a Markdown document to the configured collaboration system."""
        headers = {"Content-Type": "application/json", **config.mcp_upload_headers}
        if config.mcp_upload_api_token:
            headers["Authorization"] = f"Bearer {config.mcp_upload_api_token}"
        async with httpx.AsyncClient(
            timeout=config.mcp_upload_timeout_seconds
        ) as client:
            response = await client.post(
                config.mcp_upload_endpoint,
                headers=headers,
                json={
                    "title": title,
                    "markdown": markdown,
                    "metadata": metadata or {},
                },
            )
            response.raise_for_status()
            try:
                body: Any = response.json()
            except ValueError:
                body = response.text
            return {
                "status_code": response.status_code,
                "body": body,
            }

    server.run(transport="stdio")


if __name__ == "__main__":
    main()
