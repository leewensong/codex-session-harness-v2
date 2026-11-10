# macOS 版开发说明

更新日期：2026-11-11。对象：在另一台 Mac 上接手 V2 的开发者。

本文以当前仓库源码为基线。当前完成的是源码整理、依赖收纳和开发交接；尚未在 macOS 主机上安装、启动或验收。文中的 macOS 测试项都是后续验收要求，不能据此宣称 Mac 版已经可用。首次克隆、环境准备和交接记录见 [MACOS-HANDOFF.md](MACOS-HANDOFF.md)。

## 目标与范围

第一阶段交付在 Mac 本机运行的 V2 Web 工作台：查看本机 Codex 项目和会话、阅读记录、在正确项目中新建会话、发送消息、流式显示、停止当前轮次、设置会话模型和思考强度、上传附件。继续复用 Python 后端和现有 Web UI。

Claude Code 是随后独立验收的第二种 adaptor。首轮使用 `config/adaptors.macos.example.json`，仅启用 Codex native。Mac 上的登录、模型权限与供应商设置由该机器配置。不要沿用 Windows 开发机的第三方模型名作为通用默认值。

原生 `.app` 包装、菜单栏、自动更新、签名、公证、安装包、开机启动和远程访问都属于后续工作。目前没有这些能力的完成承诺，也没有经实机验证的最低 macOS 版本要求。

## 当前架构与代码入口

```text
浏览器：app/web/index.html + workspace.js / workspace.css / *.mjs
    │ HTTP + /ws
Python 网关：app/gateway/server.py
    │
会话编排：app/orchestrator/session_manager.py
    │
统一 facade / pool：app/adapters/unified.py
    │
V2 adaptor 工厂：app/adapters/local_registry.py
    ├─ codex-native → app/adapters/codex/adapter.py
    │                 ├─ local_store.py：只读本机持久化记录
    │                 └─ rpc.py：启动 codex app-server，stdio JSON-RPC
    └─ claude-code → vendor/claude-code-bridge/agent_adaptors/
                      ├─ history.py：读取 Claude 项目 JSONL
                      ├─ claude_sdk.py：显式 session UUID / resume
                      └─ lease.py：本项目 bridge 状态目录的 OS 文件锁
```

| 文件或目录 | 接手时应了解的职责 |
| --- | --- |
| `pyproject.toml`、`uv.lock` | Python 至少 3.11；`unified` extra 安装统一 adaptor 及 MCP/SDK 依赖；本地 adaptor 源随仓库放在 `vendor/claude-code-bridge` |
| `app/gateway/server.py` | 参数解析、监听地址、健康检查、WebSocket 命令、附件路由和退出清理；默认数据根为仓库下 `data/` |
| `app/orchestrator/session_manager.py` | 浏览模式、会话选择、消息状态、幂等请求、项目内创建和持久化协调 |
| `app/adapters/unified.py` | Codex / Claude 选择、会话 namespace、运行状态和配置展示 |
| `app/adapters/local_registry.py` | `codex-native` 接入、V2 独立 Claude 状态、项目指定 cwd、会话模型参数覆盖 |
| `app/adapters/codex/config.py` | CLI 和 `CODEX_HOME` 解析、执行策略、当前 SQLite 文件名 |
| `app/adapters/codex/local_store.py` | 合并本机索引、rollout 和 SQLite 数据；读取消息与模型元数据 |
| `app/adapters/codex/rpc.py` | app-server 初始化、请求超时、流式通知、thread/turn 操作、子进程退出 |
| `app/session_preferences.py`、`app/created_sessions.py` | V2 的会话设置和空会话元数据；不把这些设置写回 provider 的历史文件 |
| `app/attachments.py` | 上传大小、附件 ID、路径范围、图片识别和历史图片读取 |
| `app/web/session-utils.mjs`、`session-tree.mjs` | 以 cwd / projectKey 区分项目、筛选排序、项目与会话两层树 |
| `vendor/claude-code-bridge/agent_adaptors/` | 随仓库提交的共享 adaptor 源码；Mac 修改应连同此处回归测试提交 |
| `tests/` | Python 临时目录/模拟依赖测试、Node UI 逻辑测试，以及需要手动运行的联调脚本 |
| `deploy/edge-auth/` | 现有 Linux Nginx + systemd 公网访问组件；不是 Mac 启动组件 |

默认 native 模式不要求额外启动 `codex-mcp-server` 或监听 8765 的 MCP 服务。`codex-mcp` 是显式配置的兼容选项，首轮 Mac 开发不依赖它。

## 配置、路径与状态边界

### 本机配置

在仓库根执行：

```sh
cp config/adaptors.macos.example.json config/adaptors.local.json
```

通过 `--adaptor-config config/adaptors.local.json` 显式选择它。`cwd`、`stateDir`、`v2StateDir` 和 `projectsDir` 中的相对路径以**配置文件所在目录**为基准，例如配置中的 `cwd: ".."` 指向仓库根。修改配置位置会改变相对路径含义。

不要假设 JSON 配置会展开 shell 的 `~` 或 `$HOME`：当前加载器以 `Path(config目录) / 配置值` 解析。自定义目录使用实际绝对路径或相对于配置目录的路径。CLI 的 `binary` 也应写实际可执行路径，避免把未展开的 shell 变量放进 JSON。

网关的 `--cwd` 与 adaptor 条目中的 `cwd` 作用不同：前者设置 Harness 编排的项目目录，后者控制对应 adaptor 的默认工作目录。项目内“＋”使用服务器确认的目标会话 cwd。为检查配置而新建的测试会话，应放在单独的测试项目目录。

### Codex CLI 和用户目录

`app/adapters/codex/config.py` 当前解析顺序：

1. `CODEX_HOME` 指定 Codex 用户目录；未设置时使用 Python 当前用户的 `~/.codex`。该环境变量会展开 `~`。
2. `CODEX_BIN` 指定 CLI；否则查找该 Codex 配置中的 CLI 路径，最后通过 `PATH` 查找 `codex`。
3. 当前本地读取器使用该目录下 `sessions/`、`session_index.jsonl`、`state_5.sqlite`、`thread_history_1.sqlite` 和 `thread-writer-locks/`。

在启动 V2 的同一终端确认：

```sh
command -v codex
codex --version
uv --version
node --version
```

需要固定 CLI 时，在已经能找到 `codex` 的终端中设置：

```sh
export CODEX_BIN="$(command -v codex)"
test -n "$CODEX_BIN" && test -x "$CODEX_BIN"
```

如果第二条失败，先完成 CLI 安装和路径配置。不要把失败后的空值作为配置。未来通过桌面入口或 launchd 启动时，必须单独验证启动环境中的 `PATH`、`HOME`、`CODEX_BIN` 和 `CODEX_HOME`；不能用交互终端能找到命令来替代该项验收。不要硬编码某一种 CPU 架构对应的软件安装目录。

Windows 开发机的 `CODEX_HOME`、`.venv`、provider 用户目录、SQLite 库、token、附件和 V2 `data/` 不随源码迁移。Mac 使用自己的账号登录与本机会话。若将来需要迁移历史，先定义独立的数据迁移方案：历史 cwd 与图片路径含 Windows 绝对路径，复制文件不会自动把它们转换成 Mac 项目。

### V2 状态与写入策略

统一模式把 V2 元数据写入 `data/adaptors/<配置路径哈希>/<agent>/`；哈希来自配置文件的绝对路径。移动仓库或改用另一份配置后出现新的 V2 状态目录是当前设计的结果。Claude 使用独立 `v2StateDir`，不得与其他 bridge 实例共用。

Codex 默认执行策略由 `CODEX_MCP_SANDBOX` 和 `CODEX_MCP_APPROVAL_POLICY` 控制，源码默认值分别为 `workspace-write` 和 `never`。这些名称保留历史 MCP 前缀，但 native adaptor 也会读取。Mac 接手者应记录实际生效值，不应通过关闭沙箱解决路径或权限问题。

默认真实启动只探测和浏览已有会话，不自动创建受管助手。`--initialize-assistant` 会进入旧的助手初始化流程；首轮浏览验收不使用该参数。用户点击新建或发送消息仍会写真实会话。

## 源码确认的移植风险

| 主题 | 当前实现与风险 | macOS 要验证或开发的内容 |
| --- | --- | --- |
| 启动脚本 | 现有入口是 `.ps1`；后端本身可以通过 Python 模块启动 | 首轮直接使用下文命令，不要求安装 PowerShell；统一 shell 启动器作为后续便利功能 |
| 依赖完整性 | unified 使用仓库内 vendor 包；不能只复制 `app/` 和 README | 干净克隆后 `uv sync --locked --extra unified` 成功，无旧 Windows 邻接目录依赖 |
| Codex 协议 | `rpc.py` 依赖初始化参数、`thread/*`、`turn/*` 和通知字段 | 记录 Mac CLI 版本，用测试会话验收初始化、创建、resume、发送、停止；不同版本的行为不得凭文件存在推断 |
| 本地数据库 | SQLite 文件名、表名和列名固定；读取失败会退回可用数据源 | 实机比对项目列表、标题、归档状态、模型参数及消息；文件名/表结构不匹配时可能仅显示部分历史，应明确诊断 |
| 项目分组 | UI 优先使用原始 cwd / projectKey；目录名相同不应合并 | POSIX 路径、空格、中文、同名不同目录、软链接，以及文件系统允许时的大小写不同目录 |
| Claude 历史 | 默认读取当前用户 `~/.claude/projects`；项目目录编码和 cwd 校验在 `history.py` | 实机对照 Claude 本机 JSONL；自定义目录显式设置 `projectsDir`；验证中文/标点路径编码 |
| Claude 默认模型 | Windows 的运行配置可能带供应商特定模型；SDK 可加载不等于认证成功 | Mac 示例不固定第三方模型；启用后先确认本机认证和实际可用模型，再做一轮显式发送 |
| 进程生命周期 | native 用 asyncio 子进程；关闭时 terminate，等待 5 秒后 kill；Claude CLI 有 POSIX 分支，SDK 管理自己的子进程 | Ctrl+C、SIGTERM、发送中退出、超时、重启后锁恢复、子进程及监听端口释放 |
| 锁 | Claude bridge 在 POSIX 使用 `fcntl.flock`；Codex 历史状态也会参考 `.lock` 文件存在 | 对 bridge 独占做双实例测试；不要删除 provider 锁；Codex `.lock` 存在不等于进程仍存活，需核实状态展示 |
| 附件 | 路径校验和文件引用使用运行主机路径；Codex 图片经 localImage 传递，Claude 当前为文件引用 | Mac 本机上传、历史图片、中文文件名、越界路径拒绝；不可把 Windows 路径当成本机文件 |
| 浏览器交互 | 现有 UI 为 Web，快捷键、中文输入和剪贴板仍需浏览器验收 | Safari / Chrome 中输入法 Enter、Cmd 快捷键、复制、拖入、粘贴截图、Retina 布局与滚动 |
| 网络暴露 | server 默认 host 是 `0.0.0.0`；直连端口无内置网页登录 | 首轮显式绑定 `127.0.0.1`；后续需要 LAN 时单独验收地址、权限和访问边界 |
| 公网部署 | Nginx/systemd 服务和已有隧道属于独立 Linux 部署链 | 不在 Mac 执行该部署安装脚本；Mac 本机验收完成后再设计独立部署参数 |

本地历史读取对 SQLite 使用只读 URI。不要为了兼容数据库版本而改写 provider 数据库、重建索引或删除历史记录。应先用合成 SQLite/JSONL fixture 复现差异，再修改解析器或增加版本探测。

## 分阶段开发与完成标准

### P0：干净环境可以接手，Codex 主流程通过

1. 从 GitHub 干净克隆，用仓库内依赖创建环境；记录 Mac 型号、CPU 架构、macOS / Python / uv / Node / Codex CLI 版本和提交号。
2. 跑通离线 Python 和 Node 测试；启动 fake 页面，验证基本 UI 和假消息闭环。
3. 使用 Mac 配置启动 Codex native。验证默认浏览不创建新会话、不发送消息，列表和记录来自 Mac 本机。
4. 建立独立测试项目，在正确 cwd 新建、刷新、重启、发送、流式接收、续写、停止；设置下一轮模型参数并与实际记录比对。
5. 验证退出后端口和子进程释放；再次启动不会因本项目遗留锁失败。

完成标准：干净克隆可按文档复现；所有离线测试通过；下表中 Codex 的 P0 项均有实机记录；真实调用失败可区分认证、路径、协议和模型错误；Windows 回归通过。尚未通过的 Mac 项不能标注“已支持”。

### P1：Claude、附件和日常使用

1. 在本机配置启用 Claude SDK，独立 state 目录，不填未经验证的第三方模型。
2. 对 owned / external 会话、正确 cwd、新建空会话、首次 `session_id` 与后续 `resume`、发送中停止、provider 错误做实机验收。
3. 测试双 Agent 切换、会话 namespace、会话独立模型设置、附件与中文路径；发送中切换应被拒绝。
4. 在 Safari / Chrome 验证复制、粘贴截图、拖入、输入法、宽屏/窄屏和长历史滚动。
5. 补足数据库版本诊断和 POSIX 路径 fixture；只有实机发现兼容问题时才改核心行为。

完成标准：Claude 已完成真实收发与停止测试，未把 SDK import 成功当作认证成功；两种 Agent 不串会话、不共用 bridge 状态；附件行为和模型设置与 UI 文案一致。

### P2：原生体验和分发

先确定目标用户和分发形式，再设计 `.app` / 原生壳、状态目录迁移、端口分配、日志、启动与退出、更新、签名和公证。原型阶段如评估 launchd，使用用户会话并显式配置可执行路径和环境，验证重新登录、重启、重复启动及退出；不要直接复制 Linux 的 systemd 文件。

完成标准需要另行定义，至少包括可复现构建、安装/卸载、权限提示、进程回收、版本升级和数据保留。Apple Silicon 与 Intel 的支持范围在相应架构验证后再声明。

## 本机启动与离线验证

以下命令均在仓库根执行。先完成交接文档中的工具安装；这里不要求实现新的启动脚本。

```sh
uv sync --locked --extra unified
uv run --locked --extra unified python -m unittest discover -s tests -v
uv run --locked --extra unified python -m unittest discover -s vendor/claude-code-bridge/tests -v
node --test tests/*.test.mjs
```

这些 unittest discovery 命令使用 fixture / mock。`tests/test_fake_smoke.py`、`tests/test_real_smoke.py` 等含 `main()` 的联调脚本不会因此自动执行；不要把它们当作已完成的实机链路覆盖。

单独终端启动 fake：

```sh
uv run --locked --extra unified python -m app.gateway.server --fake --host 127.0.0.1 --port 8055
```

打开 `http://127.0.0.1:8055/`，检查项目树、会话记录、发送与停止、模型设置状态、附件 UI。可在另一终端执行：

```sh
curl --fail http://127.0.0.1:8055/health
```

先按 Ctrl+C 停止 fake，再启动真实 Codex：

```sh
uv run --locked --extra unified python -m app.gateway.server \
  --adaptor-config config/adaptors.local.json \
  --agent-type codex --host 127.0.0.1 --port 8055
```

`tests/test_fake_smoke.py` 和 `tests/test_real_smoke.py` 目前硬编码 8050；浏览器创建测试脚本也使用固定测试端口。不要直接用它们验证 8055 或把它们指向不相关服务。若后续纳入自动端到端测试，先参数化 URL，并确保 fake 测试只连 fake 服务。`test_real_smoke.py` 会实际发送消息，且依赖旧受管助手事件流程，不适合作为首轮只读验证。

8055 在本文是约定开发端口。端口已占用时显式改为另一个可用端口，同步测试 URL；不要停止未知进程。代码保留 7950–8049 的禁用范围，Mac 首轮也遵循该约束。

## 实机验收矩阵

所有行初始状态均为“待 Mac 实机验证”。把结果写入交接记录，并注明提交号、版本、期望/实际及复现命令；截图和日志使用不含账号凭据的测试数据。

| 优先级 | 场景 | 通过条件 |
| --- | --- | --- |
| P0 | 干净克隆与安装 | 无 Windows 邻接项目、已有虚拟环境或运行时 data 仍可安装及测试 |
| P0 | fake 页面与假消息 | 页面、WS、假消息闭环正常，无 provider 调用 |
| P0 | CLI 寻址 | 同一启动环境能找到正确架构 CLI；设置 CODEX_BIN 后实际使用该路径 |
| P0 | 首次默认打开 | 无自动创建/发送；空 Codex 用户目录有清楚状态，不误显示 Windows 项目 |
| P0 | 项目列表 | 同名不同 cwd 不合并；中文/空格路径正确；标题与主会话一致；子代理条目不冒充用户主会话 |
| P0 | 读取记录 | 本机 SQLite / JSONL 各路径均有 fixture；实机正文和模型信息与实际会话一致 |
| P0 | 创建与恢复 | 项目“＋”使用选中项目 cwd；刷新和服务重启后空会话可见；重复请求不重复创建 |
| P0 | 原生会话收发 | 测试项目中一轮发送只产生一条用户输入；流式回复完整；续写不新建另一会话 |
| P0 | 模型与强度 | 设置持久化，下一轮实际参数符合选择；不支持组合有明确错误；不改全局配置 |
| P0 | 停止当前轮次 | 中断指向当前 session / turn；结束状态正确；不会自动重发 |
| P0 | Ctrl+C / SIGTERM | 仅向已确认的本次测试进程发信号；5 秒等待及强制退出路径可验证；子进程、端口和本项目锁可释放 |
| P0 | 发送中退出 / 重启 | 已写记录保留，启动不重放请求，UI 不永久停在运行中；无误杀其他 Desktop / CLI 进程 |
| P0 | provider 同时运行 | Desktop / CLI 占用测试会话时，V2 不偷删锁、不串写；错误可见且无自动重试 |
| P1 | Claude 状态锁 | 两实例共用 bridge state 时第二实例明确失败；正常退出、异常退出后可重新取得 OS 锁 |
| P1 | Claude external | 仅在显式启用 external write 后续写；cwd 缺失或不匹配有明确错误；不宣称 Desktop UI 必然立即刷新 |
| P1 | 双 Agent 切换 | namespace 和设置隔离；运行中拒绝切换；一个不可用时不静默替换 |
| P1 | 附件 | 8 个/单个 20 MB/总 64 MB 的限制有效；Codex 本机图片可读，文件引用正确；越界路径拒绝 |
| P1 | 特殊路径 | 中文、空格、同名目录、软链接；大小写目录测试按文件系统实际能力执行；不自动把历史 Windows cwd 映射到新项目 |
| P1 | 浏览器与输入 | Safari / Chrome 中中文输入法、Enter、Cmd 复制、粘贴图片、拖入和滚动符合预期 |
| P1 | 异常恢复 | CLI 缺失、认证失败、模型无权限、SQLite 不兼容、网络断开均可诊断，无重复发送 |
| P2 | 启动入口与分发 | 桌面或 launchd 环境、重启/重登录、安装升级卸载、签名权限和架构支持单独验收 |

## 提交规范与接手记录

修复优先保持 Windows / macOS 共享实现。只有平台行为确有不同，才引入明确的系统分支；不要把 macOS 绝对路径写入公共默认配置。新增与路径、协议或锁相关的修复应有能复现实际差异的 fixture。

每个 Mac 开发提交说明触发场景、改变的行为、离线回归结果和实机结果。未做实机验证时写明“未验证”。环境和验收记录保存在交接文档的记录模板中；本机 token、provider 配置、会话历史和附件继续保留在 Git 忽略范围。
