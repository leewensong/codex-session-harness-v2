# Codex 能力接口（MCP Server）使用说明

> 本接口让我们的本地 AI 应用（Ollama 本地模型）通过 **MCP (Model Context Protocol)** 调用 Codex（ChatGPT 桌面应用）的能力：查询会话状态、读取会话内容、把用户反馈转发给 Codex 会话并等待回复。
>
> 协议形式：MCP Server。本地模型一侧只需一个支持 MCP Client 的框架（或直接走 stdio 的 JSON-RPC 2.0）即可调用。

---

## 1. 它能做什么

| 能力 | 工具名 | 说明 |
| --- | --- | --- |
| 查询运行环境 | `codex_runtime_status` | codex CLI 是否可用、版本、会话存储是否健康、锁文件清单 |
| 列出所有会话 | `codex_list_sessions` | 分页列出全部会话，支持按名称/摘要关键字过滤 |
| 新建会话 | `codex_start_session` | 创建一个新的空会话，返回会话 ID |
| 读取会话内容 | `codex_read_session` | 读取某个会话最近 N 条消息（角色 + 文本） |
| 向会话发消息 | `codex_send_message` | 把用户反馈转发给指定会话，等待 Codex 代理回复 |
| 中断回合 | `codex_interrupt_turn` | 打断正在运行的回合（如等待超时后想停止） |

**调用链路：**

```
本地 AI（Ollama）
   │  MCP (stdio / http)
   ▼
codex-mcp-server（本包）
   │  codex app-server（stdio JSON-RPC 2.0）
   ▼
Codex CLI 核心（读/写 ~/.codex/sessions/）
   ▼
与桌面 App 同一个会话存储
```

## 2. 关键概念：会话锁

桌面 App 打开某会话时，会留下写锁标记（`~/.codex/thread-writer-locks/<thread_id>.lock`）。

- 锁标记是**建议性**的：如果桌面 App 进程还活着，`codex_send_message` 会收到 "active writer" 错误并翻译成友好提示；
- 桌面 App **退出后**，残留的锁标记会变成"陈旧锁"，codex 会在写入时自动接管——此时**可以正常发消息**；
- `codex_runtime_status` 返回的 `heldThreadIds` 只是锁文件清单，**可能包含陈旧锁**，不能据此断定不能写。真正的占用与否，以 `codex_send_message` 的返回为准。

## 3. 工具详述

### 3.1 codex_runtime_status — 运行环境状态

**参数：** 无

**返回：**

```json
{
  "ok": true,
  "bin": "C:\\Users\\liws\\AppData\\Local\\OpenAI\\Codex\\bin\\e305f1c75d8da435\\codex.exe",
  "version": "codex-cli 0.148.0-alpha.9",
  "codexHome": "C:\\Users\\liws\\.codex",
  "executionPolicy": { "sandbox": "danger-full-access", "approvalPolicy": "never" },
  "sessionsDirExists": true,
  "threadsHeldByDesktopApp": 2,
  "heldThreadIds": ["019ff295-...", "01a00b6f-..."],
  "note": "..."
}
```

**用途：** 启动时先调一次，确认环境可用、拿到当前被桌面 App 占用的会话清单。

#### Bridge 执行权限

当前项目的 bridge 会以 `danger-full-access` 与 `never` 启动 app-server，并把策略显式传递给新建、恢复和启动的回合。`executionPolicy` 是调用方确认该策略的权威状态字段。完全访问让 Codex 在当前 Windows 用户权限下执行文件、进程和网络操作；调用方应仅在可信网络与可信操作者范围内将消息转发给 bridge。

可在启动 MCP 前设置 `CODEX_MCP_SANDBOX` 和 `CODEX_MCP_APPROVAL_POLICY` 覆盖策略。移动端 bridge 不实现交互审批回调，因此正常的自动化运行应保持 `approvalPolicy=never`。此策略不会忽略桌面 App 对已打开会话的 writer lock。

### 3.2 codex_list_sessions — 列出会话

**参数：**

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `limit` | int | 否 | 返回条数，默认 50，最大 200 |
| `cursor` | string | 否 | 分页游标（用返回里的 `nextCursor`） |
| `name_filter` | string | 否 | 大小写不敏感的子串匹配，对会话名和首条摘要生效 |

**返回：** `{ "ok": true, "sessions": [...], "nextCursor": "...", "backwardsCursor": "..." }`

每条 session 摘要字段：`id`（会话 ID，之后所有操作都用它）、`name`、`cwd`、`status`、`updatedAt`（Unix 秒）、`cliVersion`、`source`、`preview`（首条消息前 200 字）。

**典型用法：** 让用户选会话 → `list_sessions(name_filter=用户输入的关键词)` → 展示列表让用户挑选。

### 3.3 codex_start_session — 新建会话

**参数：**

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `name` | string | 否 | 会话名（通过 `thread/name/set` 设置；`thread/start` 本身忽略 name） |
| `cwd` | string | 否 | 新会话的工作目录（默认继承 app-server 的 cwd） |

**返回：**

```json
{ "ok": true, "threadId": "01a00f0c-...", "sessionId": "01a00f0c-...", "name": "mcp-e2e-test", "cwd": "D:\\Newproj\\202608\\CodexHTML", "status": "idle" }
```

**注意：** 新会话是**空的**，`status` 为 `idle`，还没有任何消息；要让它真正干活，需接着调 `codex_send_message(thread_id, 首条消息)`。新建后若想删除，协议层有 `thread/delete`（本 server 未暴露为工具）。

### 3.4 codex_read_session — 读取会话

**参数：**

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `thread_id` | string | 是 | 会话 ID（来自 list_sessions） |
| `max_messages` | int | 否 | 返回最近 N 条消息，默认 30，0 表示全部 |

**返回：** `{ "ok": true, "threadId": "...", "name": "...", "cwd": "...", "status": "...", "totalTurns": 12, "messages": [{"role": "user"|"assistant", "text": "..."}] }`

**注意：** 工具事件（工具调用、文件读写等）会被省略，只保留 user/assistant 的文本消息。会话正被桌面 App 锁定时可能读不到（会返回错误说明）。

### 3.5 codex_send_message — 向会话发消息（核心）

**参数：**

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `thread_id` | string | 是 | 目标会话 ID |
| `message` | string | 是 | 要转发给 Codex 的用户反馈 |
| `timeout_seconds` | float | 否 | 等待回复的超时秒数，默认 180，最长不要超过 600 |

**返回（三种状态）：**

```json
// 正常完成
{ "ok": true, "status": "completed", "threadId": "...", "turnId": "...", "reply": "Codex 的最终回复文本...", "elapsedSeconds": 42.3 }

// 超时（会话仍在后台跑，可中断）
{ "ok": true, "status": "running", "threadId": "...", "turnId": "...", "error": "turn still running after 180s; call codex_interrupt_turn to stop it" }

// 错误（如会话被锁、会话 ID 无效）
{ "ok": false, "status": "error", "error": "session xxx is locked: ..." }
```

**用法规则：**
1. （可选）调 `codex_runtime_status` 看整体环境，但不要因为会话出现在 `heldThreadIds` 就放弃——桌面 App 退出后那是陈旧锁；
2. 直接调 `codex_send_message(thread_id, message)`；
3. 若返回 `status == "error"` 且提示 locked，说明桌面 App 正在占用该会话，让用户先退出桌面 App 或关掉该会话；
4. 若 `status == "running"`，询问用户是否继续等或中断（调 `codex_interrupt_turn`）。

### 3.6 codex_interrupt_turn — 中断回合

**参数：** `thread_id`（必填）

**返回：** `{ "ok": true, "result": {...} }` 或 `{ "ok": false, "error": "..." }`

### 3.7 harness_service_control — 管理 H5 harness 生命周期

参数 `action` 取值 `status`、`start`、`stop`、`restart`。动作由一次性 Windows 计划任务执行，因此 MCP 可以请求重启当前 harness，而不会因为 harness 被 stop 而丢失后续 start。

## 4. 给本地模型的调用策略（建议）

1. **首次连接** → `codex_runtime_status`，检查 `ok`（bin 与会话存储健康）；
2. **用户问"Codex 在做什么 / 有哪些会话"** → `codex_list_sessions`，必要时 `codex_read_session` 展开最近内容；
3. **用户要开新话题** → `codex_start_session`（可带 `name`/`cwd`），拿到新 `threadId` 后 `codex_send_message` 发首条消息；
4. **用户说"把这个反馈发给 Codex"** → 直接 `codex_send_message`，被真正占用时返回锁错误，再提示用户处理；
5. **回复超时** → 提示用户，可 `codex_interrupt_turn`。

## 5. 启动方式

### stdio（推荐给 MCP Client 框架）

```
$env:CODEX_MCP_SANDBOX = "danger-full-access"
$env:CODEX_MCP_APPROVAL_POLICY = "never"
uv run --project subprojects/codex-mcp-server python -m codex_mcp.server
```

配置成 MCP Server（以 Claude Code 的 `.mcp.json` 为例）：

```json
{
  "mcpServers": {
    "codex": {
      "command": "uv",
      "env": {
        "CODEX_MCP_SANDBOX": "danger-full-access",
        "CODEX_MCP_APPROVAL_POLICY": "never"
      },
      "args": [
        "run", "--project", "D:/Newproj/202608/CodexHTML/subprojects/codex-mcp-server",
        "python", "-m", "codex_mcp.server"
      ]
    }
  }
}
```

### HTTP（给不走标准 MCP 的本地模型用）

```
$env:CODEX_MCP_SANDBOX = "danger-full-access"
$env:CODEX_MCP_APPROVAL_POLICY = "never"
uv run --project subprojects/codex-mcp-server python -m codex_mcp.server --transport http --port 8765
```

端点：`http://127.0.0.1:8765/mcp`（Streamable HTTP，MCP 协议）。

## 6. 环境要求

- Windows（开发目标是本机）；
- 已安装 Codex / ChatGPT 桌面应用（提供 `codex.exe`，自动从 `~/.codex/config.toml` 解析路径）；
- 会话存储位于 `~/.codex/sessions/`（与桌面 App 共享）；
- Python 3.11+，依赖 `mcp`（MCP SDK）。

## 7. 常见错误

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| `session xxx is locked` | 会话在桌面 App 里开着 | 让用户先在桌面 App 关掉该标签页，再重试 |
| `ok: false` 且 error 为 `[WinError 2]` | 找不到 codex.exe | 检查桌面 App 是否安装、`config.toml` 是否写有 CODEX_CLI_PATH |
| `turn still running after Ns` | 超时 | 等一会再读，或 `codex_interrupt_turn` |
| 读会话得到空消息 | 会话刚创建无消息 / 被锁 | 换个会话或稍后再试 |
