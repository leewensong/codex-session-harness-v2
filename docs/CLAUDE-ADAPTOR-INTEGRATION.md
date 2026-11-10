# Claude adaptor：当前能力、V2 接入与测试说明

> 更新日期：2026-10-08。依据本机当前源码和本次离线测试整理。
> 本文是接入现状和验收指南，不是 Claude Desktop 官方接口承诺。
> 本次未向真实 Claude 会话追加消息、未启动 V2 实例、未重启现有服务。

## 1. 结论与版本变化

**可将现有 Claude adaptor 接入 V2 测试，无需重新开发一个 adaptor。** V2 已有统一 adaptor facade、registry/service 接入和依赖声明。

需要区分两条后端路径：

| 能力 | `backend: "cli"` | `backend: "sdk"` |
|---|---|---|
| 扫描本机 Claude 项目/会话 | 支持 | 支持，复用同一 history 层 |
| 读取 Desktop 既有会话 | 支持最近消息 | 支持最近消息及完整主会话 transcript |
| 向 Harness 自建会话追加用户消息、获取模型回复 | 支持 | 支持 |
| 续写 Desktop/外部既有会话 | 不支持，返回 `session_not_owned` | 显式 `allowExternalSessions: true` 后支持；还须有有效原始 cwd |
| 文本流式回复 | CLI `stream-json` | SDK partial text events |
| 中断 | adaptor 自己启动的 CLI 进程 | adaptor 自己发起的 SDK turn |
| Desktop 页面实时刷新 | 不保证 | 不保证 |
| CDP / 操作 Desktop 输入框 | 不使用 | 不使用 |

2026-09 的实现将外部会话设为只读；**2026-10-08 当前源码新增 `ClaudeSdkAdaptor`，不能继续将“外部会话全部只读”作为整体结论。** CLI 兼容路径没有改变。

“添加回复”在本文中指：向原有 session 追加一条用户消息，由模型执行并产生新的 assistant 回复；不是手工伪造 assistant message 或编辑 JSONL。

### 当前 V2 已配置什么

配置文件：

`D:\Newproj\202608\CodexHTML\subprojects\codex-session-harness-v2\config\adaptors.json`

当前 Claude 条目如下，模型名只是本机配置快照，不代表推荐或可用性保证：

```json
{
  "id": "claude-local",
  "type": "claude-code",
  "enabled": true,
  "cwd": "../../claude-code-bridge",
  "stateDir": "../../claude-code-bridge/data/harness-claude",
  "envMode": "inherit",
  "backend": "sdk",
  "allowExternalSessions": true,
  "model": "glm-5.3-flash:cloud",
  "permissionMode": "acceptEdits"
}
```

配置已存在 ≠ 真实端到端发送已验收。本次只验证离线路径；真实模型调用、历史落盘和 V2 页面呈现仍按第 8 节逐项验收。

## 2. 组件位置与集成边界

### 共享 adaptor 包

目录：`D:\Newproj\202608\CodexHTML\subprojects\claude-code-bridge`

| 文件（完整路径） | 职责 |
|---|---|
| `D:\Newproj\202608\CodexHTML\subprojects\claude-code-bridge\agent_adaptors\contracts.py` | contract v1、capabilities、标准错误 |
| `D:\Newproj\202608\CodexHTML\subprojects\claude-code-bridge\agent_adaptors\config.py` | allowlisted factories；由 `backend` 选择 CLI/SDK |
| `D:\Newproj\202608\CodexHTML\subprojects\claude-code-bridge\agent_adaptors\registry.py` | 多实例装载、namespaced ID 路由、probe、关闭 |
| `D:\Newproj\202608\CodexHTML\subprojects\claude-code-bridge\agent_adaptors\service.py` | list/read/full-read/resume、start-turn/poll/interrupt |
| `D:\Newproj\202608\CodexHTML\subprojects\claude-code-bridge\agent_adaptors\claude.py` | CLI 后端、bridge-owned 状态和 lease |
| `D:\Newproj\202608\CodexHTML\subprojects\claude-code-bridge\agent_adaptors\claude_sdk.py` | SDK 后端、原 UUID + 原 cwd 的外部会话续写 |
| `D:\Newproj\202608\CodexHTML\subprojects\claude-code-bridge\agent_adaptors\history.py` | 本地项目发现、JSONL 最近/完整读取 |
| `D:\Newproj\202608\CodexHTML\subprojects\claude-code-bridge\agent_adaptors\server.py` | 可选统一 MCP facade；V2 直接导入时不需要单独启动 |

### V2 的真实调用链

```text
V2 WebSocket session.send
  -> HarnessServer.handle_command
  -> ManagedAssistant.send(thread_id=目标会话)
  -> UnifiedHarnessAdapter.send_turn
  -> AdaptorService.start_turn / poll
  -> registry.route("claude-local:<UUID>")
  -> ClaudeSdkAdaptor.send_turn
  -> ClaudeSDKClient，options.resume=<原 UUID>，options.cwd=<原会话 cwd>
  -> Claude runtime 负责执行与历史持久化
```

V2 接入文件：

- `D:\Newproj\202608\CodexHTML\subprojects\codex-session-harness-v2\app\adapters\unified.py`
- `D:\Newproj\202608\CodexHTML\subprojects\codex-session-harness-v2\app\gateway\server.py`
- `D:\Newproj\202608\CodexHTML\subprojects\codex-session-harness-v2\app\orchestrator\session_manager.py`
- `D:\Newproj\202608\CodexHTML\subprojects\codex-session-harness-v2\pyproject.toml`

V2 的 `unified` extra 指向共享包的 editable 本地依赖，并启用 `mcp,sdk` extras。当前 SDK 固定版本为 `claude-agent-sdk==0.2.139`。

不要把语音前端协议直接照搬进 V2：语音页面使用 `agent.session.send`，V2 使用 `session.send`；V2 返回统一事件 envelope，业务数据位于 `payload`。

## 3. 项目发现、历史与 session ID

### 数据来源

默认扫描：`C:\Users\liws\.claude\projects`

扫描所有一级项目目录中的主会话 `*.jsonl`，不是只扫描 adaptor 的配置 cwd。以 transcript 内的真实 cwd 取目录末级作为项目名，以 `custom-title` 作为会话标题；没有标题时可能显示 UUID。项目返回包含无会话目录，也可能包含临时目录。

范围限于已落地本机的 Claude Code transcript。未本地落盘的远程记录、Cowork 独立存储内容，不应宣称全部覆盖。项目数、会话数会变化，不使用之前的 18/79 或 22/101 作为固定验收指标。

### 路由规则

- public ID：`claude-local:<canonical UUID>`。
- native ID：原始 Claude session UUID。
- SDK 续写既有会话：使用原 UUID 和该会话的真实 cwd，**不是**一律使用 bridge 项目目录。
- SDK 不将外部会话登记成 bridge-owned，不手工改写 JSONL 或 provider 锁。
- Harness 自建空会话：先保留 UUID，首次发送用 `session_id`；后续用 `resume`。
- namespace 保持稳定，不跨 Codex/Claude resume，不自动 provider fallback。

### 完整读取不等于“全部附件已下载”

SDK 支持 `AdaptorService.read_full_session(session_id)`；V2 facade 检测到该能力后，`session.read` 实际走 full-read。

返回：

- `messages`：可读投影，包括内容 blocks；不是最后 30/50 条窗口。
- `records`：有效 JSONL object 及原行号，保留工具、附件块、元数据等原始记录。
- `diagnostics.complete`、`parseWarnings`、`messageCount`、`recordCount` 等。

边界：

- 读取主会话文件的快照，不声称正确重建活动分支；`branchAware=false`。
- 不嵌入引用的图片/文件二进制，不包含独立 subagent transcript。
- 坏行使 `complete=false`；超过 64 MiB 返回 `history_too_large`，不会偷偷截断并声称完整。
- V2 Gateway 仍要求 `limit` 为 30/100/200，但 SDK full-read 不按这个值截为最近 N 条；界面应明确这一差异。
- 大历史可能触发客户端 WebSocket 接收上限或渲染性能问题，必须单独测试。

## 4. 参数、状态与权限

| 参数 | 当前意义与注意点 |
|---|---|
| `type` | 保持 `claude-code`，SDK 后端不是新的 Agent 类型 |
| `backend` | 默认 `cli`；续写外部会话须显式用 `sdk` |
| `allowExternalSessions` | SDK 外部会话写开关；默认 false |
| `cwd` | Harness 新建会话的默认 cwd；外部续写会从 transcript 读取原 cwd |
| `stateDir` | bridge ownership 状态/OS lease，路径需独占 |
| `projectsDir` | 可选 history 读取根；默认用户 `.claude/projects`；不等于替 SDK 配置 provider 存储根 |
| `binary` | CLI executable，SDK 时映射到 `cli_path`；不指定则依赖 runtime/SDK 的查找行为 |
| `envMode` | CLI 的 inherit/minimal；SDK 目前没有调用 CLI `_env()`，不要以为 minimal 已对 SDK 做同等环境隔离 |
| `tools` | 可选工具名单；首轮真实验收建议 `[]`，关闭工具执行 |
| `model` | SDK 模型覆盖；需与本机 provider 路由匹配，不写入 token |
| `permissionMode` | SDK 允许 dontAsk/default/acceptEdits/plan；没有自动启用 bypassPermissions |

相对 cwd/stateDir/projectsDir 以 **配置文件所在目录** 为基准。认证及 provider 路由来自运行进程可用的用户配置/环境；本文不复制凭据。

`acceptEdits` 允许文件编辑，不等于所有命令都获批；尚无完整交互审批 UI。测试真实续写时应使用 `tools: []` 和最小权限配置。

### 三种状态必须分开

1. enabled：配置允许装载。
2. loaded：registry 已实例化对象。
3. runtime.ok：probe 结果。

SDK 的 probe 主要检查 SDK 模块/Client 是否可用，`authenticationVerified=false`；不证明认证、模型或网络调用成功。`writable=true` 也只代表开关和 cwd 条件满足，不证明 Desktop 空闲或续写一定成功。

## 5. V2 已接入与尚需补齐的部分

### 已接入

- `--adaptor-config` 装载多个 adaptor；`--agent-type claude` 可选 Claude 初始执行实例。
- V2 独立 managed-session 状态按配置路径 hash 和 Agent 类型隔离。
- `session.send` 接收精确 threadId，不必把消息发给 managed 小助理。
- SDK facade 支持完整读取和 start-turn/poll 文本流。
- `agent.select` 后端支持切换；执行中/排队时拒绝切换，不迁移正在运行的 turn。

### 本次核对出的接入缺口

1. **列表未自动合并**：V2 `sessions.list` 当前返回选中 adaptor 的一页（100 条），不是所有 adaptor 的聚合索引。需要跟随 nextCursor；如要让 V2 小助理查询全部 Agent，应迁移语音 Harness 的合并目录思路，另做 provider-neutral 查询层。
2. **状态摘要落后于 SDK 配置**：V2 pool 的安全参数白名单没有 backend/allowExternalSessions/model/permissionMode，而且 Claude 摘要硬编码 `dontAsk`。实际 SDK 当前为 acceptEdits。测试应以配置文件和 adaptor executionPolicy/capabilities 为准，后续需修正 GUI 摘要，不能仅看卡片认定配置生效。
3. **前端入口职责保持不变**：用户-facing Agent 选择应放主 Web UI 首页，Harness GUI 负责状态。后端存在 `agent.select` 不代表 GUI 已有选择入口；V2 联调可先用 `--agent-type claude` 或 WebSocket 显式选择。
4. **混合读取要增加显式防线**：V2 facade 的 send/resume/interrupt 验证目标属于选中 adaptor；read 直接通过 service namespace 路由。后续需明确允许跨 adaptor 只读还是拒绝，不能把所有操作都描述为相同隔离规则。
5. **真实端到端回归待做**：共享包模拟 SDK 测试不等于 V2 WebSocket + 真实 SDK + 历史落盘 + UI 全链路测试通过。
6. **发送后页面同步待验收**：必须在成功、错误和超时后重新读取同一会话；不以 Desktop 页面是否刷新作为唯一判断。

## 6. 隔离测试配置与启动

先做离线测试；确认允许真实模型调用后再启动真实 V2。

不要直接复用正在运行的语音 Harness 的 `stateDir`。当前生产条目和 V2 都可能指向同一个 `data/harness-claude`，第二个实例会返回 `state_in_use`。

以下 PowerShell 命令仅创建**另一个测试配置**，不覆盖原配置：

```powershell
$V2 = 'D:\Newproj\202608\CodexHTML\subprojects\codex-session-harness-v2'
$Bridge = 'D:\Newproj\202608\CodexHTML\subprojects\claude-code-bridge'
$SourceConfig = Join-Path $V2 'config\adaptors.json'
$TestConfig = Join-Path $V2 'config\adaptors.claude-sdk-test.json'

$config = Get-Content -LiteralPath $SourceConfig -Raw | ConvertFrom-Json
foreach ($entry in $config.adaptors) {
    if ($entry.type -eq 'claude-code') {
        $entry.cwd = $Bridge
        $entry.stateDir = Join-Path $V2 'data\claude-sdk-test'
        $entry.backend = 'sdk'
        $entry.allowExternalSessions = $true
        $entry.permissionMode = 'dontAsk'
        $entry | Add-Member -NotePropertyName tools -NotePropertyValue @() -Force
    } else {
        $entry.enabled = $false  # 第一阶段只验收 Claude，排除 Codex 故障干扰
    }
}
$config | ConvertTo-Json -Depth 12 | Set-Content -LiteralPath $TestConfig -Encoding utf8

Set-Location -LiteralPath $V2
uv sync --locked --extra unified

# 8056 仅作测试示例；先检查实际可用，不与主 UI 的 8050/8060 混用。
Get-NetTCPConnection -LocalPort 8056 -State Listen -ErrorAction SilentlyContinue
# 若已占用，改用另一个 >=8050 的可用端口；避开 7950-8049。
uv run --locked --extra unified python -m app.gateway.server `
  --adaptor-config $TestConfig --agent-type claude --host 127.0.0.1 --port 8056
```

V2 启动会对 managed 会话做 Skill 初始化；新建或需要重初始化时会发送真实模型请求并写入历史，**不是只读探测**。这也是本次不自动启动的原因。

测试服务默认仅监听 loopback，不暴露到公网；测试停止后在前台 Ctrl+C，正常释放 service/lease。

双 adaptor 阶段再启用 Codex 条目，并确认其 endpoint 可用。`start-unified.ps1` 当前默认 `--agent-type codex`，不能用它代替上面的 Claude 初始测试命令。

## 7. V2 WebSocket 联调协议

以下 JSON 发送到测试 V2 的 `/ws`，不发送到 `8765/mcp`：

```json
{"type":"agent.status"}
{"type":"agent.select","agentType":"claude"}
{"type":"sessions.list","requestId":"claude-list-1"}
{"type":"session.read","threadId":"claude-local:<目标 UUID>","limit":30,"requestId":"claude-read-1"}
```

从 `sessions.snapshot.payload.sessions` 取精确 id；不要猜 UUID，也不要把 Codex native ID 传给 Claude。

**以下发送会追加真实消息，必须先选定可修改的测试会话：**

```json
{
  "type": "session.send",
  "threadId": "claude-local:<目标 UUID>",
  "clientMessageId": "<本次请求的唯一 ID>",
  "requestId": "claude-send-1",
  "text": "不调用任何工具，不修改任何文件。计算 13 × 17，只回复 V2_CLAUDE_TEST_OK= 后接结果。"
}
```

期望：`chat.status` → `chat.delta`（可能若干）→ `chat.completed`，或 `chat.error`；参数错误会返回 `command.error`。事件 threadId 必须与目标一致。`chat.send` 若不传 threadId，会发往 managed 会话，而不是当前想测试的 Desktop 会话。

回合后再次 `session.read`，检查新用户消息与回复是否落盘。同一进程内相同 clientMessageId/text/target 不会重复派发；不声称跨进程、跨重启 exactly-once。

`chat.retry` 是显式重发，会产生另一个 ID；错误/超时后先读历史，不要直接 retry。

## 8. 验收清单：从无副作用到真实续写

| 阶段 | 验收点 | 是否调用模型/改变会话 |
|---|---|---|
| A：离线回归 | registry、namespace、历史发现、模拟 SDK 原 UUID/cwd、禁用写、错误/超时 | 否；仅临时文件和 fixture |
| B：真实只读 | 能读目标 project/session，完整 records，坏行标识，writable 和 cwd | 不发送模型消息 |
| C：V2 启动 | SDK 依赖装载、managed 初始化、状态 | 可能发送初始化请求并写 managed 历史 |
| D：真实续写 | 对选定测试 session 发送一次无工具标记；流、终态、输入/回复落盘 | 是；需用户同意目标会话 |
| E：异常处理 | 认证/模型/权限失败、timeout、cancel，失败后重读、无自动 retry | 真实失败也可能留下部分历史 |
| F：双 adaptor | Claude/Codex 精确路由、执行中拒绝切换、分页、单 adaptor 故障 | 根据具体测试可能产生真实消息 |

### 本次已跑的离线测试（2026-10-08）

在 V2 `unified` 环境中验证：

- 共享 adaptor 包 Python：**31 项通过**（含 SDK 模拟回归）。
- V2 Python：**19 项通过**。
- V2 session utils Node：**9 项通过**。

复现命令：

```powershell
$V2 = 'D:\Newproj\202608\CodexHTML\subprojects\codex-session-harness-v2'
$Bridge = 'D:\Newproj\202608\CodexHTML\subprojects\claude-code-bridge'

Set-Location -LiteralPath $Bridge
uv run --locked --extra unified --project $V2 python -m unittest discover -s tests -p 'test_*.py' -v

Set-Location -LiteralPath $V2
uv run --locked --extra unified python -m unittest discover -s tests -p 'test_*.py' -v
node --test tests/session-utils.test.mjs
```

### 现成的真实 SDK 验收脚本

脚本：`D:\Newproj\202608\CodexHTML\subprojects\claude-code-bridge\tests\real_sdk_harness_e2e.py`

该脚本测试 registry/service + 真实 SDK，**不经过 V2 WebSocket**。默认只有 read/full-read/resume 元数据检查；只有 `--send` 才追加一次标记。必须显式给测试 session，避免误用脚本内默认 UUID。

```powershell
$V2 = 'D:\Newproj\202608\CodexHTML\subprojects\codex-session-harness-v2'
$Bridge = 'D:\Newproj\202608\CodexHTML\subprojects\claude-code-bridge'
$TestConfig = Join-Path $V2 'config\adaptors.claude-sdk-test.json'
$Session = '<由列表取得的原始 UUID，不含 claude-local: 前缀>'
Set-Location -LiteralPath $Bridge

# 不追加消息；脚本自身使用临时 stateDir，并设置 tools=[]。
uv run --locked --extra unified --project $V2 python tests\real_sdk_harness_e2e.py `
  --config $TestConfig --session $Session

# 确认目标会话可修改，并确保 Desktop 和其他 Harness 没有在写同一 session 后，才运行：
uv run --locked --extra unified --project $V2 python tests\real_sdk_harness_e2e.py `
  --config $TestConfig --session $Session --send
```

检查报告中的 `status=completed`、`inputPersisted`、`replyPersisted`、`historyStartsAtBeginning`、`complete`。再按第 7 节完成 V2 通路的独立验收。

## 9. 并发、错误与不可承诺的行为

- `stateDir` 锁保护 bridge state；**不是 Desktop 同一 UUID 的全局 writer lock**。独立测试 stateDir 不意味着可以同时写同一原会话。
- SDK 层对同一会话 active 状态防重入；V2 ManagedAssistant 对发送串行化。不提供 Desktop/Harness 跨进程 writer 协調。
- `interrupt` 仅针对本 adaptor 发起的 turn；不承诺能打断 Desktop 独立运行的 turn，不回滚文件/命令副作用。
- SDK `is_error=true` 即使 subtype=success 也判为失败。
- timeout/异常可能已经写入部分用户消息或工具记录；不自动重发，不自动换 provider。
- Desktop UI 即使暂时不显示追加内容，也不能据此重发。优先验收目标 UUID 的 JSONL 和 Harness 完整读取结果。
- 共享 service 默认单个回复/累计文本有 1,000,000 字符保留上限，stream 有保留期；完整历史读取是另一条通路，不与流缓存混为一谈。

## 10. 故障排查

| 现象/错误 | 优先检查 |
|---|---|
| `state_in_use` | 另一个进程持有相同 stateDir；使用独立测试目录，不删除 provider 锁 |
| `session_not_owned` | 仍是 CLI 或 allowExternalSessions=false |
| `project_unavailable` | transcript 原 cwd 缺失/目录不存在，不能用 bridge cwd 偷换 |
| `runtime_unavailable` | V2 Python 环境缺 SDK、Client 不可用、runtime 启动失败 |
| `provider_error` | 认证、模型路由、限额/权限等；先看 reply/历史，probe 成功不代表 API 调用成功 |
| `ambiguous_write` | 请求发出后无可靠终态；先读历史，不重发 |
| `turn_timeout` | SDK 等待超时；可能已有历史写入，不等于无副作用 |
| `history_too_large` | 主 transcript >64 MiB；需要 export/paged reader，当前没有该回退 |
| UI 摘要显示 dontAsk，但配置是 acceptEdits | V2 状态摘要旧白名单/硬编码，按实际配置和 executionPolicy 核对 |
| 列表似乎缺少后面的会话 | V2 单页 100 条，检查 nextCursor；不要把单页当全集 |
| Claude 已写成功但 Desktop 不更新 | Desktop refresh 不保证；读同一 UUID 主 transcript 确认 |

## 11. 建议的 V2 接入顺序

1. 先修正状态参数摘要，准确展示 backend / allowExternalSessions / permissionMode / model，不显示密钥。
2. 以隔离配置运行 Claude-only，验收 list/full-read，再验收一次无工具续写。
3. 确认 UI writable、pending、delta、error、完成后完整重读和请求/会话关联都正确。
4. 加入双 adaptor 回归，再设计合并列表（保留 adaptorId/provider，分页与部分故障显式报告），供小助理统一查询。
5. 最后完善并发 writer 检测、超时 outcome reconcile 和大历史分页，避免将测试版本视为成熟 Desktop 控制 API。

## 12. 相关文档

- 共享包 README：`D:\Newproj\202608\CodexHTML\subprojects\claude-code-bridge\README.md`
- Kiro requirements：`D:\Newproj\202608\CodexHTML\subprojects\claude-code-bridge\.kiro\specs\unified-agent-adaptor\requirements.md`
- Kiro design：`D:\Newproj\202608\CodexHTML\subprojects\claude-code-bridge\.kiro\specs\unified-agent-adaptor\design.md`
- Kiro tasks：`D:\Newproj\202608\CodexHTML\subprojects\claude-code-bridge\.kiro\specs\unified-agent-adaptor\tasks.md`
- V2 早期调试记录：`D:\Newproj\202608\CodexHTML\subprojects\codex-session-harness-v2\docs\DEBUGGING.md`（2026-08 记录，不替代本说明）
