# Debugging status

更新时间：2026-08-21

## 已验证环境

- Harness URL：`http://127.0.0.1:8050/`
- Codex：`codex-cli 0.148.0-alpha.15`
- 默认执行策略：`workspace-write / never`
- 受管助手公开短码：`ses_A6E02B54`
- 受管 thread 安全短码：`972d0406`

## 已通过

- Python 代码编译检查；
- fake adapter：启动、Skill 初始化、会话列表、消息增量、完成事件、重复消息去重；
- 浏览器：会话列表、受管助手标记、文本发送与安全文本渲染，控制台无错误；
- 真实 Codex：runtime probe、创建会话、Skill 首回合、真实文本回复；
- 服务重启后恢复同一个受管 Codex 会话；
- Skill 初始化回合不会显示在普通用户聊天历史中；
- fake 与真实模式使用隔离的状态目录。

## 待调试

- Codex writer lock、回合超时和协议不匹配的错误 UI；
- 消息失败后的完整重试按钮和幂等语义；
- 正在运行消息的取消按钮；
- 会话列表项目过滤；
- WebSocket sequence 补发/快照恢复的完整实现；
- Skill 或 Codex 版本变化时自动新建会话的测试。

## 命令

```powershell
# fake 模式
.\start-fake.ps1

# 真实 Codex 模式
.\start.ps1

# fake 端到端测试（先启动 fake 模式）
uv run --project . python -m tests.test_fake_smoke

# 真实端到端测试（先启动真实模式；会向受管会话发送一条测试消息）
uv run --project . python -m tests.test_real_smoke

# 运行时探测
uv run --project . python -m tests.runtime_probe
```
