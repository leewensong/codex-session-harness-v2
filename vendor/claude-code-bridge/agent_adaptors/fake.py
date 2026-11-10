"""Deterministic provider for offline contract and MCP integration tests."""
import asyncio
import time
import uuid
from .contracts import AdaptorError, Capabilities, check_limit, check_timeout


class FakeAdaptor:
    provider = "fake"
    adaptor_version = "0.1.0"
    capabilities = Capabilities(stream=True, create_mode="native", history_source="memory", interrupt="owned-turn")

    def __init__(self, *, available=True, delay=0.01):
        self.available, self.delay = available, delay
        self.sessions, self.tasks = {}, {}
        self.closed = False

    def _check(self):
        if self.closed or not self.available:
            raise AdaptorError("runtime_unavailable", "fake runtime unavailable")

    async def runtime_status(self):
        return {"ok": self.available and not self.closed, "version": "fake-1"}

    async def create_session(self, *, name):
        self._check()
        sid = str(uuid.uuid4())
        self.sessions[sid] = {"id": sid, "name": name, "status": "idle", "updatedAt": time.time(), "messages": [], "writable": True}
        return {k: v for k, v in self.sessions[sid].items() if k != "messages"}

    async def resume_session(self, session_id):
        return await self.read_session(session_id)

    async def read_session(self, session_id, *, limit=30):
        self._check()
        check_limit(limit)
        if session_id not in self.sessions:
            raise AdaptorError("session_not_found", "unknown fake session")
        row = self.sessions[session_id]
        return {**row, "messages": row["messages"][-limit:]}

    async def list_sessions(self, *, limit=50, cursor=None):
        self._check()
        check_limit(limit)
        try:
            offset = int(cursor or "0")
            if offset < 0: raise ValueError()
        except (ValueError, TypeError):
            raise AdaptorError("invalid_cursor", "invalid cursor") from None
        rows = list(self.sessions.values())
        return {"sessions": [{k:v for k,v in r.items() if k != "messages"} for r in rows[offset:offset+limit]], "nextCursor": str(offset+limit) if offset+limit < len(rows) else None}

    async def send_turn(self, session_id, text, *, on_delta=None, timeout=180):
        check_timeout(timeout)
        await self.read_session(session_id)
        if session_id in self.tasks:
            raise AdaptorError("session_busy", "session already running")
        self.tasks[session_id] = asyncio.current_task()
        try:
            reply = "Fake: " + text
            async def run():
                for chunk in (reply[:6], reply[6:]):
                    await asyncio.sleep(self.delay)
                    if on_delta: await on_delta(chunk)
            await asyncio.wait_for(run(), timeout)
            self.sessions[session_id]["messages"] += [{"role": "user", "text": text}, {"role": "assistant", "text": reply}]
            return {"status": "completed", "sessionId": session_id, "reply": reply}
        except asyncio.TimeoutError:
            return {"status": "cancelled", "sessionId": session_id, "reply": "", "error": {"code": "turn_timeout", "message": "fake turn timed out"}}
        except asyncio.CancelledError:
            return {"status": "cancelled", "sessionId": session_id, "reply": ""}
        finally:
            self.tasks.pop(session_id, None)

    async def interrupt(self, session_id):
        if task := self.tasks.get(session_id):
            task.cancel()
            return {"ok": True, "status": "interrupted"}
        return {"ok": True, "status": "not-running"}

    async def close(self):
        self.closed = True
        tasks = list(self.tasks.values())
        for task in tasks: task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
