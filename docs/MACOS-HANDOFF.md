# Mac 环境安装与开发交接

更新日期：2026-11-11。仓库：`leewensong/codex-session-harness-v2`，默认分支 `main`。

## 目标与当前状态

在另一台 Mac 上继续开发 V2，保留项目/会话列表、历史阅读、项目内新建会话、流式发送、图片/文件附件及模型设置。首个交付形态是本地 Python 服务 + 浏览器界面。原生窗口、菜单栏、自动启动、签名和 DMG 安装包在基础链路通过后另行实现。

本次交接已提供完整源码、仓库内 adaptor 依赖、依赖锁、配置示例和开发验收文档。**尚未在 Mac 上启动或验收，不应将 Windows 测试结果标记为 Mac 已通过。**

## 1. 准备环境

仓库已公开，可直接克隆；推送改动仍需有写入权限的 GitHub 账号。准备 Git、uv、Python 3.12、Node 22，以及能在当前终端运行的 Codex CLI；Claude Code / SDK 放到第二阶段。Python 3.12、Node 22 是此次交接的验证基线选择，不是最低支持版本承诺。

安装工具以官方说明为准：

- [uv 安装](https://docs.astral.sh/uv/getting-started/installation/)
- [Codex CLI](https://developers.openai.com/codex/cli/)
- [Codex app-server 协议](https://developers.openai.com/codex/app-server/)

Codex 官方 CLI 页面提供 npm 安装方式：`npm install -g @openai/codex`。新机器独立完成账号登录，再记录 `codex --version`；不要从 Windows 拷贝认证文件。CLI 与本机 Desktop 的历史/数据库兼容性必须用该 Mac 的实际版本确认。

```bash
sw_vers
uname -m
git --version
uv --version
node --version
command -v codex
codex --version
```

把版本填到 [实机验收记录](MACOS-VALIDATION.md)。目前未确定最低 macOS 版本；优先验证实际到位的设备架构，Intel 只有在有对应设备并通过验收后才列为支持。

## 2. 克隆并安装

```bash
git clone https://github.com/leewensong/codex-session-harness-v2.git
cd codex-session-harness-v2
git switch -c macos-port
uv python install 3.12
uv sync --locked --python 3.12 --extra unified
uv run --locked --extra unified python --version
uv run --locked --extra unified python -c 'import aiohttp, agent_adaptors; print("imports OK")'
```

依赖已内置到 `vendor/claude-code-bridge/`，不要额外建立原 Windows 的兄弟目录，也不要复制 Windows `.venv`。若锁文件解析或特定平台 wheel 不可用，记录完整错误、Python 与架构后处理；不要先升级所有依赖。SDK 当前锁定版本由 vendor 的 `pyproject.toml` 和根 `uv.lock` 决定。

## 3. 先跑自动回归和 Fake

在项目根运行：

```bash
uv run --locked --extra unified python -m unittest discover -s tests -v
uv run --locked --extra unified python -m unittest discover -s vendor/claude-code-bridge/tests -v
node --test tests/*.test.mjs
lsof -nP -iTCP:8055 -sTCP:LISTEN
```

`lsof` 无结果通常表示没有该监听；若已有进程，先确认归属，改用另一空闲端口并同步后续 URL。不要终止其他项目服务。

```bash
uv run --locked python -m app.gateway.server --fake --host 127.0.0.1 --port 8055
```

在第二个终端执行 `curl -fsS http://127.0.0.1:8055/health`，在浏览器打开 `http://127.0.0.1:8055/`。检查项目与会话、列表展开/搜索、历史显示、Fake 消息收发、刷新重连。按 Ctrl+C 关闭 Fake，确认端口释放，再启动真实 adaptor。

首次安装需要网络；默认 unittest 与前端测试不依赖真实 provider 发送。`tests/test_real_smoke.py`、`tests/test_fake_smoke.py` 是手工执行脚本，保留早期 8050/受管助手假设，不要直接拿来验证本节 8055 的浏览模式。

## 4. 配置并启动本机 Codex

```bash
cp config/adaptors.macos.example.json config/adaptors.local.json
```

此本地文件已被 Git 忽略。初始配置只启用 `codex-native`；`cwd: ".."` 相对于 `config/`，因此指向仓库根目录。若需要其他默认项目目录，填写该 Mac 上实际存在的绝对路径。配置中的 `~` 和 `$变量` 不应假定会被自动展开，建议填绝对路径。

```bash
export CODEX_BIN="$(command -v codex)"
test -n "$CODEX_BIN" && test -x "$CODEX_BIN"
uv run --locked --extra unified python -m app.gateway.server \
  --adaptor-config config/adaptors.local.json \
  --agent-type codex --host 127.0.0.1 --port 8055
```

先确认 `command -v codex` 成功再继续。若环境已有自定义 `CODEX_HOME`，核实它是否正是该 Mac 的目标数据目录；否则保持未设置，由代码默认使用 `~/.codex`。不要将 Windows 的 home、认证、SQLite、rollout 或锁文件覆盖到 Mac。

初次启动不加 `--initialize-assistant`，先验证只读的列表、项目分组与会话历史。**新 Mac 没有本机 Codex 历史时，列表为空是正常的；配置 `cwd` 不会自动产生列表项目。** 先在该 Mac 上建立专用测试目录，用官方 Codex CLI 在此目录人工创建一条测试会话/历史，再刷新 V2；看到该项目后才能使用其“＋”验证项目内新建。不要为了填充列表全局启用旧受管助手初始化。

随后在专用测试项目创建新会话，人工发送最小测试消息，验证流式增量、结束、中断、重启与模型参数；附件进入 P1 验收。真实发送可能产生账户用量，验收记录中写清使用的账号类别与模型，不记录密钥或会话正文。

## 5. 再接入 Claude

Codex 路径通过后，在同一 `adaptors.local.json` 中启用 Claude；为该 Mac 配置 CLI/SDK 登录和所需模型。示例初始 `allowExternalSessions: false`、`permissionMode: dontAsk`，先验证 V2 新建的专用测试会话。需要外部历史续写时明确启用并使用可丢弃测试会话验收。

示例不带 Windows 使用的第三方模型名。保留 `backend: sdk` 和独立 `v2StateDir`，不要复用其他正在运行的 bridge 的状态目录。SDK 依赖可导入只说明依赖加载成功，登录、额度、模型权限、工具权限与一次真实回复必须分别验证。

## 6. 下一位开发者任务说明

可将以下文字交给 Mac 上继续工作的开发者或 Codex：

> 继续开发此仓库的 macOS 版本。先读 README、docs/MACOS-HANDOFF.md、docs/MACOS-DEVELOPMENT.md 和 docs/MACOS-VALIDATION.md，以当前源码为准。使用仓库内 vendor 依赖，在 macos-port 分支工作。先记录 Mac/架构/Python/Node/Codex 版本，完成锁定依赖安装、现有默认回归和 Fake 启动；再使用 config/adaptors.local.json 在 127.0.0.1:8055 验证本机 Codex。先验证列表与历史，随后在专用测试项目完成创建、发送、流式、中断、重启、附件和模型设置。按开发文档的 P0 顺序处理问题；保留 Codex/Claude 会话 namespace 与各自状态。通过后再做 Claude 和便捷启动/launchd；原生 .app/DMG 不属于第一阶段完成条件。每项实测填入验收记录并提交源码/文档；不提交 data、登录凭据、真实历史与本地配置，不改现有 Windows 公网部署。

## 7. 同步回 GitHub

```bash
git status --short
git add app config docs tests pyproject.toml uv.lock README.md
git diff --cached --stat
git commit -m "Add macOS support and record device validation"
git push -u origin macos-port
```

按实际改动补充 vendor 或启动脚本；提交前查看 staged 内容。通过代码评审后再合入 main。`data/`、`config/*.local.json`、`.env`、`.venv/` 均不应进入 staged 清单。

Windows 的 PowerShell 启动脚本、Linux edge-auth/systemd、现有域名/FRP 配置属于既有部署背景，Mac 本地开发不需要复制或修改这些服务。后台启动前的 PATH、停止与恢复细节见开发文档。
