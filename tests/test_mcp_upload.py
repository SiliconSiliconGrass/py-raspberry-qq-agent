from __future__ import annotations

import asyncio
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, ClassVar

import pytest

from py_raspberry_qq_agent.config import AppConfig
from py_raspberry_qq_agent.mcp_runtime import McpManager


class UploadHandler(BaseHTTPRequestHandler):
    received: ClassVar[list[dict[str, Any]]] = []

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length))
        self.received.append(payload)
        body = json.dumps({"id": "doc-1"}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        return


def _mcp_config(tmp_path: Path, endpoint: str) -> AppConfig:
    path = tmp_path / "mcp-config.yaml"
    path.write_text(
        f"""
app:
  timezone: Asia/Shanghai
  data_dir: ./data
  knowledge_dir: ./knowledge
  notebook_dir: ./notebooks
logging:
  file: ./logs/test.log
llm:
  main:
    base_url: https://example.invalid/v1
    api_key: main-key
    model: main-model
  classifier:
    base_url: https://example.invalid/v1
    api_key: classifier-key
    model: classifier-model
security:
  encryption_key: test-encryption-secret
mcp:
  enabled: true
  upload:
    endpoint: {endpoint}
  servers:
    items:
      - name: upload
        transport: stdio
        command: {json.dumps(sys.executable)}
        args:
          - -m
          - py_raspberry_qq_agent.upload_mcp_server
          - --config
          - {path.name}
        allowed_tools:
          - upload_markdown
""",
        encoding="utf-8",
    )
    return AppConfig.load(path)


@pytest.mark.asyncio
async def test_mcp_stdio_upload_bridge(tmp_path: Path) -> None:
    UploadHandler.received = []
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), UploadHandler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    endpoint = f"http://127.0.0.1:{httpd.server_port}/upload"
    manager = McpManager(_mcp_config(tmp_path, endpoint))
    try:
        await asyncio.wait_for(manager.start(), timeout=20)
        assert [tool.exposed_name for tool in manager.tools] == [
            "mcp__upload__upload_markdown"
        ]
        result = await asyncio.wait_for(
            manager.call(
                "mcp__upload__upload_markdown",
                {
                    "title": "Digest",
                    "markdown": "# Hello",
                    "metadata": {"group": "qq-group:test"},
                },
            ),
            timeout=20,
        )
        assert result["ok"] is True
        assert UploadHandler.received == [
            {
                "title": "Digest",
                "markdown": "# Hello",
                "metadata": {"group": "qq-group:test"},
            }
        ]
    finally:
        await manager.stop()
        httpd.shutdown()
        thread.join(timeout=5)
