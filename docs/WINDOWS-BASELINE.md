> 归档基线：2026-11-11 从原 README 保存，描述既有 Windows 部署。Mac 启动请看 [Mac 交接](MACOS-HANDOFF.md)，以仓库首页和当前源码为准。

# Codex Workspace V2

本 Harness 支持两种 agent adaptor，并通过统一 adaptor contract 接入：

- **Codex**：默认使用 V2 本地 native adaptor；只读列表/记录优先读取 Codex 的 session_index 与 rollout，写入使用本机 app-server，支持原生图片和会话级模型/强度参数。仍兼容显式配置 codex-mcp 的方式，但旧 MCP adaptor 不支持原生模型/强度覆盖。
- **Claude Code**：通过 Claude Code adaptor 访问本地 CLI 或 SDK，并读取本机 JSONL；当前配置使用 SDK，允许显式续写外部会话。项目显示工作目录末级目录名。

## 当前公网入口与网页登录

当前公网入口 `https://copyto.me:8061/` 已改为与 V1 类似的网页登录：输入原 V2 访问密码（无需用户名），验证后保留 30 天。退出地址为 `/logout`，提交后立即撤销登录。

**当前本机 8050 属于 Strata，公网 V2 使用 8055。** 推荐启动：

```powershell
.\start-public.ps1
# 等价：.\start.ps1 -Port 8055
```

frpc 中 V2 的 `localPort` 已同步为 8055；V1 的隧道与本地业务服务不变。公网外层验证由服务器的 Nginx + 独立 systemd 服务负责，本机/局域网直连不经过这层。部署、测试、退出、密码轮换与回滚说明见 `docs/EDGE-WEB-ACCESS.md`。

## 启动双 adaptor 模式

默认配置不再依赖 8765 MCP 服务，且 Claude 使用 V2 独立状态目录 `data/claude-sdk-v2/`（配置字段 `v2StateDir`），V1 若引用同一配置仍沿用原 `stateDir`，V2 优先使用 `v2StateDir`，不会与 V1 的 bridge 状态锁冲突。执行：

```powershell
.\start.ps1
# 或：.\start-unified.ps1
```

或：

```powershell
uv run --extra unified --project . python -m app.gateway.server `
  --adaptor-config config\adaptors.json `
  --agent-type codex `
  --port 8050
```

后端可通过 `--agent-type codex|claude` 或 `agent.select` 在 Codex 与 Claude Code 之间切换；用户选择入口属于主 Web UI 首页，Harness GUI 负责 adaptor 状态。切换时：

- 正在执行 turn 时拒绝切换；
- 两个 agent 使用独立的 managed-session 状态目录；
- session ID 保留 `adaptorId:nativeId` namespace，不跨 provider resume；
- 会话列表只返回当前选中 agent 的会话；
- 一个 adaptor 不可用时保留选项并标记不可用，不自动 fallback。

## 局域网访问和 Desktop 风格界面

默认 `.\start.ps1` / `.\start-unified.ps1` 监听 `0.0.0.0:8050`，本机和同一受信任局域网内的设备都可连接：

```text
http://127.0.0.1:8050/
http://<LAN-IP>:8050/  # 示例为本机当前 Wi-Fi 地址；换机/换网后以实际 IP 为准
```

如果只希望本机访问：

```powershell
.\start.ps1 -BindAddress 127.0.0.1
```

- 页面左侧使用 **项目 → 会话** 两层可展开结构；每个项目默认显示最近 5 个会话，可展开更多。支持搜索、状态筛选、排序以及单项目/全部项目折叠。
- 项目展开状态按 Agent 保存在浏览器；搜索临时展开匹配结果，不覆盖原来的折叠状态。
- 左上角直接切换 Codex / Claude，并同步切换图标、页面标题和侧栏配色。
- 输入框右下角直接选择模型与思考强度，选择后自动保存到当前会话；保存确认前禁用发送。仍可通过旁边的设置按钮输入自定义模型、查看历史参数或恢复默认。
- 普通局域网 HTTP 不一定提供 `crypto.randomUUID` 和 Clipboard API。页面使用安全随机数生成兼容请求 ID，复制操作也提供后备方式，不再要求 loopback / HTTPS 才能加载会话。
- 本机已可通过 LAN IP 直接访问并建立 WebSocket；其他设备还需连接同一局域网，Windows 防火墙允许对应端口。若系统/浏览器配置了代理，私网地址应走直连；未自动修改防火墙或系统代理。
- **直连仅用于受信任局域网**：本机 HTTP 端口可读写真实 Agent 会话，不自带网页登录。公网应只通过已配置的 HTTPS 8061 登录入口访问；不要单独将本机端口或 FRP 后端端口开放给公网。

## 项目内新建会话

项目名称右侧的 **＋** 会在该项目下新建空会话，自动展开项目、选中“新会话”并将焦点放到输入框。

- 使用当前选中的 Agent（Codex / Claude），目标目录由服务器根据项目中的现有会话确定；不会误用 adaptor 默认目录，也不会按同名文件夹合并。
- Codex native 调用 `thread/start(cwd=目标目录)`；Claude SDK 预留持久化 UUID，第一条消息使用 `session_id`，后续使用 `resume`，两者始终带原项目的 `cwd`。
- 新建不发送任何初始化/用户消息，不触发模型回复。Claude 空会话在首次发送前保存在 V2 独立状态中，不能据此承诺立即出现在 Desktop。
- V2 的空会话元数据写入当前 Agent 的 `created-sessions.json`，即使原生索引还未包含空会话，刷新或服务重启后仍可看到。
- 新建期间禁用“＋”和 Agent 切换；相同请求 ID 重复提交只返回已创建的会话。超时不会自动重复创建，请先刷新列表确认。
- 当前项目目录已移动/删除时会明确报错。显式配置的旧 MCP/CLI adaptor 若不支持指定项目创建，会禁用“＋”，不静默创建到其他目录。

## 会话级模型与思考强度

选中会话后，标题下显示最近一轮实际记录的模型和强度；在输入框右下角直接选择下一轮模型和强度，或点击旁边的“会话模型设置”查看详情、输入自定义模型和恢复默认。

- 读取真实会话元数据：Codex 从最新 `turn_context` 读取，Claude 从主会话 JSONL 的 assistant.model 读取。不从回答内容猜测模型或强度。
- Claude 的旧历史不一定记录 effort；这种情况显示“未知”，与默认值和下一轮覆盖值分开显示。
- 每个会话独立保存设置，写入 `data/adaptors/<配置标识>/<agent>/session-preferences.json`（兼容模式写入 `data/session-preferences.json`）。设置不修改 provider 历史文件或全局 config。
- 保存只影响 **下一次通过 V2 发送消息**：Codex 将参数放入 turn/start 的 model/effort；Claude 使用当前 SDK 的 model/effort 选项。不会立即同步 Desktop UI，也不会改变正在运行的一轮。
- 已知 Codex 模型只显示其目录声明的强度。Claude SDK 接受 low/medium/high/xhigh/max，但第三方模型是否支持仍由服务商确认。
- SDK“可用”表示依赖和适配器可加载，并不是登录、配额、模型权限的成功推理验证。认证和供应商错误在用户发送时反馈，不自动重发。
- 默认启动只探测和浏览，不创建/初始化新助手会话，避免打开列表就产生模型调用；如确需旧受管助手行为，传入 `--initialize-assistant`。

## 图片与文件附件

- 在输入框左侧点附件按钮，或拖入文件、粘贴截图。上传后可以预览、移除，上传本身不触发模型请求。
- 每条消息最多 8 个附件，单文件最大 20 MB，总大小最大 64 MB。允许只发送附件。
- Codex native 的图片以 localImage 输入传递；文件以保存在本机的路径引用传递。Claude 当前以文件引用方式接入，不能把它当成已验证的原生视觉输入。
- PNG/JPEG/GIF/WebP 可以预览；SVG、HTML、脚本等只提供下载，不内联执行。历史外部图片需要用户点击才加载，不默认请求第三方 URL。
- 附件绑定目标会话和 Agent，客户端只能发送服务器返回的附件 ID。文件保存于 `data/attachments/`，不会自动删除已发送附件。

## 验证

```powershell
uv run --extra unified python -m unittest discover -s tests -v
node --test tests/*.test.mjs
```

## 兼容旧模式

```powershell
.\start-fake.ps1
.\start.ps1 -LegacyCodex
```

`--managed-adaptor` 仍可用于选择初始 adaptor；`--agent-type codex|claude` 是新的 UI/运行时选择入口。

## Claude SDK 接入与测试说明（2026-10-08）

当前 `claude-code` adaptor 同时支持 CLI 和显式 SDK 后端。SDK 配合 `allowExternalSessions: true` 可续写本机 Desktop 既有会话；CLI 外部会话仍只读。V2 配置已启用 SDK，但真实发送需独立验收。

详细现状、参数、V2 接入缺口、隔离测试步骤与离线回归结果见：

[CLAUDE-ADAPTOR-INTEGRATION.md](CLAUDE-ADAPTOR-INTEGRATION.md)

本文前面的 Agent 选择说明是后端兼容能力，不代表 Harness GUI 当前必须有下拉入口；主 Web UI 首页负责用户选择，Harness GUI 负责 adaptor 状态。
