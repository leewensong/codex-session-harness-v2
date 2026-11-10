# Reference copies

这里是为新版本迁移而复制的旧代码，仅作参考，不是运行时入口，也不应被新版本直接 import。

- `legacy-session-ui/index.html`：旧 H5 页面，包含会话列表及不再需要的 Ollama、语音和 3D 功能。
- `legacy-session-ui/server.py`：旧 WebSocket 服务，包含会话列表/读取/发送相关处理以及旧版媒体路径。
- `old-harness/`：旧 Ollama Harness 的 Codex client 和 store，仅供迁移数据结构和调用约定。
- `codex-mcp-server/`：Codex bridge 的源代码与能力文档副本。

复制时没有包含旧项目的运行时数据、日志、证书、模型或虚拟环境。后续实现必须在 `app/` 中重新组织，并移除 Ollama、ASR/TTS、WebRTC 和 Three.js 依赖。
