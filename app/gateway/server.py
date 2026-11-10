"""Local HTTP/WebSocket gateway for Codex Session Harness v2."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
from pathlib import Path
from urllib.parse import urlsplit, quote
from typing import Any

from aiohttp import web

from app.attachments import AttachmentStore, MAX_FILE_BYTES, MAX_TOTAL_BYTES, MAX_ATTACHMENTS
from app.adapters.codex.adapter import CodexSessionAdapter
from app.adapters.fake import FakeAdapter
from app.orchestrator.session_manager import ManagedAssistant

ROOT = Path(__file__).resolve().parents[2]
WEB_DIR = ROOT / "app" / "web"
DATA_DIR = ROOT / "data"
SKILL_PATH = ROOT / "skills" / "codex-harness-assistant.md"
logger = logging.getLogger("codex-harness")


class HarnessServer:
    def __init__(self, *, fake: bool = False, cwd: str | None = None, adaptor_config: str | None = None, managed_adaptor: str | None = None, agent_type: str | None = None, browse_only: bool = False):
        self.clients: set[web.WebSocketResponse] = set()
        self.browse_only = browse_only
        self.attachments = AttachmentStore(DATA_DIR / ("fake-attachments" if fake else "attachments"))
        self.agent_pool = None
        self.config_key = None
        self.project_dir = Path(cwd or ROOT).resolve()
        if fake and adaptor_config:
            raise ValueError("--fake cannot be combined with --adaptor-config")
        if adaptor_config:
            from app.adapters.unified import build_unified_pool
            self.agent_pool = build_unified_pool(adaptor_config)
            selected_type = agent_type or (self.agent_pool.get_agent_type_for_adaptor(managed_adaptor) if managed_adaptor else None)
            selected_type = selected_type or ("codex" if "codex" in self.agent_pool.adapters else next(iter(self.agent_pool.adapters), None))
            if not selected_type:
                raise ValueError("no enabled adaptor is available")
            adapter = self.agent_pool.get(selected_type)
            import hashlib
            config_key = hashlib.sha256(str(Path(adaptor_config).resolve()).encode()).hexdigest()[:12]
            self.config_key = config_key
            data_dir = DATA_DIR / "adaptors" / config_key / adapter.agent_type
        else:
            adapter = FakeAdapter(cwd=str(Path(cwd or ROOT).resolve())) if fake else CodexSessionAdapter(cwd=cwd or str(ROOT))
            data_dir = DATA_DIR / "fake" if fake else DATA_DIR
        skill_path = ROOT / "skills" / "harness-assistant.md" if adaptor_config else SKILL_PATH
        self.assistant = ManagedAssistant(adapter=adapter, project_dir=self.project_dir, skill_path=skill_path, data_dir=data_dir, emit=self.broadcast)
        self.start_task: asyncio.Task | None = None
        self.command_tasks: set[asyncio.Task] = set()

    def snapshot(self):
        result = self.assistant.snapshot()
        result["sessionCapabilities"] = {"create": bool(getattr(self.assistant.adapter, "supports_project_create", False))}
        result["attachmentCapabilities"] = {"enabled": True, "maxFiles": MAX_ATTACHMENTS,
            "maxFileBytes": MAX_FILE_BYTES, "maxTotalBytes": MAX_TOTAL_BYTES,
            "imageMode": getattr(self.assistant.adapter, "image_mode", "file-reference")}
        if self.agent_pool:
            result["agentOptions"] = self.agent_pool.options()
        else:
            result["agentOptions"] = [
                {"type": "codex", "label": "Codex（旧模式）", "configured": True, "loaded": True,
                 "config": {"enabled": True, "mode": "legacy / fake"}, "runtime": None},
                {"type": "claude", "label": "Claude Code", "configured": False, "loaded": False,
                 "config": {"enabled": False}, "runtime": None},
            ]
        return result

    async def _switch_agent(self, agent_type: str):
        if not self.agent_pool:
            raise ValueError("agent selection requires --adaptor-config")
        agent_type = agent_type.strip().lower()
        adapter = self.agent_pool.get(agent_type)
        if self.assistant._creation_lock.locked() or self.assistant._message_lock.locked() or any(r.status == "queued" for r in self.assistant._records.values()):
            raise ValueError("cannot switch agent while a turn is running")
        if self.start_task and not self.start_task.done():
            self.start_task.cancel()
            await asyncio.gather(self.start_task, return_exceptions=True)
        await self.assistant.close()
        data_dir = DATA_DIR / "adaptors" / self.config_key / adapter.agent_type
        self.assistant = ManagedAssistant(adapter=adapter, project_dir=self.project_dir, skill_path=ROOT / "skills" / "harness-assistant.md", data_dir=data_dir, emit=self.broadcast)
        self.start_task = asyncio.create_task(self.assistant.activate_browser() if self.browse_only else self.assistant.initialize())
        await self.broadcast({"v": 1, "type": "agent.selected", "sequence": 0, "payload": {"agentType": agent_type, "agentLabel": adapter.agent_label, "status": "starting"}})
        await self.broadcast({"v": 1, "type": "agent.options", "sequence": 0, "payload": {"options": await self.agent_pool.status_options(), "selected": agent_type}})
        await self.broadcast({"v": 1, "type": "harness.snapshot", "sequence": self.snapshot()["sequence"], "payload": self.snapshot()})

    async def broadcast(self, event: dict[str, Any]) -> None:
        if not self.clients:
            return
        raw = json.dumps(event, ensure_ascii=False)
        dead: list[web.WebSocketResponse] = []
        for ws in tuple(self.clients):
            try:
                await ws.send_str(raw)
            except (ConnectionError, RuntimeError, web.WebSocketError):
                dead.append(ws)
        for ws in dead:
            self.clients.discard(ws)

    async def startup(self, app: web.Application) -> None:
        self.start_task = asyncio.create_task(self.assistant.activate_browser() if self.browse_only else self.assistant.initialize())

    async def cleanup(self, app: web.Application) -> None:
        for task in self.command_tasks:
            task.cancel()
        await asyncio.gather(*self.command_tasks, return_exceptions=True)
        if self.start_task:
            self.start_task.cancel()
            await asyncio.gather(self.start_task, return_exceptions=True)
        await self.assistant.close()
        if self.agent_pool:
            await self.agent_pool.close()

    async def index(self, request: web.Request) -> web.StreamResponse:
        return web.FileResponse(WEB_DIR / "index.html")

    async def health(self, request: web.Request) -> web.Response:
        snap = self.snapshot()
        code = 200 if snap["status"] in {"ready", "starting", "probing", "creating", "resuming", "initializing"} else 503
        return web.json_response({"ok": code == 200, **snap}, status=code)

    def attachment_scope(self) -> str:
        return f"{self.config_key or 'direct'}:{self.assistant.provider}:{self.assistant.adaptor_id or self.assistant.agent_type}"

    @staticmethod
    def check_origin(request: web.Request) -> None:
        origin = request.headers.get("Origin")
        if origin and origin != f"{request.scheme}://{request.host}":
            raise web.HTTPForbidden(text="Cross-origin access is not allowed")
        if request.headers.get("Sec-Fetch-Site") == "cross-site":
            raise web.HTTPForbidden(text="Cross-site access is not allowed")

    async def upload_attachments(self, request: web.Request) -> web.Response:
        self.check_origin(request)
        if request.headers.get("X-Harness-Upload") != "1":
            raise web.HTTPForbidden(text="Same-origin upload header required")
        created = []
        try:
            thread_id = str(request.query.get("threadId") or "").strip()
            if not thread_id:
                raise ValueError("请先选择目标会话")
            assistant = self.assistant
            scope = self.attachment_scope()
            result = await asyncio.wait_for(assistant.read_session(thread_id, limit=30), timeout=15)
            if result.get("ok") is False:
                raise ValueError("目标会话不可读取")
            total = 0
            async with asyncio.timeout(90):
                reader = await request.multipart()
                async for part in reader:
                    if part.name != "files" or not part.filename:
                        raise ValueError("无效的上传字段")
                    if len(created) >= MAX_ATTACHMENTS:
                        raise ValueError("每次最多上传 8 个附件")
                    data = bytearray()
                    while chunk := await part.read_chunk(64 * 1024):
                        total += len(chunk)
                        data.extend(chunk)
                        if len(data) > MAX_FILE_BYTES or total > MAX_TOTAL_BYTES:
                            raise ValueError("单个文件最多 20 MB，附件总大小最多 64 MB")
                    item = await asyncio.to_thread(self.attachments.save, bytes(data), part.filename,
                                                   scope=scope, thread_id=thread_id)
                    created.append(item)
            if not created:
                raise ValueError("请选择图片或文件")
            if assistant is not self.assistant:
                raise ValueError("Agent 已切换，请在新会话重新上传")
            return web.json_response({"ok": True, "attachments": [a.public() for a in created]}, status=201)
        except (ValueError, TimeoutError) as exc:
            for item in created:
                self.attachments.discard(item.id)
            return web.json_response({"ok": False, "error": str(exc) or "上传超时，请重试"}, status=400)
        except BaseException:
            for item in created:
                self.attachments.discard(item.id)
            raise

    async def attachment_file(self, request: web.Request) -> web.Response:
        self.check_origin(request)
        try:
            item = self.attachments.get(request.match_info["id"])
        except ValueError:
            raise web.HTTPNotFound(text="附件不存在")
        preview = request.match_info["action"] == "preview"
        if preview and item.kind != "image":
            raise web.HTTPNotFound(text="此文件不支持图片预览")
        # Serve a bounded byte snapshot. Re-check image signatures if a local process modified it.
        if item.size > MAX_FILE_BYTES:
            raise web.HTTPRequestEntityTooLarge(max_size=MAX_FILE_BYTES, actual_size=item.size)
        data = await asyncio.to_thread(item.path.read_bytes)
        if preview:
            from app.attachments import image_mime
            mime = image_mime(data)
            if not mime:
                raise web.HTTPNotFound(text="图片已失效")
        else:
            mime = "application/octet-stream"
        response = web.Response(body=data, content_type=mime, headers={
            "X-Content-Type-Options": "nosniff", "Cache-Control": "private, no-store",
            "Content-Security-Policy": "default-src 'none'; sandbox",
            "Cross-Origin-Resource-Policy": "same-origin",
            "Content-Disposition": ("inline" if preview else "attachment") + "; filename*=UTF-8''" + quote(item.name, safe=""),
        })
        return response

    async def websocket(self, request: web.Request) -> web.WebSocketResponse:
        self.check_origin(request)
        ws = web.WebSocketResponse(heartbeat=30)
        await ws.prepare(request)
        self.clients.add(ws)
        try:
            await ws.send_json({"v": 1, "type": "harness.snapshot", "sequence": self.snapshot()["sequence"], "payload": self.snapshot()})
            if self.agent_pool:
                await ws.send_json({"v": 1, "type": "agent.options", "sequence": self.assistant.snapshot()["sequence"], "payload": {"options": await self.agent_pool.status_options(), "selected": self.assistant.agent_type}})
            if self.assistant.state == "ready":
                await ws.send_json({"v": 1, "type": "assistant.session.ready", "sequence": self.assistant.snapshot()["sequence"], "payload": {"publicId": self.assistant.public_id, "skillVersion": self.assistant.skill_version, "status": "ready"}})
            async for message in ws:
                if message.type == web.WSMsgType.TEXT:
                    await self.handle_command(ws, message.json())
                elif message.type == web.WSMsgType.ERROR:
                    break
        finally:
            self.clients.discard(ws)
        return ws

    def start_command(self, coroutine) -> None:
        task = asyncio.create_task(coroutine)
        self.command_tasks.add(task)
        task.add_done_callback(self.command_tasks.discard)

    async def handle_command(self, ws: web.WebSocketResponse, command: Any) -> None:
        if not isinstance(command, dict):
            await ws.send_json({"v": 1, "type": "error", "payload": {"error": "command must be an object"}})
            return
        kind = command.get("type")
        try:
            if kind == "agent.status":
                options = await self.agent_pool.status_options() if self.agent_pool else self.snapshot()["agentOptions"]
                await ws.send_json({"v": 1, "type": "agent.options", "payload": {"options": options, "selected": self.assistant.agent_type}})
            elif kind == "agent.select":
                await self._switch_agent(str(command.get("agentType") or ""))
            elif kind in {"chat.send", "session.send"}:
                client_id = str(command.get("clientMessageId") or "").strip()
                text = str(command.get("text") or "").strip()
                if not client_id or (not text and not command.get("attachmentIds")):
                    raise ValueError("消息需要文字或附件")
                thread_id = str(command.get("threadId") or "").strip() or None
                if kind == "session.send" and not thread_id:
                    raise ValueError("threadId is required")
                if thread_id:
                    # Validate the target through this adaptor without resuming or writing to it.
                    result = await self.assistant.read_session(thread_id, limit=30)
                    if result.get("ok") is False:
                        raise ValueError("无法读取目标会话")
                target = thread_id or self.assistant.thread_id
                if not target:
                    raise ValueError("请先选择会话")
                if not thread_id:
                    result = await self.assistant.read_session(target, limit=30)
                configuration = self.assistant.session_configuration(target, result)
                settings = {k: v for k, v in configuration["effective"].items() if v and configuration["support"].get(k)}
                attachments = self.attachments.resolve(command.get("attachmentIds", []),
                    scope=self.attachment_scope(), thread_id=thread_id or self.assistant.thread_id or "")
                self.start_command(self.assistant.send(client_id, text, thread_id=thread_id, attachments=attachments, settings=settings))
            elif kind == "chat.retry":
                client_id = str(command.get("clientMessageId") or "")
                record = self.assistant._records.get(client_id)
                if not record:
                    raise ValueError("unknown clientMessageId")
                retry_id = client_id + "-retry"
                self.start_command(self.assistant.send(retry_id, record.text, thread_id=record.thread_id, attachments=record.attachments, settings=record.settings))
            elif kind == "chat.cancel":
                await self.assistant.cancel(str(command.get("clientMessageId") or ""))
            elif kind == "models.list":
                catalog = await self.assistant.model_catalog()
                await ws.send_json({"v": 1, "type": "models.snapshot", "requestId": command.get("requestId"), "payload": {**catalog, "agentType": self.assistant.agent_type}})
            elif kind in {"session.config.get", "session.config.set"}:
                thread_id = str(command.get("threadId") or "").strip()
                assistant = self.assistant
                history = await assistant.read_session(thread_id, limit=30)
                if history.get("ok") is False:
                    raise ValueError("无法读取目标会话设置")
                if assistant is not self.assistant:
                    raise ValueError("Agent 已切换，请重新选择会话")
                if kind == "session.config.set":
                    if history.get("writable") is False or history.get("readOnly"):
                        raise ValueError("此会话只读，不能配置下一轮")
                    if any(r.thread_id == thread_id and r.status in {"queued", "delivered", "running"} for r in assistant._records.values()):
                        raise ValueError("会话正在运行，请完成后再修改设置")
                    configuration = assistant.session_configuration(thread_id, history)
                    catalog = await assistant.model_catalog()
                    submitted = command.get("settings")
                    # Validate effort-only overrides against the currently effective model too.
                    if isinstance(submitted, dict) and submitted.get("reasoningEffort") and not submitted.get("model"):
                        model = configuration["observed"].get("model") or configuration["defaults"].get("model")
                        known = next((m for m in catalog.get("models", []) if m.get("model") == model), None)
                        if known and submitted["reasoningEffort"] not in known.get("supportedReasoningEfforts", catalog.get("efforts", [])):
                            raise ValueError("当前模型不支持这个思考强度")
                    assistant.preferences.save(thread_id, submitted, support=configuration["support"], catalog=catalog)
                configuration = assistant.session_configuration(thread_id, history)
                await ws.send_json({"v": 1, "type": "session.config", "requestId": command.get("requestId"), "payload": {"threadId": thread_id, "configuration": configuration, "saved": kind.endswith('.set')}})
            elif kind == "session.create":
                assistant = self.assistant
                if "cwd" in command:
                    raise ValueError("新建会话不能指定任意目录，请使用项目引用")
                row = await assistant.create_project_session(str(command.get("referenceId") or ""),
                    command.get("name") or "新会话", command.get("requestId"), project_key=command.get("projectKey"))
                await ws.send_json({"v": 1, "type": "session.created", "requestId": command.get("requestId"),
                    "payload": {"session": row, "agentType": assistant.agent_type}})
            elif kind == "sessions.list":
                result = await self.assistant.list_sessions(cursor=command.get("cursor"))
                await ws.send_json({"v": 1, "type": "sessions.snapshot", "requestId": command.get("requestId"), "sequence": self.assistant.snapshot()["sequence"], "payload": result})
            elif kind == "session.read":
                thread_id = str(command.get("threadId") or "")
                scope = self.attachment_scope()
                result = await self.assistant.read_session(thread_id, limit=command.get("limit", 30))
                result = await asyncio.to_thread(self.attachments.decorate, result, scope=scope, thread_id=thread_id)
                await ws.send_json({"v": 1, "type": "session.read", "requestId": command.get("requestId"), "sequence": self.assistant.snapshot()["sequence"], "payload": result})
            elif kind == "harness.retry-init":
                if self.assistant.state in {"error", "degraded"}:
                    self.assistant._initialized.clear()
                    self.start_task = asyncio.create_task(self.assistant.activate_browser() if self.browse_only else self.assistant.initialize())
            else:
                raise ValueError(f"unknown command: {kind}")
        except Exception as exc:
            await ws.send_json({"v": 1, "type": "command.error", "requestId": command.get("requestId"), "sequence": self.assistant.snapshot()["sequence"], "payload": {"error": str(exc), "command": kind, "threadId": command.get("threadId"), "clientMessageId": command.get("clientMessageId")}})


def build_app(server: HarnessServer) -> web.Application:
    app = web.Application(client_max_size=MAX_TOTAL_BYTES + 1024 * 1024)
    app["harness"] = server
    app.router.add_get("/", server.index)
    app.router.add_get("/health", server.health)
    app.router.add_get("/ws", server.websocket)
    app.router.add_post("/api/attachments", server.upload_attachments)
    app.router.add_get("/api/attachments/{id}/{action:preview|download}", server.attachment_file)
    app.router.add_static("/static/", WEB_DIR, show_index=False)
    app.on_startup.append(server.startup)
    app.on_cleanup.append(server.cleanup)
    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="Codex Session Harness v2")
    parser.add_argument("--host", default=os.environ.get("HARNESS_HOST", "0.0.0.0"), help="bind address; default exposes the workspace on trusted LAN (use 127.0.0.1 for local only)")
    parser.add_argument("--port", type=int, default=int(os.environ.get("HARNESS_PORT", "8050")))
    parser.add_argument("--cwd", default=os.environ.get("HARNESS_CWD"))
    parser.add_argument("--fake", action="store_true", help="run a deterministic fake Codex adapter for UI smoke tests")
    parser.add_argument("--legacy-codex", action="store_true", help="run Codex-only compatibility mode")
    parser.add_argument("--initialize-assistant", action="store_true", help="create/resume a managed assistant; default only probes and browses existing sessions")
    parser.add_argument("--adaptor-config", help="unified agent adaptor JSON config")
    parser.add_argument("--managed-adaptor", help="managed assistant adaptor id (no automatic fallback)")
    parser.add_argument("--agent-type", choices=["codex", "claude"], help="initial agent type")
    parser.add_argument("--log-level", default=os.environ.get("HARNESS_LOG_LEVEL", "INFO"))
    args = parser.parse_args()
    logging.basicConfig(level=getattr(logging, args.log_level.upper(), logging.INFO), format="%(asctime)s %(levelname)s %(name)s %(message)s")
    if not 1 <= args.port <= 65535 or 7950 <= args.port <= 8049:
        parser.error("invalid or reserved port; use 8050 or above")
    if args.legacy_codex and args.adaptor_config:
        parser.error("--legacy-codex cannot be combined with --adaptor-config")
    config = args.adaptor_config
    if not args.fake and not args.legacy_codex and not config:
        config = str(ROOT / "config" / "adaptors.json")
    server = HarnessServer(fake=args.fake, cwd=args.cwd, adaptor_config=config, managed_adaptor=args.managed_adaptor,
        agent_type=args.agent_type, browse_only=not args.initialize_assistant and not args.fake)
    web.run_app(build_app(server), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
