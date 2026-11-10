"""Codex app-server adapter used by the lightweight Harness."""

from __future__ import annotations

import asyncio
import os
import subprocess
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from app.attachments import attachment_prompt, message_images
from .config import CodexPaths, resolve_execution_policy, resolve_paths, _read_config
from app.session_preferences import CODEX_EFFORTS
from .local_store import list_local_sessions, read_local_session
from .rpc import CodexRpcClient, CodexRpcError

DeltaCallback = Callable[[str], Awaitable[None]]


def _text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if not isinstance(value, list):
        return str(value or "")
    return "".join(str(item.get("text", "")) for item in value if isinstance(item, dict) and isinstance(item.get("text"), str))


def _status(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("type") or "unknown")
    return str(value or "unknown")


class CodexAdapterError(RuntimeError):
    pass


class CodexSessionAdapter:
    agent_type = "codex"
    provider = "codex"
    image_mode = "native"
    settings_support = {"model": True, "reasoningEffort": True}
    supports_project_create = True

    def __init__(self, *, cwd: str | None = None, timeout: float = 30.0):
        self.paths: CodexPaths = resolve_paths()
        self.policy = resolve_execution_policy()
        self.client = CodexRpcClient(self.paths, timeout=timeout, policy=self.policy)
        self.cwd = str(Path(cwd or os.getcwd()).resolve())
        self.version = "unknown"
        self._catalog = None
        self._resumed: set[str] = set()
        self._turn_locks: dict[str, asyncio.Lock] = {}

    @property
    def default_settings(self):
        config = _read_config(self.paths.home)
        return {"model": config.get("model"), "reasoningEffort": config.get("model_reasoning_effort")}

    async def model_catalog(self):
        if self._catalog is not None:
            return self._catalog
        try:
            await self.client.start()
            response = await self.client.request("model/list", {"limit": 100, "includeHidden": False}, timeout=8)
            models = [{"model": m["model"], "displayName": m.get("displayName") or m["model"],
                       "supportedReasoningEfforts": [e["reasoningEffort"] for e in m.get("supportedReasoningEfforts", [])],
                       "defaultReasoningEffort": m.get("defaultReasoningEffort")}
                      for m in response.get("data", []) if isinstance(m, dict) and m.get("model") and not m.get("hidden")]
            efforts = list(dict.fromkeys(CODEX_EFFORTS + [e for m in models for e in m["supportedReasoningEfforts"]]))
            self._catalog = {"models": models, "efforts": efforts, "source": "codex-model/list", "verified": True, "supportsCustomModel": True}
        except Exception:
            self._catalog = {"models": [{"model": self.default_settings["model"], "displayName": self.default_settings["model"]}]
                if self.default_settings.get("model") else [], "efforts": CODEX_EFFORTS,
                "source": "local-config", "verified": False, "supportsCustomModel": True,
                "note": "在线模型清单暂不可读；自定义模型/强度是否可用由下一轮 Codex 请求确认。"}
        return self._catalog

    async def runtime_status(self) -> dict[str, Any]:
        if not self.paths.bin.is_file() and not shutil_which(self.paths.bin.name):
            return {"ok": False, "error": f"Codex executable not found: {self.paths.bin}", "bin": str(self.paths.bin)}
        try:
            await self.client.start()
            self.version = await asyncio.to_thread(self._version)
        except Exception as exc:
            return {"ok": False, "error": str(exc), "bin": str(self.paths.bin)}
        return {
            "ok": True,
            "bin": str(self.paths.bin),
            "version": self.version,
            "codexHome": str(self.paths.home),
            "sessionsDirExists": self.paths.sessions_dir.is_dir(),
            "executionPolicy": self.policy.as_dict(),
        }

    def _version(self) -> str:
        try:
            completed = subprocess.run([str(self.paths.bin), "--version"], capture_output=True, text=True, timeout=10)
            return (completed.stdout or completed.stderr).strip() or "unknown"
        except (OSError, subprocess.SubprocessError):
            return "unknown"

    async def create_session(self, *, name: str, cwd: str | None = None) -> dict[str, Any]:
        try:
            await self.client.start()
            result = await self.client.start_thread(cwd or self.cwd)
            thread = result.get("thread") or {}
            thread_id = thread.get("id")
            if not thread_id:
                raise CodexAdapterError(f"thread/start returned no thread id: {result}")
            # thread/start already gives this app-server the new thread writer.
            # Do not resume it again before the first user-initiated turn.
            self._resumed.add(thread_id)
            if name:
                try:
                    await self.client.set_name(thread_id, name)
                except CodexRpcError:
                    pass
            return {
                "threadId": thread_id,
                "name": name,
                "cwd": thread.get("cwd") or cwd or self.cwd,
                "status": _status(thread.get("status")),
            }
        except CodexRpcError as exc:
            raise CodexAdapterError(exc.message) from exc

    async def create_project_session(self, *, name: str, cwd: str):
        return await self.create_session(name=name, cwd=cwd)

    async def resume_session(self, thread_id: str) -> dict[str, Any]:
        if thread_id not in self._resumed:
            try:
                result = await self.client.resume_thread(thread_id)
            except CodexRpcError as exc:
                raise CodexAdapterError(exc.message) from exc
            self._resumed.add(thread_id)
        else:
            result = {}
        thread = result.get("thread") or {}
        return {"threadId": thread_id, "name": thread.get("name"), "cwd": thread.get("cwd") or self.cwd, "status": _status(thread.get("status"))}

    async def send_turn(self, thread_id: str, text: str, *, on_delta: DeltaCallback | None = None, timeout: float = 180.0, attachments=None, settings=None) -> dict[str, Any]:
        lock = self._turn_locks.setdefault(thread_id, asyncio.Lock())
        async with lock:
            if thread_id not in self._resumed:
                await self.resume_session(thread_id)
            loop = asyncio.get_running_loop()
            completed: asyncio.Future[dict[str, Any]] = loop.create_future()
            turn_id: str | None = None
            deltas: list[str] = []

            async def handler(payload: dict[str, Any]) -> None:
                nonlocal turn_id
                method = payload.get("method")
                params = payload.get("params") or {}
                if params.get("threadId") and params.get("threadId") != thread_id:
                    return
                if method == "turn/started":
                    turn = params.get("turn") or {}
                    turn_id = turn.get("id") or turn_id
                elif method == "item/agentMessage/delta":
                    delta = params.get("delta") or params.get("text") or ""
                    if isinstance(delta, str) and delta:
                        deltas.append(delta)
                        if on_delta:
                            await on_delta(delta)
                elif method == "turn/completed":
                    turn = params.get("turn") or {}
                    matches = params.get("threadId") == thread_id or turn.get("threadId") == thread_id or (turn_id and turn.get("id") == turn_id)
                    if matches and not completed.done():
                        completed.set_result(turn)

            self.client.add_handler(handler)
            try:
                try:
                    start_options = {}
                    if attachments:
                        start_options["images"] = [str(a.path) for a in attachments if a.kind == "image"]
                    if settings:
                        start_options["settings"] = settings
                    start_result = await self.client.start_turn(thread_id, attachment_prompt(text, attachments or []), **start_options)
                except CodexRpcError as exc:
                    return {"status": "error", "error": exc.message}
                turn_id = (start_result.get("turn") or {}).get("id") or turn_id
                done, _ = await asyncio.wait({completed}, timeout=timeout)
                if not done:
                    return {"status": "running", "threadId": thread_id, "turnId": turn_id, "reply": "", "error": f"turn still running after {timeout:g}s"}
                turn = completed.result()
                reply = ""
                for item in turn.get("items") or []:
                    if item.get("type") == "agentMessage":
                        reply = item.get("text") or _text(item.get("content")) or ""
                if not reply:
                    reply = "".join(deltas)
                return {"status": _status(turn.get("status")) or "completed", "threadId": thread_id, "turnId": turn.get("id") or turn_id, "reply": reply}
            finally:
                self.client.remove_handler(handler)

    async def interrupt(self, thread_id: str) -> None:
        try:
            await self.client.interrupt(thread_id)
        except CodexRpcError as exc:
            raise CodexAdapterError(exc.message) from exc

    async def list_sessions(self, *, limit: int = 500, cursor: str | None = None) -> dict[str, Any]:
        # Read-only local data is the same source used by the MCP bridge and
        # does not require taking a Codex writer lock. Prefer it while the
        # Desktop app or another app-server owns the active thread database.
        local = list_local_sessions(self.paths, limit=limit, cursor=cursor)
        if local is not None:
            return local
        try:
            result = await self.client.list_sessions(limit=limit, cursor=cursor)
        except (CodexRpcError, RuntimeError) as exc:
            raise CodexAdapterError(str(exc)) from exc
        sessions = []
        for raw in result.get("data") or []:
            if not isinstance(raw, dict):
                continue
            status = raw.get("status") or {}
            cwd = raw.get("cwd") or ""
            sessions.append({
                "id": raw.get("id"),
                "name": raw.get("name") or raw.get("preview") or raw.get("id"),
                "cwd": cwd,
                "project": Path(cwd).name if cwd else "未指定项目",
                "status": _status(status),
                "updatedAt": raw.get("updatedAt"),
                "cliVersion": raw.get("cliVersion"),
                "source": raw.get("source"),
                "preview": str(raw.get("preview") or "")[:200],
            })
        return {"ok": True, "sessions": sessions, "nextCursor": result.get("nextCursor")}

    async def read_session(self, thread_id: str, *, limit: int = 30) -> dict[str, Any]:
        local = read_local_session(self.paths, thread_id, limit=limit)
        if local is not None:
            return local
        try:
            result = await self.client.read_thread(thread_id)
        except (CodexRpcError, RuntimeError) as exc:
            raise CodexAdapterError(str(exc)) from exc
        thread = result.get("thread") or {}
        messages: list[dict[str, str]] = []
        for turn in thread.get("turns") or []:
            for item in turn.get("items") or []:
                item_type = item.get("type")
                if item_type == "userMessage":
                    role, content = "user", _text(item.get("content"))
                elif item_type == "agentMessage":
                    role, content = "assistant", item.get("text") or _text(item.get("content"))
                else:
                    continue
                images = message_images(item.get("content"))
                if content or images:
                    message = {"role": role, "text": content}
                    if images:
                        message["images"] = images
                    messages.append(message)
        return {"ok": True, "threadId": thread_id, "name": thread.get("name"), "cwd": thread.get("cwd"), "status": _status(thread.get("status")), "messages": messages[-limit:], "observedSettings": {"model": thread.get("model"), "reasoningEffort": thread.get("reasoningEffort"), "source": "codex-thread/read"}}

    async def close(self) -> None:
        await self.client.close()


def shutil_which(name: str) -> str | None:
    import shutil
    return shutil.which(name)
