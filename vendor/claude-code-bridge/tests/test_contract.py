import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from agent_adaptors.claude import ClaudeCodeAdaptor
from agent_adaptors.contracts import AdaptorError
from agent_adaptors.fake import FakeAdaptor
from agent_adaptors.history import History, project_dir_name
from agent_adaptors.registry import AdaptorRegistry
from agent_adaptors.service import AdaptorService
from agent_adaptors.stream import StreamParser


class ContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_registry_namespaces_multiple_adaptors(self):
        registry = AdaptorRegistry()
        registry.register("one", FakeAdaptor())
        registry.register("two", FakeAdaptor())
        service = AdaptorService(registry)
        first = await service.create_session("one", name="one")
        second = await service.create_session("two", name="two")
        self.assertTrue(first["id"].startswith("one:"))
        self.assertTrue(second["id"].startswith("two:"))
        read = await service.read_session(first["id"])
        self.assertEqual(read["adaptorId"], "one")
        await service.close()

    async def test_stream_retention_and_delta(self):
        registry = AdaptorRegistry()
        registry.register("fake", FakeAdaptor(delay=0))
        service = AdaptorService(registry, max_chars=5)
        session = await service.create_session("fake", name="test")
        stream = await service.start_turn(session["id"], "hello")
        while True:
            row = await service.poll(stream["streamId"])
            if row["status"] not in {"starting", "running"}:
                break
            await asyncio.sleep(0)
        self.assertEqual(row["status"], "completed")
        self.assertTrue(row["truncated"])
        self.assertGreater(row["nextCursor"], 0)
        await service.close()


class ParserTests(unittest.TestCase):
    def test_stream_filters_non_displayable_events(self):
        parser = StreamParser("s1")
        self.assertEqual(parser.feed(json.dumps({"type": "stream_event", "session_id": "s1", "event": {"type": "content_block_delta", "delta": {"type": "thinking_delta", "thinking": "secret"}}}).encode()), "")
        self.assertEqual(parser.feed(json.dumps({"type": "stream_event", "session_id": "s1", "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "hello"}}}).encode()), "hello")
        parser.feed(json.dumps({"type": "result", "session_id": "s1", "subtype": "success", "result": "hello"}).encode())
        self.assertEqual(parser.result["result"], "hello")

    def test_stream_rejects_duplicate_result(self):
        parser = StreamParser("s1")
        line = json.dumps({"type": "result", "session_id": "s1", "subtype": "success", "result": "x"}).encode()
        parser.feed(line)
        with self.assertRaises(AdaptorError):
            parser.feed(line)


class HistoryTests(unittest.TestCase):
    def test_history_filters_tools_and_mismatched_project(self):
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            projects = cwd / "projects"
            project = projects / project_dir_name(str(cwd))
            project.mkdir(parents=True)
            sid = "123e4567-e89b-12d3-a456-426614174000"
            path = project / f"{sid}.jsonl"
            rows = [
                {"type": "user", "sessionId": sid, "cwd": str(cwd), "message": {"role": "user", "content": "hi"}},
                {"type": "assistant", "sessionId": sid, "cwd": str(cwd), "message": {"role": "assistant", "content": [{"type": "text", "text": "there"}, {"type": "tool_use", "name": "secret"}]}},
            ]
            path.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")
            result = History(cwd, projects).read(sid)
            self.assertEqual([m["text"] for m in result["messages"]], ["hi", "there"])


class ConfigTests(unittest.TestCase):
    def test_duplicate_ids_rejected(self):
        with self.assertRaises(AdaptorError):
            AdaptorRegistry.from_config({"contractVersion": 1, "adaptors": [{"id": "x"}, {"id": "x"}]}, {})

    def test_invalid_entry_rejected(self):
        with self.assertRaises(AdaptorError):
            AdaptorRegistry.from_config({"contractVersion": 1, "adaptors": ["bad"]}, {})


if __name__ == "__main__":
    unittest.main()
