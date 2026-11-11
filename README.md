# Codex Workspace V2

V1 的桌面宽屏简化版：在浏览器中按项目浏览 Codex / Claude Code 会话、阅读记录、创建会话、发送消息、上传附件，并设置会话模型与思考强度。后端使用 Python + aiohttp，前端为原生 HTML / CSS / JavaScript，无前端打包步骤。

仓库：`leewensong/codex-session-harness-v2`。2026-11-11 建立独立源码基线。

## Mac 开发入口

**当前已运行环境是 Windows；macOS 尚待另一台 Mac 实机开发与验收。** 第一阶段目标是复用现有 Python + Web 架构，在 Mac 上运行本地服务与浏览器界面；原生 `.app` / DMG 属于后续单独里程碑。

- [Mac 环境安装与开发交接](docs/MACOS-HANDOFF.md)：新电脑从克隆到启动的完整顺序，以及可直接交给下一位开发者的任务说明。
- [Mac 开发设计与验收](docs/MACOS-DEVELOPMENT.md)：架构、已识别的平台差异、任务优先级、测试矩阵与完成标准。
- [Mac 实机验收记录模板](docs/MACOS-VALIDATION.md)：在 Mac 上填写实际版本、结果与剩余问题。
- [macOS adaptor 示例](config/adaptors.macos.example.json)：初始启用 Codex，Claude SDK 显式启用后再验证。

## 源码范围与安装

```text
app/                            HTTP / WebSocket / adaptor / Web UI
config/                         默认配置与 Mac 示例；*.local.json 不入库
vendor/claude-code-bridge/       unified 模式使用的共享 adaptor 源码快照
skills/                         Harness 助手指令
tests/                         Python / Node 回归测试（目录名为 tests）
docs/                           开发、交接及部署文档
deploy/edge-auth/                既有 Linux 反向代理认证部署模板
reference/                      历史实现参考，不是启动入口
```

依赖已经放入仓库，单独克隆无需原来的 `../claude-code-bridge` 兄弟目录。Python 要求以 `pyproject.toml` 的 `>=3.11` 为准，版本锁定在 `uv.lock`；新 Mac 的首轮验证统一使用 Python 3.12。Node 仅用于运行前端测试；当前 Windows 使用 Node 22。

```bash
git clone https://github.com/leewensong/codex-session-harness-v2.git
cd codex-session-harness-v2
uv sync --locked --extra unified
```

需要相应 agent 的本机 CLI、登录状态和本机项目历史；这些属于每台机器的本地环境。Git 不包含 provider 登录凭据、聊天历史、附件、虚拟环境与运行状态。

## Windows 启动

```powershell
.\start-public.ps1
# 当前 Windows 部署使用 8055；明确设置仅本机监听的示例：
.\start.ps1 -Port 8055 -BindAddress 127.0.0.1
```

原有 `start.ps1` 默认监听 `0.0.0.0:8050`，本机 8050 已被其他应用使用，因此当前环境使用 8055。局域网直连可读写 agent 会话，使用范围应为受信任网络；外部访问沿用单独的 HTTPS 认证入口。既有部署细节见 [Windows 功能与部署基线](docs/WINDOWS-BASELINE.md) 和 [边缘网页登录](docs/EDGE-WEB-ACCESS.md)。

默认启动只探测和浏览，不初始化受管助手。主动发送才触发推理；SDK 加载成功与账号/模型推理成功是不同的验收项。

## 离线逻辑回归与 Fake 启动

首次安装依赖需要网络。安装完成后，以下默认回归使用临时数据、mock 和 fake，不发送真实 agent 消息：

```bash
uv run --locked --extra unified python -m unittest discover -s tests -v
uv run --locked --extra unified python -m unittest discover -s vendor/claude-code-bridge/tests -v
node --test tests/*.test.mjs

# Fake 服务，先确认端口空闲；关闭时按 Ctrl+C
uv run --locked python -m app.gateway.server --fake --host 127.0.0.1 --port 8055
```

打开 `http://127.0.0.1:8055/`，健康检查为 `/health`。`tests/test_real_smoke.py` 与部分内置 adaptor 手工脚本会调用真实 agent，且含旧端口或旧受管助手假设，不属于上述默认 unittest 自动发现；Mac 联调前先读开发文档。

## 开发资料

- [仓库建立与验证基线](docs/REPOSITORY-BASELINE.md)
- [Claude adaptor 接入历史](docs/CLAUDE-ADAPTOR-INTEGRATION.md)
- [内置 adaptor 来源与更新说明](vendor/README.md)
- [应用目录说明](app/README.md)
- [早期调试记录](docs/DEBUGGING.md)（历史记录，以当前源码与实测为准）

当前仓库已公开，未声明开源许可证。Mac 的实际最低系统版本、Intel 支持及安装包形式在实机验收后确定。
