import asyncio
import unittest

from agent_adaptors.codex_mcp import CodexMcpAdaptor
from agent_adaptors.contracts import AdaptorError
from agent_adaptors.service import AdaptorService
from agent_adaptors.registry import AdaptorRegistry


class FakeTransport:
    def __init__(self):
        self.calls = []
        self.polls = 0
        self.interrupted = False
        self.sleep_first = True

    async def call(self, name, args):
        self.calls.append((name, args))
        if name == "codex_runtime_status":
            return {"ok": True, "version": "fake-codex"}
        if name == "codex_start_session":
            return {"ok": True, "threadId": "thread-1", "name": args["name"]}
        if name == "codex_read_session":
            return {"ok": True, "threadId": args["thread_id"], "messages": []}
        if name == "codex_start_stream_message":
            return {"ok": True, "streamId": "stream-1"}
        if name == "codex_poll_stream":
            if self.sleep_first:
                self.sleep_first = False
                await asyncio.sleep(0.02)
            self.polls += 1
            if self.interrupted or self.polls >= 2:
                return {"ok": True, "status": "completed" if not self.interrupted else "interrupted", "chunks": [{"text": "done"}], "nextCursor": args["cursor"] + 1, "reply": "done"}
            return {"ok": True, "status": "running", "chunks": [{"text": "do"}], "nextCursor": 1}
        if name == "codex_interrupt_turn":
            self.interrupted = True
            return {"ok": True}
        raise AssertionError(name)

    async def close(self):
        pass


class CodexAdaptorTests(unittest.IsolatedAsyncioTestCase):
    async def test_backend_poll_continues_after_client_timeout(self):
        transport = FakeTransport()
        adaptor = CodexMcpAdaptor(cwd=".", transport=transport, poll_interval=0)
        first = await adaptor.send_turn("thread-1", "hello", timeout=0.001)
        self.assertEqual(first["status"], "running")
        self.assertIn("thread-1", adaptor.running)
        second = await adaptor.refresh_turn("thread-1", first["streamId"])
        if second["status"] == "running":
            second = await adaptor.refresh_turn("thread-1", first["streamId"])
        self.assertEqual(second["status"], "completed")
        self.assertNotIn("thread-1", adaptor.running)
        await adaptor.close()

    async def test_interrupt_keeps_session_busy_until_terminal_poll(self):
        transport = FakeTransport()
        adaptor = CodexMcpAdaptor(cwd=".", transport=transport, poll_interval=0)
        first = await adaptor.send_turn("thread-1", "hello", timeout=0.001)
        result = await adaptor.interrupt("thread-1")
        self.assertEqual(result["status"], "interrupt-requested")
        with self.assertRaises(AdaptorError) as caught:
            await adaptor.send_turn("thread-1", "again", timeout=1)
        self.assertEqual(caught.exception.code, "session_busy")
        terminal = await adaptor.refresh_turn("thread-1", first["streamId"])
        self.assertEqual(terminal["status"], "cancelled")
        await adaptor.close()


if __name__ == "__main__":
    unittest.main()
