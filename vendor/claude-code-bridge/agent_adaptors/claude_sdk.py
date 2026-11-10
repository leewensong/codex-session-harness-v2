"""SDK turns on explicit, persisted Claude session UUIDs; no Desktop IPC needed."""
from __future__ import annotations

import asyncio
import importlib
import time
from dataclasses import replace
from pathlib import Path

from .claude import ClaudeCodeAdaptor, resolve_command
from .contracts import AdaptorError, Capabilities, check_timeout
from .history import native_id


class ClaudeSdkAdaptor(ClaudeCodeAdaptor):
    provider = "claude-code"  # Preserve existing Harness routing and namespaces.
    adaptor_version = "0.2.0"

    def __init__(self, *, allow_external_sessions=False, model=None,
                 permission_mode="dontAsk", sdk_module=None, **kwargs):
        if type(allow_external_sessions) is not bool:
            raise AdaptorError("invalid_config", "allowExternalSessions must be boolean")
        if permission_mode not in {"dontAsk", "default", "acceptEdits", "plan"}:
            raise AdaptorError("invalid_config", "unsupported SDK permission mode")
        if model is not None and (not isinstance(model, str) or not model.strip()):
            raise AdaptorError("invalid_config", "model must be a nonempty string")
        super().__init__(**kwargs)
        self.allow_external_sessions = allow_external_sessions
        self.model, self.permission_mode = model, permission_mode
        self.sdk_module = sdk_module
        self.clients = {}
        self.capabilities = replace(Capabilities(stream=True),
                                    external_session_write=allow_external_sessions)

    def _sdk(self):
        try:
            return self.sdk_module or importlib.import_module("claude_agent_sdk")
        except ImportError:
            raise AdaptorError("runtime_unavailable", "Install claude-agent-sdk in the Harness Python environment") from None

    async def runtime_status(self):
        self._check()
        try:
            sdk = self._sdk()
            if not callable(getattr(sdk, "ClaudeSDKClient", None)):
                raise AdaptorError("runtime_unavailable", "ClaudeSDKClient unavailable")
            return {"ok": True, "provider": self.provider, "backend": "sdk",
                    "capabilities": self.capabilities.as_dict(),
                    "executionPolicy": {"permissionMode": self.permission_mode, "model": self.model},
                    "diagnostics": {"authenticationVerified": False,
                                    "desktopRefresh": "not-guaranteed", "fullRead": True}}
        except AdaptorError as exc:
            return exc.as_dict()

    def _decorate(self, row, *, owned=False):
        row = dict(row)
        cwd = row.get("cwd")
        row["writable"] = bool(owned or (self.allow_external_sessions and isinstance(cwd, str)
                                        and cwd and Path(cwd).is_dir()))
        row["backend"] = "sdk"
        row["desktopRefresh"] = "not-guaranteed"
        if row.get("id") in self.active:
            row["status"] = "running"
        return row

    async def resume_session(self, session_id):
        self._check()
        sid = native_id(session_id)
        if sid in self.sessions:
            return self._decorate(await super().resume_session(sid), owned=True)
        if not self.allow_external_sessions:
            raise AdaptorError("session_not_owned", "external session writes require allowExternalSessions=true")
        # Discovery never adopts an external session into bridge-owned state.
        row = await asyncio.to_thread(self.history.read, sid, 1)
        row = self._decorate(row)
        if not row["writable"]:
            raise AdaptorError("project_unavailable", "target session working directory is missing or invalid")
        return row

    async def list_sessions(self, *, limit=50, cursor=None):
        result = await super().list_sessions(limit=limit, cursor=cursor)
        result["sessions"] = [self._decorate(row, owned=row["id"] in self.sessions)
                              for row in result["sessions"]]
        return result

    async def read_session(self, session_id, *, limit=30):
        return self._decorate(await super().read_session(session_id, limit=limit),
                              owned=native_id(session_id) in self.sessions)

    async def read_full_session(self, session_id):
        self._check()
        sid = native_id(session_id)
        if sid in self.sessions and not self.sessions[sid]["started"] and not self.history.path(sid).exists():
            return {**self._decorate(self._summary(sid), owned=True), "messages": [], "records": [],
                    "diagnostics": {"complete": True, "messageCount": 0, "recordCount": 0}}
        row = await asyncio.to_thread(self.history.read_full, sid)
        return self._decorate(row, owned=sid in self.sessions)

    async def send_turn(self, session_id, text, *, on_delta=None, timeout=180):
        self._check()
        check_timeout(timeout)
        if not isinstance(text, str) or not text.strip() or len(text) > 200000:
            raise AdaptorError("invalid_argument", "text must contain 1..200000 characters")
        sid = native_id(session_id)
        row = await self.resume_session(sid)
        if sid in self.active:
            raise AdaptorError("session_busy", "a turn is already running", retryable=True)
        # No awaits between reservation and task registration.
        self.active[sid] = None
        self.tasks[sid] = asyncio.current_task()
        started = time.monotonic()
        result = {"sessionId": sid, "status": "error", "reply": "",
                  "backend": "sdk", "desktopRefresh": "not-guaranteed"}
        client = None
        requested = False
        try:
            sdk = self._sdk()
            options = {"cwd": row["cwd"], "permission_mode": self.permission_mode,
                       "include_partial_messages": True}
            if self.model:
                options["model"] = self.model
            if self.binary:
                options["cli_path"] = resolve_command(self.binary)[0]
            if self.tools is not None:
                options["tools"] = self.tools
            if sid in self.sessions and not self.sessions[sid]["started"]:
                options["session_id"] = sid
            else:
                options["resume"] = sid
            client = sdk.ClaudeSDKClient(options=sdk.ClaudeAgentOptions(**options))
            self.clients[sid] = client
            # Same async task for connect/receive/disconnect (SDK anyio task group).
            async with asyncio.timeout(timeout):
                await client.connect()
                requested = True
                await client.query(text)
                terminal = None
                texts = []
                saw_delta = False
                async for message in client.receive_response():
                    response_sid = getattr(message, "session_id", None)
                    if response_sid and response_sid != sid:
                        raise AdaptorError("protocol_error", "SDK returned another session UUID; do not resend")
                    if getattr(message, "parent_tool_use_id", None):
                        continue
                    event = getattr(message, "event", None)
                    if isinstance(event, dict):
                        delta = event.get("delta") or {}
                        if event.get("type") == "content_block_delta" and delta.get("type") == "text_delta":
                            chunk = delta.get("text")
                            if isinstance(chunk, str) and chunk:
                                saw_delta = True
                                if on_delta:
                                    await on_delta(chunk)
                    if type(message).__name__ == "AssistantMessage":
                        for block in getattr(message, "content", []) or []:
                            chunk = getattr(block, "text", None)
                            if isinstance(chunk, str) and chunk:
                                texts.append(chunk)
                                if on_delta and not saw_delta:
                                    await on_delta(chunk)
                    if type(message).__name__ == "ResultMessage":
                        terminal = message
                        break
                if terminal is None:
                    raise AdaptorError("ambiguous_write", "SDK stream ended without a result; inspect history before retrying")
                reply = getattr(terminal, "result", None) or "\n\n".join(texts)
                if sid in self.cancelled:
                    return {**result, "status": "cancelled", "reply": reply}
                # An API error can still have subtype=success. is_error is authoritative.
                if getattr(terminal, "is_error", False) or getattr(terminal, "subtype", "success") != "success":
                    return {**result, "reply": reply, "error": {"code": "provider_error",
                            "message": "Claude SDK returned an error; inspect the reply/history; no automatic retry"}}
                if sid in self.sessions:
                    self.sessions[sid]["started"] = True
                    self.sessions[sid]["updatedAt"] = time.time()
                    self._save()
                return {**result, "status": "completed", "reply": reply,
                        "elapsedSeconds": time.monotonic() - started}
        except TimeoutError:
            return {**result, "status": "cancelled", "error": {"code": "turn_timeout",
                    "message": "SDK wait timed out; history may contain writes. No automatic resend."}}
        except asyncio.CancelledError:
            raise
        except AdaptorError:
            raise
        except Exception:
            # Never include provider stderr, environment tokens, or arbitrary SDK exception text.
            return {**result, "error": {"code": "ambiguous_write" if requested else "runtime_unavailable",
                    "message": "Claude SDK operation failed; inspect history before retrying"}}
        finally:
            try:
                if client is not None:
                    await client.disconnect()
            finally:
                self.clients.pop(sid, None)
                self.active.pop(sid, None)
                self.tasks.pop(sid, None)
                self.cancelled.discard(sid)

    async def interrupt(self, session_id):
        sid = native_id(session_id)
        if sid not in self.active:
            return {"ok": True, "status": "not-running"}
        self.cancelled.add(sid)
        client = self.clients.get(sid)
        if client is not None:
            await client.interrupt()
        return {"ok": True, "status": "interrupt-requested", "scope": "owned-turn",
                "sideEffectsRolledBack": False}
