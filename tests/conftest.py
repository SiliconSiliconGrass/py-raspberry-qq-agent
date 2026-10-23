from __future__ import annotations

from pathlib import Path

import pytest

from py_raspberry_qq_agent.config import AppConfig


@pytest.fixture
def config(tmp_path: Path) -> AppConfig:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
app:
  timezone: Asia/Shanghai
  data_dir: ./data
  knowledge_dir: ./knowledge
  notebook_dir: ./notebooks
logging:
  level: WARNING
  file: ./logs/test.log
  json: false
llm:
  main:
    base_url: https://example.invalid/v1
    api_key: main-key
    model: main-model
  classifier:
    base_url: https://example.invalid/v1
    api_key: classifier-key
    model: classifier-model
  max_tool_rounds: 3
  max_todo_rewakes: 2
security:
  encryption_key: test-encryption-secret
  admin_qq_ids: []
message_gate:
  outbound_min_interval_seconds: 0
  outbound_max_interval_seconds: 0
scheduler:
  enabled: false
history:
  limit: 80
  max_fetch: 500
""",
        encoding="utf-8",
    )
    config = AppConfig.load(config_path)
    config.prepare_directories()
    return config
