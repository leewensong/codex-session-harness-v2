# 仓库建立与验证基线

建立日期：2026-11-11。仓库：`leewensong/codex-session-harness-v2`，分支 `main`。建立时为私有，2026-11-12 按用户要求改为公开。

## 本次收纳

- V2 当前应用、前端、配置、测试、部署模板、助手指令及历史参考源码。
- `vendor/claude-code-bridge` 内置共享 adaptor，根 `pyproject.toml` / `uv.lock` 改用仓库内路径；现有共享工程保持原目录。
- Mac 环境交接、架构与开发计划、实机验收模板、专用配置示例。
- 原 README 保存在 `WINDOWS-BASELINE.md`；其他旧文档继续作为历史背景。

`data/`、认证与运行状态、会话历史、附件、本地配置、虚拟环境、日志、编辑器备份和 `.kiro` 未纳入 Git。首次提交清单已做目录排除检查与凭据模式扫描。

## 本次实际验证：Windows

从 Git 暂存源码导出一个隔离目录，在其中新建 Python 3.12.13 环境安装，不复用原工程 `.venv` 或 `../claude-code-bridge`。uv 0.11.6，Node 22.18.0。

| 检查 | 结果 |
|---|---|
| `uv sync --locked --python 3.12 --extra unified` | 成功，41 个锁定包；adaptor 从仓库内 vendor 构建 |
| 根 `tests/` unittest discovery | 74 项，72 通过，2 跳过；无失败 |
| vendor adaptor unittest discovery | 31 项通过 |
| `node --test tests/*.test.mjs` | 21 项通过 |
| Fake 独立 HTTP/WS smoke | 健康检查、首页、静态 JS、WS 快照、会话列表、发送、重复请求去重通过 |
| Fake 监听清理 | 测试结束后端口可重新绑定 |
| Git 忽略范围 | 运行数据、附件、虚拟环境、本地配置、环境文件和备份均被忽略 |

Fake smoke 只调用 fake adaptor。默认回归中的 Linux libcrypt 兼容检查在此 Windows 环境跳过；其他平台仍需运行对应检查。没有以这次回归结果代替真实 Codex / Claude 的 Mac 链路验收。

## Mac 状态

尚无 Mac 实机结果。下一台机器从 [MACOS-HANDOFF.md](MACOS-HANDOFF.md) 开始，在 [MACOS-VALIDATION.md](MACOS-VALIDATION.md) 记录实际环境及结果。第一阶段先运行本地服务和浏览器 UI；原生应用打包、签名与分发另行验收。
