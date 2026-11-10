# app

V2 当前运行时代码。入口为 `python -m app.gateway.server`；Mac 开发请先阅读 [`../docs/MACOS-HANDOFF.md`](../docs/MACOS-HANDOFF.md)。`reference/` 是历史实现，不参与默认启动。

主要入口：

- `gateway/server.py`：HTTP、WebSocket、静态页面；
- `orchestrator/session_manager.py`：受管助手会话状态机与消息队列；
- `adapters/codex/adapter.py`：Codex app-server/MCP bridge 封装；
- `web/index.html`：轻量文本界面。
