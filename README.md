# py-raspberry-qq-agent

面向 QQ 私聊和群聊的树莓娘 Agent，使用 NapCat / OneBot 11 和 OpenAI 兼容模型接口。
聊天、定时动作和报告生成分别执行，报告不需要指定 QQ 收件人。

## 运行

需要 Python 3.12、uv、可用的模型接口及 NapCat WebSocket 服务。

```bash
uv sync --all-groups
cp config.example.yaml config.yaml
cp .env.example .env
uv run --env-file .env py-raspberry-qq-agent --config config.yaml config-check
uv run --env-file .env py-raspberry-qq-agent --config config.yaml
```

所有行为参数从 YAML 读取；使用 `${VAR}` 或 `${VAR:-fallback}` 引用环境变量。
`config.yaml` 和 `.env` 不提交到仓库。

## 执行方式

- 保存收到的消息历史。私聊和群内 @机器人消息经过允许列表、权限和重要性判断。
- 管理员跳过分类模型；其他消息只有达到重要性阈值才进入内存任务队列。
- 聊天 Agent 每次处理一个任务，外部动作通过工具完成。普通模型文本不会自动发送到 QQ。
- 任务由程序取出和结束，不向模型提供添加、删除 TODO 的工具。失败记日志，不自动重试；重启丢失待处理任务。
- QQ 发送使用有界内存队列，串行发送并保留随机间隔。失败记日志，不补发。
- 定时群摘要直接读取历史、生成 Markdown 并保存，可显式配置 MCP 上传工具；不经过聊天 Agent。
- 每个聊天任务使用独立的模型上下文，需要更多信息时调用历史工具；不同任务不共享模型对话缓存。

这是一套尽力而为的自动化，不维护持久化工作队列、投递记录或补偿流程。

## 身份与模型边界

业务代码、配置和本地数据直接使用 QQ 号和群号。不再使用联系人加密、加密密钥或密文映射。
`ModelIds` 只在模型输入和内置 QQ 工具边界把结构化号码转成不透明引用。
模型工具返回的引用在程序内还原，未见过的引用和类型不匹配的引用会被拒绝。

这些引用只是进程内映射，不是用户身份主键，也不提供落盘保密保护。
消息正文、昵称和备注是普通文本，不承诺检测其中人为填写的号码。
外部 MCP 工具保留各自的参数语义，不套用 QQ 字段转换。

联系人备注与管理员授权分开存储，模型只能编辑备注，不能修改授权。

```bash
uv run --env-file .env py-raspberry-qq-agent --config config.yaml admin add --qq 123456789
uv run --env-file .env py-raspberry-qq-agent --config config.yaml admin remove --qq 123456789
uv run --env-file .env py-raspberry-qq-agent --config config.yaml admin list
uv run --env-file .env py-raspberry-qq-agent --config config.yaml profile set --qq 123456789 --name 张三 --notes 技术组同事
uv run --env-file .env py-raspberry-qq-agent --config config.yaml profile get --qq 123456789
uv run --env-file .env py-raspberry-qq-agent --config config.yaml profile list --search 技术组
uv run --env-file .env py-raspberry-qq-agent --config config.yaml profile remove --qq 123456789
```

CLI 和本地文件会显示真实号码。手动编辑资料或通过 CLI 修改时应先停止 Agent，避免内存缓存覆盖文件。
`security.admin_qq_ids` 可指定启动时授予管理员权限的号码。

## 聊天允许列表

```yaml
message_gate:
  allowlist:
    enabled: true
    private_qq_ids: [123456789]
    group_ids: [987654321]
```

启用后只允许指定聊天触发 Agent 和接收发送队列中的消息；管理员也受限制。
未 @机器人的群消息不触发模型。历史、好友列表和群列表查询不受此列表限制。

好友和群工具支持 `page` / `page_size` 分页。联系人备注支持非好友，不用于建立外部账号绑定关系。

## 定时任务与报告

聊天 Agent 的 `manage_scheduled_tasks` 支持 once、interval、daily、weekly、monthly、yearly。
时间使用 `app.timezone`。目标聊天可为空，定时配置保存在 `schedules.json`；触发后的执行队列不落盘。

群摘要示例：

```yaml
group_digest:
  enabled: true
  groups: [987654321]
  interval_minutes: 60
  initial_lookback_minutes: 60
  min_messages: 3
  prompt: 整理技术结论、争议和后续事项。
  upload_tool: "" # 指定如 mcp__upload__upload_markdown；为空则只保存本地
```

文件保存在 `notebooks/group-digests/`。历史窗口使用 `[start, end)`，游标只保存在内存。
每次启动重新按 `initial_lookback_minutes` 选取起点。每个窗口推进一次，生成或上传失败不补做。
历史查询最多读取 `history.max_fetch` 条，超出部分不包含在摘要中。

`reports.summarize` 返回 Markdown；`reports.publish` 保存文件，再按明确配置调用上传工具。
后台代码可以直接复用这两个函数，无须构造聊天任务。

## 知识库、记事本与 MCP

知识库支持列文件、按行读取和字面搜索；记事本支持新建、替换、追加和删除。
路径限制在配置目录内，拒绝绝对路径和 `..` 越界。不引入 RAG 或向量数据库。

MCP 支持 stdio 和 Streamable HTTP，可通过 `allowed_tools` 限制工具。
仓库附带 Markdown 上传桥接器，读取 `mcp.upload`，向 HTTP 接收端发送：

```json
{"title": "标题", "markdown": "# 正文", "metadata": {}}
```

模型工具名带服务前缀，如 `mcp__upload__upload_markdown`。
思考模式按供应商要求配置在 `llm.main.extra_body`；不将模型思考写入 QQ。

## 本地数据

| 文件 | 用途 |
| --- | --- |
| `roles.json` | 按 QQ 号维护管理员授权 |
| `profiles.json` | 按 QQ 号保存昵称和备注 |
| `schedules.json` | 用户创建的定时配置 |
| `messages.jsonl` | 消息历史 |

即时任务、发送队列和群摘要游标只存在于内存。不要公开本地数据、知识库和记事本。
此版本不兼容旧的加密数据文件，不提供迁移或旧参数兼容层。

## 验证

```bash
uv run pytest -q
uv run python -m compileall -q src
```

测试覆盖消息分流、允许列表、模型引用边界、工具调用、任务失败后继续处理、发送间隔、定时配置、报告输出及 MCP 上传。
