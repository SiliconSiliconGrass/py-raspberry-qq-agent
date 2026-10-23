# py-raspberry-qq-agent

一个面向 QQ 私聊和群聊的协作 Agent。它把 NapCat 收到的 OneBot 11 事件转成加密标识，
先做权限/重要性分流，再通过持久化 TODO 队列驱动主 Agent。主 Agent 可以发送 QQ 消息、
读取消息历史、管理定时任务、搜索知识库、编辑记事本，并调用外部 MCP 工具上传 Markdown。

## 关键行为

- 私聊消息和群内 `@机器人` 消息进入重要性判断。
- 发送者的加密标识若具有 `admin` 权限，直接写入 TODO 并唤醒主模型，不调用分类模型。
- 非管理员才调用配置中的小参数模型。只有 `important=true` 且置信度达到阈值时才写入
  TODO；不重要的消息不会唤醒主模型。
- 所有群消息都会以加密发送者标识保存到本地历史，供定时群聊总结使用。未 `@` 机器人的
  群消息不会触发分类模型或主模型。
- 可选的聊天允许列表启用后，只有列表中的私聊或列表中的群聊 `@消息` 能触发 Agent。
  该列表也限制 `send_qq_message` 的发送目标，但不限制历史消息读取。
- 主模型返回的普通文本不会发给 QQ。外部动作必须通过 `send_qq_message` 工具进入发送队列。
- 发送队列由单个消费者串行处理，默认在两次发送之间等待 2 到 4 秒的随机安全间隔。
- TODO、定时任务、群摘要游标、联系人别名和历史都持久化到磁盘，重启后不会丢失。

## 架构

```text
QQ
  |
NapCat OneBot 11 WebSocket server :3001
  |
  +-- 收到私聊/群聊事件
        |
        +-- 加密 user/group ID
        +-- 保存群聊历史
        +-- admin? --是--> TODO --> 主 Agent
        |            否
        +-- 小模型判断 --是--> TODO --> 主 Agent
                      --否--> 只保留历史

主 Agent
  +-- send_qq_message ------> 安全发送队列 --> NapCat
  +-- get_qq_message_history
  +-- TODO 工具
  +-- 定时任务工具
  +-- 联系人资料工具
  +-- 知识库/记事本工具
  +-- MCP 动态工具（例如 upload_markdown）
```

QQ 数字 ID 的加解密只在网关内部完成。主 Agent 的提示词、工具参数、消息历史和日志中
只出现类似 `qq-user:...`、`qq-group:...` 的稳定加密别名。

## 环境准备

需要 Python 3.12、`uv`、一个 OpenAI Chat Completions 兼容接口，以及已经运行的 NapCat。
现有 NapCat Playground 在 `3001` 端口提供 OneBot WebSocket 服务端，本项目作为客户端连接，
通常不需要修改它的 `docker-compose.yml`。

```powershell
cd D:\MyStuff\Playground\py-raspberry-qq-agent
uv sync --all-groups
Copy-Item config.example.yaml config.yaml
Copy-Item .env.example .env
```

在 `config.yaml` 中填写模型、存储目录、群摘要和 MCP 配置。一个基本 `.env` 示例：

```dotenv
LLM_API_KEY=replace-me
QQ_ID_ENCRYPTION_KEY=replace-with-a-long-random-stable-value
NAPCAT_ACCESS_TOKEN=
```

`config.yaml` 本身也已被 `.gitignore` 忽略。所有行为参数都从 YAML 读取；环境变量仅通过
YAML 中的 `${VAR}` 占位符提供密钥等部署值。

生成加密密钥的一种方式：

```powershell
uv run python -c "import secrets; print(secrets.token_urlsafe(48))"
```

密钥一旦用于生产，应保持不变。变更密钥会使已有的加密别名和密文映射失配。

## 启动

如果 NapCat 尚未运行：

```powershell
cd D:\MyStuff\Playground\napcat-playground
docker compose up -d
```

启动 Agent：

```powershell
cd D:\MyStuff\Playground\py-raspberry-qq-agent
uv run --env-file .env py-raspberry-qq-agent --config config.yaml
```

也可以直接运行模块：

```powershell
uv run --env-file .env python -m py_raspberry_qq_agent --config config.yaml
```

先检查配置：

```powershell
uv run --env-file .env py-raspberry-qq-agent --config config.yaml config-check
```

日志输出到控制台和 `logging.file`，默认是 `logs/agent.log`。日志不会打印 QQ 原始号、
消息正文或模型密钥；重要的分流结果、TODO id、工具名、定时任务 id 和发送目标加密别名
会写入日志。

## 管理员权限

角色与 Agent 可编辑的联系人资料是两份独立文件。Agent 只有
`update_contact_profile`，没有任何授予或收回角色的工具。

通过运维 CLI 授权或撤销：

```powershell
uv run --env-file .env py-raspberry-qq-agent --config config.yaml admin add --qq 123456
uv run --env-file .env py-raspberry-qq-agent --config config.yaml admin remove --qq 123456
uv run --env-file .env py-raspberry-qq-agent --config config.yaml admin list
```

`admin list` 只显示加密别名。也可以把初始管理员 QQ 填入
`security.admin_qq_ids`，服务启动时会将其转换成加密别名后再写入角色表。该列表只存在于
本地 YAML，不会传给 Agent。

## 聊天允许列表

在 `message_gate.allowlist` 中启用聊天白名单：

```yaml
message_gate:
  allowlist:
    enabled: true
    private_qq_ids:
      - 123456789
    group_ids:
      - 987654321
```

启用后：

- 只有列表中的 QQ 私聊可以触发 Agent。
- 只有列表中的群聊可以触发 Agent，并且消息仍需 `@机器人`。
- 允许列表检查优先于 admin 和重要性小模型；未列出的聊天即使发送者是 admin 也不会触发。
- `send_qq_message` 只能发往同一列表中的私聊或群聊。
- `get_qq_message_history` 不受允许列表限制。

关闭时设置 `enabled: false`，此时不会限制聊天。原始 QQ/群号保留在本地 YAML 中，Agent
仍然只接触加密别名。

查看某个 QQ 或群号对应的 Agent 可见标识：

```powershell
uv run --env-file .env py-raspberry-qq-agent --config config.yaml encrypt --kind user --id 123456
```

## 好友与群聊目录

Agent 可以通过 `list_qq_friends` 和 `list_qq_groups` 查询机器人当前的好友和已加入群聊。
两个工具都调用 NapCat 的实时 `get_friend_list` / `get_group_list` API：

- 好友结果包含加密好友标识、昵称、备注和显示名称。
- 群聊结果包含加密群标识、群名、群备注、当前人数和人数上限。
- 不返回原始 QQ 号或群号。
- 使用从 1 开始的 `page` 和 `page_size` 参数分页，并返回 `total`、`total_pages` 和
  `has_more`。

分页参数配置在 YAML：

```yaml
directory:
  default_page_size: 50
  max_page_size: 200
```

这两个查询是只读操作，不受聊天允许列表限制。

## 联系人资料（支持非好友）

`profiles.json` 按加密 QQ 标识记录资料，不依赖 QQ 好友关系。机器人可以在群聊中见到
非好友同事后记录其资料，也可以由运维人员提前预置。

人工新增或更新：

```powershell
uv run --env-file .env py-raspberry-qq-agent --config config.yaml `
  profile set --qq 123456789 --name "张三" --notes "技术组同事，负责服务器维护"
```

查询、分页和删除：

```powershell
uv run --env-file .env py-raspberry-qq-agent --config config.yaml profile get --qq 123456789
uv run --env-file .env py-raspberry-qq-agent --config config.yaml profile list --page 1
uv run --env-file .env py-raspberry-qq-agent --config config.yaml profile list --search "技术组"
uv run --env-file .env py-raspberry-qq-agent --config config.yaml profile remove --qq 123456789
```

CLI 会先把 QQ 号转换成加密标识，再写入 `profiles.json`，命令行和文件输出不会包含原始
QQ 号。Agent 对应工具为：

- `list_contact_profiles`：分页浏览资料库。
- `search_contact_profiles`：按加密标识、显示名称或备注搜索。
- `get_contact_profile`：读取指定加密账号的资料。
- `update_contact_profile`：新建或更新资料，不能修改 admin 权限。

手动修改资料时应先停止 Agent，完成后重启，避免运行中的资料缓存覆盖文件。
`profile remove` 只删除身份资料，不会修改 `roles.json`；撤销 admin 仍需使用 `admin remove`。

## TODO 与主 Agent

消息、定时任务和定时群摘要都会进入同一份 TODO。每次唤醒时，完整 TODO 会交给主 Agent；
系统提示词要求它：

1. 先完成当前任务；
2. 用 `send_qq_message` 发送需要发出的 QQ 回复；
3. 用 `delete_todo_item` 删除已完成项；
4. 如果 TODO 仍非空，再次调用主 Agent。

单次激活最多连续重试 `llm.max_todo_rewakes` 次。之后若仍有未完成项，就保留它们，等下次
新工作到达时继续处理，避免无限消耗模型 API。`llm.max_tool_rounds` 限制单次运行的
模型工具循环数量。

提供的 TODO 工具：

- `get_todo_list_length`
- `get_todo_item`
- `add_todo_item`
- `delete_todo_item`

## 定时任务

Agent 通过 `manage_scheduled_tasks` 读取、创建、修改和删除任务。任务保存在
`data/schedules.json`，支持：

- `once`: `{"kind":"once","at":"2026-10-24T09:00:00+08:00"}`
- `interval`: `{"kind":"interval","every_minutes":30}`
- `daily`: `{"kind":"daily","time":"09:00"}`
- `weekly`: `{"kind":"weekly","weekdays":["mon","周四"],"time":"18:30"}`
- `monthly`: `{"kind":"monthly","days_of_month":[1,15],"time":"10:00"}`
- `yearly`: `{"kind":"yearly","month":4,"day":24,"time":"12:00"}`

时区由 `app.timezone` 控制。任务触发后会变成一条 TODO，再由 Agent 根据任务目标决定是否
调用 `send_qq_message` 或其他工具。

## 定时群聊总结

在 `group_digest` 中启用并填写原始群号：

```yaml
group_digest:
  enabled: true
  groups: [123456789]
  interval_minutes: 60
  initial_lookback_minutes: 60
  min_messages: 3
  prompt: "重点整理技术结论、待办、争议和可执行建议。"
```

到达窗口后，系统创建一个 TODO。Agent 读取窗口内的历史，生成 Markdown，通过
`write_notebook` 保存到 `notebooks/group-digests/`，并调用已加载的 MCP 上传工具。群号在
Agent 侧仍然只是加密别名。

## 知识库和记事本

- `knowledge_dir` 是预留给人工维护的知识库，支持列文件、按行读取和字面搜索。
- `notebook_dir` 是 Agent 可写的记事本，支持新建、替换、追加和删除文件。
- 路径始终限制在这两个目录内，工具会拒绝绝对路径和 `..` 越界。
- 这里没有 RAG 或向量数据库。所需信息通过文件名、行号和关键词搜索直接读取。

## MCP 上传

项目支持动态发现标准 MCP 工具。外部 MCP server 可以通过 stdio 或 Streamable HTTP 接入，
工具名会加前缀，例如 `mcp__docs__upload_markdown`。允许的工具可用 `allowed_tools` 限制。

仓库附带一个 stdio MCP 上传桥接器。配置 HTTP 接收端后：

```dotenv
MCP_ENABLED=true
MCP_UPLOAD_ENDPOINT=https://your-system.example/api/markdown
MCP_UPLOAD_API_TOKEN=replace-me
```

`config.example.yaml` 已经配置了这个桥接器。它向 endpoint 发送：

```json
{
  "title": "群聊摘要标题",
  "markdown": "# Markdown 正文",
  "metadata": {
    "encrypted_group": "qq-group:..."
  }
}
```

如果使用自己的 MCP server，把 `mcp.servers.items` 改成该 server 的连接参数，并把上传工具
加入 `allowed_tools`。没有可用的上传工具时，Agent 仍可把 Markdown 保存在记事本中，
但提示词要求它不得声称已经上传。

## 思考模式

模型思考开关通过 OpenAI 兼容接口的 `extra_body` 配置，不把思维链写入日志或 QQ：

```yaml
llm:
  main:
    extra_body:
      reasoning_effort: medium
```

部分兼容 DeepSeek 风格思考模式的接口可使用：

```yaml
llm:
  main:
    extra_body:
      thinking:
        type: enabled
```

小模型单独配置在 `llm.classifier`。如果 `classifier.api_key` 为空，会复用主模型的 key；
base URL、模型名、温度和输出上限均独立。

## 持久化文件

默认都在 `data/`：

| 文件 | 内容 |
| --- | --- |
| `contacts.json` | 加密别名到 QQ 密文的映射 |
| `roles.json` | 加密别名到权限角色，只允许运维 CLI 修改 |
| `profiles.json` | Agent 可修改的联系人昵称和备注 |
| `todos.json` | 待立即处理的 TODO |
| `schedules.json` | 定时任务 |
| `messages.jsonl` | 加密 ID 的本地消息历史 |
| `digest_state.json` | 群摘要窗口游标 |

状态 JSON 使用同目录临时文件加原子替换写入。请不要把 `data/`、知识库或记事本目录
提交到公开仓库。

## 测试

```powershell
uv run pytest -q
uv run python -m compileall -q src
```

测试不访问真实 LLM。当前覆盖加密身份、角色/资料隔离、管理员绕过分类器、非管理员调用
分类器、不重要消息不生成 TODO、工具发送与 TODO 清理、定时任务类型与持久化、知识库越界
防护、NapCat 消息解析和发送最小间隔。
