# Unified Agent Adaptors / Claude Code Bridge

该项目为 Harness 提供 provider-neutral 的 agent adaptor contract，并保留 Claude Code bridge CLI。当前包含 Claude Code、现有 Codex MCP 和离线 Fake adaptor。

## 可行性与边界

统一 `runtime + session + turn + event` 可行，但 capability 必须真实报告：

| 能力 | Claude Code CLI | Codex MCP | Fake |
|---|---|---|---|
| create/resume/send | bridge-owned session | 原生 | 是 |
| stream | `stream-json` 文本 delta | start/poll | 是 |
| interrupt | 仅 adaptor 启动的进程 | 仅 adaptor 启动的 turn | 是 |
| list/read | 本地 JSONL，只读 | MCP | 内存 |
| 写外部 session | 否 | 是 | 不适用 |

关键限制：

- public session ID 固定为 `adaptorId:nativeId`，不允许跨 provider resume/migration。
- Claude CLI 后端对外部 Desktop/CLI 会话只读；SDK 后端可显式启用续写，不手工修改 JSONL、数据库或 provider 锁。
- Claude 默认 `--permission-mode dontAsk`，不自动启用 bypass permissions。
- 中断不代表工具副作用回滚。
- 写操作不自动 retry，也不因故障切换到另一个 provider。
- Codex MCP 等待超时后保留 backend stream/cursor；会话继续 busy，后续 poll 观察同一个 turn。
- 一个 adaptor 加载或 probe 失败不拖垮其他 adaptor。

完整设计见 `.kiro/specs/unified-agent-adaptor/`。

## 配置

```json
{
  "contractVersion": 1,
  "adaptors": [
    {
      "id": "claude-local",
      "type": "claude-code",
      "cwd": ".",
      "stateDir": "data/claude-local",
      "envMode": "inherit"
    },
    {
      "id": "codex-local",
      "type": "codex-mcp",
      "cwd": ".",
      "endpoint": "http://127.0.0.1:8765/mcp"
    }
  ]
}
```

相对路径以配置文件目录为基准。Claude `stateDir` 有 OS 文件锁，同一目录只允许一个 bridge 实例。

## 启动

```powershell
# runtime/capability probe
python -m agent_adaptors.cli --config examples_adaptors.json

# stdio MCP（默认）
uv run --extra mcp python -m agent_adaptors.server --config examples_adaptors.json

# localhost HTTP MCP；Windows 项目策略避开 7950-8049
uv run --extra mcp python -m agent_adaptors.server --config examples_adaptors.json --transport http --port 8051
```

统一 MCP 工具：

- `agent_runtime_status`
- `agent_list_sessions`
- `agent_start_session`
- `agent_resume_session`
- `agent_read_session`
- `agent_send_message`
- `agent_start_stream_message`
- `agent_poll_stream`
- `agent_interrupt_turn`

## bridge.py 兼容 CLI

```powershell
python bridge.py list <folder>
python bridge.py read <folder> <session_id>
python bridge.py new <folder> "message"
python bridge.py send <folder> <owned_session_id> "message"
```

输出为单个 JSON 文档；错误不会回显 raw stderr。默认 state 目录为 `<folder>/.claude-bridge`，可用 `--state-dir` 或 `CLAUDE_BRIDGE_STATE_DIR` 指定。

## Harness v2 接入

`codex-session-harness-v2` 保留旧 Codex 模式，并新增：

```powershell
uv run --extra unified python -m app.gateway.server `
  --adaptor-config ..\claude-code-bridge\examples_adaptors.json `
  --managed-adaptor claude-local `
  --port 8050
```

Harness 可加载多个 adaptor；`--managed-adaptor` 只选择负责主聊天的实例。`sessions.list` 聚合已加载实例，`session.read` 根据 namespace 路由。运行中不会自动 fallback。

## 测试

```powershell
python -m unittest discover -s tests -v
uv run --extra mcp python tests/mcp_stdio_e2e.py
```

真实 Claude 调用会创建/更新会话，因此不纳入默认测试；本机已验证 Claude Code 2.1.226 的 JSON 和 `stream-json` 路径。

## 故障排查

- `state_in_use`：另一 bridge/Harness 实例持有相同 Claude state 目录，关闭该实例；不要删除 provider 锁。
- `session_not_owned`：该 Claude session 来自外部 Desktop/CLI，只能 list/read。
- `permission_denied`：headless `dontAsk` 没有交互审批通道，调整明确允许的工具策略，不要自动 bypass。
- `transport_error`/`ambiguous_write`：Codex MCP 写结果可能不确定，不要重发；先 interrupt/reconcile backend。
- Codex MCP adaptor 需要现有 `codex-mcp-server` 先在配置 endpoint 提供服务。


## Harness SDK continuation (2026-10-08)

The existing `claude-code` type now supports explicit `backend: "sdk"`.
The Harness config at `../codex-session-harness-v2/config/adaptors.json` enables
`allowExternalSessions: true`. Default CLI behavior is unchanged (external sessions
remain read-only unless the SDK backend is explicitly enabled).

Install with `uv pip install --python <harness-python> "claude-agent-sdk==0.2.139"`
or install this package with the `sdk` extra. The voice demo requirements include
that tested SDK version. The current local model override is `glm-5.3-flash:cloud`;
credentials/provider routing come from the user's Claude settings (no credential
is stored in adaptor config). Update the model in adaptor config if the provider changes.

- Send: same `claude-local:UUID` via `AdaptorService.start_turn`/`poll`. SDK options
  use `resume=UUID` and the original session's cwd. External history is never manually
  edited or adopted into bridge-owned state. The SDK writes it.
- Read: `AdaptorService.read_full_session` / MCP `agent_read_full_session`. `records`
  retains every valid main-session JSONL object, including metadata, tools and attachment
  blocks; `messages` is a readable projection. No last-30/50 window. Malformed rows set
  `complete=false`; >64 MiB fails explicitly. Referenced files/images and separate
  subagent transcripts are not embedded, and active-branch reconstruction is not claimed.
- The voice frontend uses full reads and refreshes after each turn (including errors).
- `permissionMode: acceptEdits` permits file edits; other operations may be denied without
  an interactive approval UI. This does NOT silently enable bypassPermissions.
- Turns are serialized in Harness; never write the same session from Desktop at the same time.
- API errors (including `is_error=true` with `subtype=success`) are failures, not successes.
  Timeout/error never automatically resends. Read history before deciding to retry.
- Desktop UI refresh is not required or guaranteed. CDP is not involved.

Offline regression: `python -m unittest discover -s tests -v`.
Opt-in real read: `python tests/real_sdk_harness_e2e.py`.
Opt-in harmless real append: `python tests/real_sdk_harness_e2e.py --send`.
