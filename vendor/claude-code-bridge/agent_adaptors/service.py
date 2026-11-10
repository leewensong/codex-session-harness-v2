"""Provider-neutral orchestration and bounded stream retention."""
from __future__ import annotations

import asyncio
import time
import uuid

from .contracts import AdaptorError, check_timeout

_RUNNING = {"starting", "running"}


class AdaptorService:
    def __init__(self, registry, *, max_streams=64, max_chars=1_000_000, retention=600):
        self.registry, self.streams = registry, {}
        self.max_streams, self.max_chars, self.retention = max_streams, max_chars, retention

    async def list_sessions(self, adaptor_id, *, limit=50, cursor=None):
        result = await self.registry.get(adaptor_id).list_sessions(limit=limit, cursor=cursor)
        return {**result, "sessions": [self.registry.public(adaptor_id, row) for row in result.get("sessions", [])]}

    async def create_session(self, adaptor_id, *, name):
        return self.registry.public(adaptor_id, await self.registry.get(adaptor_id).create_session(name=name))

    async def read_session(self, session_id, *, limit=30):
        adaptor_id = session_id.split(":", 1)[0] if isinstance(session_id, str) else ""
        adaptor, native = self.registry.route(session_id)
        return self.registry.public(adaptor_id, await adaptor.read_session(native, limit=limit))

    async def read_full_session(self, session_id):
        adaptor_id = session_id.split(":", 1)[0] if isinstance(session_id, str) else ""
        adaptor, native = self.registry.route(session_id)
        reader = getattr(adaptor, "read_full_session", None)
        if reader is None:
            raise AdaptorError("unsupported", "this adaptor does not support full transcript reads")
        return self.registry.public(adaptor_id, await reader(native))

    async def resume_session(self, session_id):
        adaptor_id = session_id.split(":", 1)[0] if isinstance(session_id, str) else ""
        adaptor, native = self.registry.route(session_id)
        return self.registry.public(adaptor_id, await adaptor.resume_session(native))

    async def interrupt(self, session_id):
        adaptor, native = self.registry.route(session_id)
        result = await adaptor.interrupt(native)
        if result.get("status") == "interrupt-requested":
            for state in self.streams.values():
                if state["sessionId"] == session_id and state["status"] in _RUNNING:
                    state["interruptRequested"] = True
        return result

    def _cleanup(self):
        for stream_id, state in list(self.streams.items()):
            if state.get("finishedAt") and time.monotonic() - state["finishedAt"] > self.retention:
                del self.streams[stream_id]

    def _apply_result(self, state, result):
        status = result.get("status")
        if status not in {"starting", "running", "completed", "cancelled", "error"}:
            raise AdaptorError("protocol_error", "adaptor returned unknown turn status")
        state["status"] = status
        reply = result.get("reply", "")
        if isinstance(reply, str):
            state["reply"] = reply[: self.max_chars]
            state["truncated"] |= len(reply) > self.max_chars
        if result.get("error"):
            state["error"] = result["error"]
        elif status not in _RUNNING:
            state.pop("error", None)
        if result.get("streamId"):
            state["backendStreamId"] = result["streamId"]
        if result.get("interruptRequested"):
            state["interruptRequested"] = True
        if status not in _RUNNING:
            state["finishedAt"] = time.monotonic()

    async def start_turn(self, session_id, text, *, timeout=180):
        check_timeout(timeout)
        if not isinstance(text, str) or not text.strip() or len(text) > 200000:
            raise AdaptorError("invalid_argument", "text must contain 1..200000 characters")
        adaptor, native = self.registry.route(session_id)
        if not adaptor.capabilities.send:
            raise AdaptorError("unsupported", "send unsupported")
        self._cleanup()
        if any(s["sessionId"] == session_id and s["status"] in _RUNNING for s in self.streams.values()):
            raise AdaptorError("session_busy", "session already running", retryable=True)
        if len(self.streams) >= self.max_streams:
            raise AdaptorError("capacity_exceeded", "stream capacity reached")

        stream_id = str(uuid.uuid4())
        state = {
            "streamId": stream_id,
            "sessionId": session_id,
            "status": "starting",
            "chunks": [],
            "chars": 0,
            "reply": "",
            "truncated": False,
            "adaptor": adaptor,
            "nativeId": native,
            "refreshLock": asyncio.Lock(),
        }
        self.streams[stream_id] = state

        async def delta(text):
            if not isinstance(text, str):
                raise AdaptorError("protocol_error", "delta must be text")
            room = max(0, self.max_chars - state["chars"])
            kept = text[:room]
            if kept:
                state["chunks"].append(kept)
                state["chars"] += len(kept)
            state["truncated"] |= len(kept) != len(text)

        async def run():
            state["status"] = "running"
            try:
                result = await adaptor.send_turn(native, text, on_delta=delta, timeout=timeout)
                self._apply_result(state, result)
            except asyncio.CancelledError:
                state["status"] = "cancelled"
                state["finishedAt"] = time.monotonic()
                raise
            except AdaptorError as exc:
                state.update(status="error", error=exc.as_dict()["error"], finishedAt=time.monotonic())
            except Exception:
                state.update(
                    status="error",
                    error={"code": "internal_error", "message": "adaptor turn failed"},
                    finishedAt=time.monotonic(),
                )

        state["task"] = asyncio.create_task(run())
        return {"streamId": stream_id, "sessionId": session_id, "status": "starting"}

    async def _refresh(self, state):
        task = state["task"]
        refresh = getattr(state["adaptor"], "refresh_turn", None)
        if state["status"] != "running" or not task.done() or refresh is None:
            return
        async with state["refreshLock"]:
            if state["status"] != "running":
                return

            async def delta(text):
                room = max(0, self.max_chars - state["chars"])
                kept = text[:room]
                if kept:
                    state["chunks"].append(kept)
                    state["chars"] += len(kept)
                state["truncated"] |= len(kept) != len(text)

            try:
                result = await refresh(state["nativeId"], state.get("backendStreamId"), on_delta=delta)
                self._apply_result(state, result)
            except AdaptorError as exc:
                # Transport ambiguity must not release the session.  Expose a
                # nonterminal diagnostic and allow a future poll/interrupt.
                state["error"] = exc.as_dict()["error"]
            except Exception:
                state["error"] = {"code": "internal_error", "message": "backend stream refresh failed; outcome unknown"}

    async def poll(self, stream_id, *, cursor=0):
        self._cleanup()
        state = self.streams.get(stream_id)
        if state is None:
            raise AdaptorError("stream_not_found", "unknown or expired stream")
        await self._refresh(state)
        if type(cursor) is not int or not 0 <= cursor <= len(state["chunks"]):
            raise AdaptorError("invalid_cursor", "cursor outside stream")
        hidden = {"task", "chars", "finishedAt", "chunks", "adaptor", "nativeId", "refreshLock"}
        return {
            **{k: v for k, v in state.items() if k not in hidden},
            "chunks": [{"index": i, "text": t} for i, t in enumerate(state["chunks"][cursor:], cursor)],
            "nextCursor": len(state["chunks"]),
        }

    async def send_turn(self, session_id, text, *, timeout=180):
        start = await self.start_turn(session_id, text, timeout=timeout)
        await asyncio.shield(self.streams[start["streamId"]]["task"])
        return await self.poll(start["streamId"])

    async def close(self):
        tasks = [s["task"] for s in self.streams.values() if not s["task"].done()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        return await self.registry.close()
