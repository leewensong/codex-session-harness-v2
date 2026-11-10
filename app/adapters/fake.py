"""Deterministic adapter for local UI and protocol smoke tests."""

from __future__ import annotations

import asyncio
import time
import uuid
from pathlib import Path
from collections.abc import Awaitable, Callable

DeltaCallback = Callable[[str], Awaitable[None]]


class FakeAdapter:
    agent_type = "codex"
    provider = "fake"
    image_mode = "native"
    supports_project_create = True
    settings_support = {"model": True, "reasoningEffort": True}
    default_settings = {"model": "fake-codex", "reasoningEffort": "medium"}

    def __init__(self, *, cwd: str):
        self.cwd = cwd
        self.thread_id = "fake-" + uuid.uuid4().hex[:12]
        self.messages: list[dict[str, str]] = []
        self.applied_settings = {}
        self.interrupted = False
        self.updated_at = int(time.time())
        self.examples = [
            {"id": "demo-workspace", "name": "搭建桌面会话工作台", "project": "codex-session-harness-v2", "cwd": cwd, "status": "idle", "updatedAt": self.updated_at - 300,
             "messages": [
                 {"role": "user", "text": "把 V2 做成 V1 首页的简化版，重点是项目、会话列表和会话记录。电脑上要充分利用横向空间。"},
                 {"role": "assistant", "text": "## 桌面优先的会话工作台\n\n用三栏结构组织核心信息，让浏览项目和阅读记录可以同时进行。\n\n- **项目导航**：按工作目录分组，显示会话数量。\n- **会话列表**：搜索、状态筛选和最近更新时间。\n- **会话记录**：宽屏阅读、代码展示和追加消息。\n\n```css\n.workspace {\n  display: grid;\n  grid-template-columns: 220px 340px minmax(0, 1fr);\n}\n```\n\n不引入语音、3D 模型和媒体区，保持界面专注。"},
                 {"role": "user", "text": "很好，保留窄屏可用，但以桌面体验为主。"},
                 {"role": "assistant", "text": "已确认。各栏独立滚动；窄屏折叠项目导航，小屏在列表与记录间切换。发送前始终显示目标会话，避免发到错误的会话。"}]},
            {"id": "demo-filters", "name": "项目筛选与搜索交互", "project": "codex-session-harness-v2", "cwd": cwd, "status": "idle", "updatedAt": self.updated_at - 3600,
             "messages": [{"role": "user", "text": "项目名相同但目录不同时要怎么区分？"}, {"role": "assistant", "text": "按完整工作目录生成稳定分组标识，不仅按目录名合并。界面保留简短名称，同名项目使用序号区分。"}]},
            {"id": "demo-long-history", "name": "长记录阅读与代码展示", "project": "codex-session-harness-v2", "cwd": cwd, "status": "idle", "updatedAt": self.updated_at - 7200,
             "messages": [{"role": "user" if i % 2 == 0 else "assistant", "text": f"记录 {i + 1}：验证最近记录数量、搜索和独立滚动。"} for i in range(120)]},
            {"id": "demo-voice", "name": "首页会话列表体验优化", "project": "voice-duplex-h5", "cwd": str(Path(cwd).parent / "voice-duplex-h5"), "status": "idle", "updatedAt": self.updated_at - 86400,
             "messages": [{"role": "user", "text": "V1 的首页能按项目筛选会话吗？"}, {"role": "assistant", "text": "可以。首页包含项目筛选、会话卡片、最近记录和追加消息。V2 保留这些核心能力，移除语音和模型展示。"}]},
            {"id": "demo-bridge", "name": "检查会话连接状态", "project": "codex-mcp-server", "cwd": str(Path(cwd).parent / "codex-mcp-server"), "status": "error", "updatedAt": self.updated_at - 172800,
             "messages": [{"role": "system", "text": "这是演示数据中的异常会话，用于验证状态筛选。"}]},
            {"id": "demo-duplicate", "name": "另一个工作目录的会话", "project": "codex-session-harness-v2", "cwd": str(Path(cwd).parent / "archive" / "codex-session-harness-v2"), "status": "idle", "updatedAt": self.updated_at - 259200, "messages": []},
        ]

    async def runtime_status(self) -> dict:
        return {"ok": True, "version": "fake-codex-0.1", "bin": "fake", "executionPolicy": {"sandbox": "fake", "approvalPolicy": "never"}}

    async def create_session(self, *, name: str) -> dict:
        return {"threadId": self.thread_id, "name": name, "cwd": self.cwd, "status": "idle"}

    async def create_project_session(self, *, name: str, cwd: str) -> dict:
        sid = "fake-created-" + uuid.uuid4().hex[:12]
        row = {"id": sid, "name": name, "cwd": cwd, "project": Path(cwd).name,
               "status": "idle", "updatedAt": int(time.time()), "preview": "", "messages": []}
        self.examples.append(row)
        return {**{k: v for k, v in row.items() if k != "messages"}, "threadId": sid}

    async def resume_session(self, thread_id: str) -> dict:
        if thread_id != self.thread_id:
            raise RuntimeError("fake session not found")
        return {"threadId": thread_id, "name": "Codex Harness Assistant", "cwd": self.cwd, "status": "idle"}

    async def send_turn(self, thread_id: str, text: str, *, on_delta: DeltaCallback | None = None, timeout: float = 180.0, attachments=None, settings=None) -> dict:
        messages = self._messages(thread_id)
        if settings:
            self.applied_settings[thread_id] = dict(settings)
        messages.append({"role": "user", "text": text, "attachments": [a.public() for a in attachments or []]})
        reply = "Fake Codex 已收到：" + text[:400] + (f"（{len(attachments)} 个附件）" if attachments else "")
        for chunk in [reply[:12], reply[12:]]:
            if self.interrupted:
                self.interrupted = False
                return {"status": "cancelled", "threadId": thread_id, "reply": ""}
            await asyncio.sleep(0.01)
            if on_delta:
                await on_delta(chunk)
        messages.append({"role": "assistant", "text": reply})
        return {"status": "completed", "threadId": thread_id, "turnId": "fake-turn-" + uuid.uuid4().hex[:8], "reply": reply, "elapsedSeconds": 0.02}

    async def interrupt(self, thread_id: str) -> None:
        self.interrupted = True

    def _messages(self, thread_id: str) -> list:
        if thread_id == self.thread_id:
            return self.messages
        for row in self.examples:
            if row["id"] == thread_id:
                return row["messages"]
        raise ValueError("fake session not found")

    async def list_sessions(self, *, limit: int = 50, cursor: str | None = None) -> dict:
        rows = [{"id": self.thread_id, "name": "Codex Harness Assistant", "cwd": self.cwd,
                 "project": Path(self.cwd).name, "status": "idle", "updatedAt": self.updated_at,
                 "preview": "用于验证受管助手的演示会话"}]
        rows += [{**{k: v for k, v in row.items() if k != "messages"},
                  "preview": row["messages"][-1]["text"][:120] if row["messages"] else ""} for row in self.examples]
        offset = int(cursor or 0)
        return {"ok": True, "sessions": rows[offset:offset + limit],
                "nextCursor": str(offset + limit) if offset + limit < len(rows) else None}

    async def read_session(self, thread_id: str, *, limit: int = 30) -> dict:
        messages = self._messages(thread_id)
        row = next((row for row in self.examples if row["id"] == thread_id), {})
        return {"ok": True, "threadId": thread_id, "name": row.get("name", "Codex Harness Assistant"),
                "cwd": row.get("cwd", self.cwd), "status": row.get("status", "idle"), "messages": messages[-limit:], "observedSettings": {**self.applied_settings.get(thread_id, self.default_settings), "source": "fake-turn"}}

    async def model_catalog(self):
        return {"models": [{"model": "fake-codex", "displayName": "Fake Codex", "supportedReasoningEfforts": ["low", "medium", "high"]}],
                "efforts": ["low", "medium", "high"], "source": "fake", "verified": True, "supportsCustomModel": True}

    async def close(self) -> None:
        return None
