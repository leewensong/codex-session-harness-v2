"""Compatibility adaptor for the existing Codex MCP service.

A timed-out client wait does not imply that the backend turn stopped.  Pending
backend stream ids and cursors are retained so later service polls can continue
observing the same turn without resending the user message.
"""
from __future__ import annotations

import asyncio
import json
from urllib.parse import urlsplit

from .contracts import AdaptorError, Capabilities, check_limit, check_timeout

_TERMINAL = {"completed", "cancelled", "interrupted", "error", "failed"}
_RUNNING = {"starting", "running"}


class McpTransport:
    def __init__(self, endpoint: str):
        url = urlsplit(endpoint)
        if url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password:
            raise AdaptorError("invalid_config", "HTTP MCP URL without credentials required")
        self.endpoint = endpoint

    async def call(self, name, args):
        from mcp import ClientSession
        from mcp.client.streamable_http import streamable_http_client

        # Enter and exit in the same task; AnyIO cancel scopes are task-local.
        try:
            async with streamable_http_client(self.endpoint) as streams:
                async with ClientSession(streams[0], streams[1]) as session:
                    await session.initialize()
                    response = await session.call_tool(name, args)
                    if response.isError:
                        raise AdaptorError("provider_rejected", "Codex MCP rejected the operation")
                    value = getattr(response, "structuredContent", None)
                    if not isinstance(value, dict):
                        value = json.loads("".join(b.text for b in response.content if getattr(b, "type", None) == "text"))
                    if not isinstance(value, dict):
                        raise ValueError()
                    if value.get("ok") is False:
                        raise AdaptorError("provider_rejected", "Codex MCP rejected the operation")
                    return value
        except AdaptorError:
            raise
        except Exception:
            # A transport failure after a write can be ambiguous.  Callers must
            # not automatically retry a write or release the session lease.
            raise AdaptorError("transport_error", "Codex MCP request failed", retryable=True) from None

    async def close(self):
        return None


class CodexMcpAdaptor:
    provider = "codex"
    adaptor_version = "0.2.0"
    capabilities = Capabilities(
        stream=True,
        interrupt="owned-turn",
        external_session_write=True,
        create_mode="native",
        history_source="codex-mcp",
    )

    def __init__(self, *, cwd, endpoint=None, transport=None, poll_interval=0.2):
        self.cwd = cwd
        self.transport = transport or McpTransport(endpoint or "http://127.0.0.1:8765/mcp")
        self.poll_interval = poll_interval
        # session id -> backend stream/cursor state.  Presence means busy,
        # including ambiguous start failures and interrupt requests.
        self.pending: dict[str, dict] = {}
        self.closed = False

    @property
    def running(self) -> set[str]:
        return set(self.pending)

    async def _call(self, name, args):
        if self.closed:
            raise AdaptorError("closed", "adaptor is closed")
        value = await self.transport.call(name, args)
        if not isinstance(value, dict) or value.get("ok") is False:
            raise AdaptorError("provider_rejected", "Codex MCP operation failed")
        return value

    async def runtime_status(self):
        return await self._call("codex_runtime_status", {})

    async def create_session(self, *, name):
        row = await self._call("codex_start_session", {"name": name, "cwd": self.cwd})
        sid = row.get("threadId")
        if not isinstance(sid, str) or not sid:
            raise AdaptorError("protocol_error", "missing threadId")
        return {**row, "id": sid}

    async def resume_session(self, session_id):
        return await self.read_session(session_id)

    async def list_sessions(self, *, limit=50, cursor=None):
        check_limit(limit)
        return await self._call("codex_list_sessions", {"limit": limit, "cursor": cursor})

    async def read_session(self, session_id, *, limit=30):
        check_limit(limit)
        row = await self._call("codex_read_session", {"thread_id": session_id, "max_messages": limit})
        return {**row, "id": session_id}

    @staticmethod
    def _normalize_status(status):
        if status == "interrupted":
            return "cancelled"
        if status == "failed":
            return "error"
        return status

    async def _poll_once(self, session_id, *, on_delta=None):
        state = self.pending.get(session_id)
        if state is None:
            raise AdaptorError("turn_not_found", "no pending Codex turn")
        stream_id = state.get("streamId")
        if not stream_id:
            return {
                "status": "running",
                "sessionId": session_id,
                "reply": "".join(state["chunks"]),
                "error": {
                    "code": "ambiguous_write",
                    "message": "turn start outcome is unknown; interrupt and reconcile backend before restarting bridge",
                },
            }

        row = await self._call("codex_poll_stream", {"stream_id": stream_id, "cursor": state["cursor"]})
        chunks = row.get("chunks", [])
        if not isinstance(chunks, list):
            raise AdaptorError("protocol_error", "invalid Codex stream chunks")
        cursor = row.get("nextCursor")
        status = row.get("status")
        if type(cursor) is not int or cursor != state["cursor"] + len(chunks):
            raise AdaptorError("protocol_error", "invalid Codex stream cursor")
        if status not in _RUNNING | _TERMINAL:
            raise AdaptorError("protocol_error", "unknown Codex turn status")
        if row.get("reply") is not None and not isinstance(row["reply"], str):
            raise AdaptorError("protocol_error", "invalid Codex reply")
        if any(not isinstance(c, dict) or not isinstance(c.get("text"), str) for c in chunks):
            raise AdaptorError("protocol_error", "invalid Codex stream chunk")
        for chunk in chunks:
            delta = chunk["text"]
            # Consume each backend record once even when a callback fails.
            state["cursor"] += 1
            if delta:
                room = max(0, 1_000_000 - state["chars"])
                kept = delta[:room]
                if kept:
                    state["chunks"].append(kept)
                    state["chars"] += len(kept)
                if on_delta:
                    await on_delta(delta)
        normalized = self._normalize_status(status)
        result = {
            "status": normalized,
            "sessionId": session_id,
            "streamId": stream_id,
            "reply": row.get("reply") or "".join(state["chunks"]),
        }
        if normalized == "error":
            result["error"] = {"code": "provider_error", "message": "Codex turn failed"}
        if status in _TERMINAL:
            self.pending.pop(session_id, None)
        elif state.get("interruptRequested"):
            result["interruptRequested"] = True
        return result

    async def send_turn(self, session_id, text, *, on_delta=None, timeout=180):
        check_timeout(timeout)
        if self.closed:
            raise AdaptorError("closed", "adaptor is closed")
        if not isinstance(text, str) or not text.strip():
            raise AdaptorError("invalid_argument", "nonempty text required")
        if session_id in self.pending:
            raise AdaptorError("session_busy", "session already running")

        # Reserve before the write.  If transport fails after dispatch, retain
        # this state conservatively; automatic resend could duplicate a turn.
        self.pending[session_id] = {"streamId": None, "cursor": 0, "chunks": [], "chars": 0, "interruptRequested": False}
        try:
            async with asyncio.timeout(timeout):
                start = await self._call("codex_start_stream_message", {"thread_id": session_id, "message": text})
                stream_id = start.get("streamId")
                if not isinstance(stream_id, str) or not stream_id:
                    raise AdaptorError("protocol_error", "missing streamId")
                self.pending[session_id]["streamId"] = stream_id
                while True:
                    result = await self._poll_once(session_id, on_delta=on_delta)
                    if result["status"] not in _RUNNING:
                        return result
                    await asyncio.sleep(self.poll_interval)
        except asyncio.TimeoutError:
            state = self.pending[session_id]
            return {
                "status": "running",
                "sessionId": session_id,
                "streamId": state.get("streamId"),
                "reply": "".join(state["chunks"]),
                "error": {"code": "wait_timeout", "message": "backend turn is still pending"},
            }
        except AdaptorError as exc:
            state = self.pending[session_id]
            if exc.code == "provider_rejected" and state["streamId"] is None:
                self.pending.pop(session_id, None)
                raise
            # Once dispatched, even malformed responses are ambiguous. Keep
            # the stream observable and do not release the write reservation.
            return {"status": "running", "sessionId": session_id,
                    "streamId": state["streamId"], "reply": "".join(state["chunks"]),
                    "error": exc.as_dict()["error"]}

    async def refresh_turn(self, session_id, stream_id=None, *, on_delta=None):
        state = self.pending.get(session_id)
        if state is None:
            raise AdaptorError("turn_not_found", "no pending Codex turn")
        if stream_id is not None and state.get("streamId") not in {None, stream_id}:
            raise AdaptorError("protocol_error", "backend stream id mismatch")
        return await self._poll_once(session_id, on_delta=on_delta)

    async def interrupt(self, session_id):
        state = self.pending.get(session_id)
        if state is None:
            return {"ok": False, "status": "not-owned"}
        await self._call("codex_interrupt_turn", {"thread_id": session_id})
        state["interruptRequested"] = True
        # Keep busy until the stream reports a terminal state.
        return {"ok": True, "status": "interrupt-requested", "sideEffectsRolledBack": False}

    async def close(self):
        if self.closed:
            return
        errors = []
        for sid in list(self.pending):
            try:
                await asyncio.wait_for(self.interrupt(sid), 10)
            except Exception:
                errors.append(sid)
        self.closed = True
        await self.transport.close()
        if errors:
            raise AdaptorError("close_failed", "some backend turns could not be interrupted")
