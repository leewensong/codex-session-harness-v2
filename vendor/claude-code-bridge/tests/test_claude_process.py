import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_adaptors.claude import ClaudeCodeAdaptor
from agent_adaptors.contracts import AdaptorError
from agent_adaptors.history import History, MAX_LINE


class ProcessAdaptor(ClaudeCodeAdaptor):
    mode = "flood"

    async def _spawn(self, *args):
        sid = args[args.index("--session-id") + 1] if "--session-id" in args else args[args.index("--resume") + 1]
        return await asyncio.create_subprocess_exec(
            sys.executable, str(Path(__file__).with_name("process_fixture.py")), self.mode, sid, *args,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, limit=4 * 1024 * 1024)


class ProcessTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.adaptor = ProcessAdaptor(cwd=str(self.root), state_dir=str(self.root / "state"), projects_dir=str(self.root / "projects"))
        self.sid = (await self.adaptor.create_session(name="test"))["id"]

    async def asyncTearDown(self):
        await self.adaptor.close()
        self.tmp.cleanup()

    async def test_large_stderr_and_json(self):
        result = await self.adaptor.send_turn(self.sid, "-not-an-option", timeout=10)
        self.assertEqual(result["reply"], "hello")
        self.assertEqual(result["status"], "completed")
        self.assertEqual(self.adaptor.active, {})

    async def test_stream_filters_thinking(self):
        chunks = []
        async def callback(text):
            chunks.append(text)
        result = await self.adaptor.send_turn(self.sid, "hi", on_delta=callback, timeout=10)
        self.assertEqual(chunks, ["hello"])
        self.assertEqual(result["reply"], "hello")

    async def test_timeout_reaps_process(self):
        self.adaptor.mode = "slow"
        result = await self.adaptor.send_turn(self.sid, "hi", timeout=0.1)
        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(result["error"]["code"], "turn_timeout")
        self.assertFalse(self.adaptor.active)

    async def test_interrupt_during_process(self):
        self.adaptor.mode = "slow"
        task = asyncio.create_task(self.adaptor.send_turn(self.sid, "hi", timeout=10))
        while self.adaptor.active.get(self.sid) is None:
            await asyncio.sleep(0.001)
        proc = self.adaptor.active[self.sid]
        result = await self.adaptor.interrupt(self.sid)
        self.assertEqual(result["scope"], "owned-process")
        self.assertEqual((await task)["status"], "cancelled")
        self.assertIsNotNone(proc.returncode)

    async def test_invalid_output(self):
        self.adaptor.mode = "invalid"
        with self.assertRaises(AdaptorError) as ctx:
            await self.adaptor.send_turn(self.sid, "hi", timeout=10)
        self.assertEqual(ctx.exception.code, "protocol_error")
        self.assertFalse(self.adaptor.active)

    async def test_oversized_stream(self):
        self.adaptor.mode = "oversized"
        async def callback(text):
            pass
        with self.assertRaises(AdaptorError) as ctx:
            await self.adaptor.send_turn(self.sid, "hi", on_delta=callback, timeout=10)
        self.assertEqual(ctx.exception.code, "output_limit")
        self.assertFalse(self.adaptor.active)

    async def test_callback_failure_reaps_process(self):
        async def callback(text):
            raise RuntimeError("consumer unavailable")
        with self.assertRaises(RuntimeError):
            await self.adaptor.send_turn(self.sid, "hi", on_delta=callback, timeout=10)
        self.assertFalse(self.adaptor.active)

    async def test_external_session_cannot_be_written(self):
        with self.assertRaises(AdaptorError) as ctx:
            await self.adaptor.send_turn("123e4567-e89b-12d3-a456-426614174000", "hi")
        self.assertEqual(ctx.exception.code, "session_not_owned")

    async def test_close_releases_state_for_reopen(self):
        await self.adaptor.close()
        other = ClaudeCodeAdaptor(cwd=str(self.root), state_dir=str(self.root / "state"))
        self.assertIn(self.sid, other.sessions)
        await other.close()

    async def test_corrupt_state_releases_lease(self):
        await self.adaptor.close()
        (self.root / "state" / "sessions.json").write_text("[]", encoding="utf-8")
        for _ in range(2):
            with self.assertRaises(AdaptorError) as ctx:
                ClaudeCodeAdaptor(cwd=str(self.root), state_dir=str(self.root / "state"))
            self.assertEqual(ctx.exception.code, "invalid_state")


class HistoryRobustnessTests(unittest.TestCase):
    def test_bad_lines_oversize_and_project_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            history = History(root, root / "projects")
            history.directory.mkdir(parents=True)
            sid = "123e4567-e89b-12d3-a456-426614174000"
            row = {"type": "user", "sessionId": sid, "cwd": str(root), "message": {"content": "visible"}}
            history.path(sid).write_text("bad\n" + "x" * (MAX_LINE + 1) + "\n" + json.dumps(row), encoding="utf-8")
            result = history.read(sid)
            self.assertEqual(result["diagnostics"]["parseWarnings"], 2)
            self.assertEqual(result["messages"][0]["text"], "visible")
            row["cwd"] = str(root / "other-project")
            history.path(sid).write_text(json.dumps(row), encoding="utf-8")
            with self.assertRaises(AdaptorError) as ctx:
                history.read(sid)
            self.assertEqual(ctx.exception.code, "project_mismatch")


if __name__ == "__main__":
    unittest.main()
