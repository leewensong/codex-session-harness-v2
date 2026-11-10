import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from app.adapters.fake import FakeAdapter
from app.gateway.server import HarnessServer
from app.orchestrator.session_manager import ManagedAssistant


class SessionWorkspaceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.events = []
        self.adapter = FakeAdapter(cwd=str(self.root / 'workspace'))
        self.assistant = ManagedAssistant(adapter=self.adapter, project_dir=self.root,
            skill_path=Path('skills/codex-harness-assistant.md'), data_dir=self.root,
            emit=self.emit)
        self.assertTrue(await self.assistant.initialize())

    async def emit(self, event):
        self.events.append(event)

    async def test_send_targets_selected_session_not_managed_assistant(self):
        before = len(self.adapter.messages)
        result = await self.assistant.send('target-test', '继续开发', thread_id='demo-workspace')
        self.assertEqual(result.status, 'completed')
        self.assertEqual(len(self.adapter.messages), before)
        self.assertEqual(self.adapter._messages('demo-workspace')[-2]['text'], '继续开发')
        for event in self.events:
            if event['type'].startswith('chat.'):
                self.assertEqual(event['payload']['threadId'], 'demo-workspace')

    async def test_legacy_chat_still_targets_managed_assistant(self):
        result = await self.assistant.send('legacy', 'hello')
        self.assertEqual(result.thread_id, self.assistant.thread_id)
        self.assertEqual(self.adapter.messages[-2]['text'], 'hello')

    async def test_duplicate_id_does_not_write_twice_and_rejects_other_target(self):
        result = await self.assistant.send('same-id', 'hello', thread_id='demo-workspace')
        before = len(self.adapter._messages('demo-workspace'))
        self.assertIs(await self.assistant.send('same-id', 'hello', thread_id='demo-workspace'), result)
        self.assertEqual(len(self.adapter._messages('demo-workspace')), before)
        before_other = len(self.adapter._messages('demo-filters'))
        await self.assistant.send('same-id', 'hello', thread_id='demo-filters')
        self.assertEqual(self.events[-1]['type'], 'chat.error')
        self.assertEqual(len(self.adapter._messages('demo-filters')), before_other)

    async def test_same_named_folders_have_distinct_project_keys(self):
        result = await self.assistant.list_sessions()
        workspace = next(s for s in result['sessions'] if s['id'] == 'demo-workspace')
        duplicate = next(s for s in result['sessions'] if s['id'] == 'demo-duplicate')
        self.assertEqual(workspace['projectLabel'], duplicate['projectLabel'])
        self.assertNotEqual(workspace['projectKey'], duplicate['projectKey'])
        self.assertTrue(all('cwd' not in row for row in result['sessions']))

    async def test_history_limit_and_exact_target_are_preserved(self):
        result = await self.assistant.read_session('demo-long-history', limit=100)
        self.assertEqual(result['threadId'], 'demo-long-history')
        self.assertEqual(result['limit'], 100)
        self.assertEqual(len(result['messages']), 100)
        with self.assertRaises(ValueError):
            await self.assistant.read_session('demo-workspace', limit=300)
        with self.assertRaises(ValueError):
            await self.assistant.read_session('', limit=30)

    async def test_initialization_messages_hidden_only_in_managed_session(self):
        self.assertEqual((await self.assistant.read_session(self.assistant.thread_id))['messages'], [])
        self.assertEqual(len((await self.assistant.read_session('demo-workspace'))['messages']), 4)

    async def test_pagination_cursor_reaches_adapter(self):
        result = await self.assistant.list_sessions(cursor='4')
        self.assertEqual(len(result['sessions']), 3)
        self.assertFalse(any(row['managed'] for row in result['sessions']))

    async def test_final_reply_replaces_intermediate_deltas(self):
        async def send(thread_id, text, *, on_delta, timeout):
            await on_delta('intermediate progress')
            return {'status': 'completed', 'reply': 'authoritative final'}
        self.adapter.send_turn = send
        result = await self.assistant.send('final', 'hello', thread_id='demo-workspace')
        self.assertEqual(result.reply, 'authoritative final')
        self.assertEqual(self.events[-1]['payload']['reply'], 'authoritative final')

    async def test_read_does_not_resume_or_send(self):
        self.adapter.resume_session = AsyncMock(side_effect=AssertionError('must not resume'))
        self.adapter.send_turn = AsyncMock(side_effect=AssertionError('must not send'))
        await self.assistant.read_session('demo-workspace')
        self.adapter.resume_session.assert_not_awaited()
        self.adapter.send_turn.assert_not_awaited()

    async def test_inflight_snapshot_and_cancel_retain_target(self):
        started = asyncio.Event()
        stopped = asyncio.Event()
        async def slow_send(thread_id, text, *, on_delta, timeout):
            started.set()
            await stopped.wait()
            return {'status': 'cancelled', 'reply': ''}
        self.adapter.send_turn = slow_send
        self.adapter.interrupt = AsyncMock(side_effect=lambda _: stopped.set())
        task = asyncio.create_task(self.assistant.send('running-id', 'slow', thread_id='demo-workspace'))
        await asyncio.wait_for(started.wait(), 2)
        active = self.assistant.snapshot()['messagesInFlight'][0]
        self.assertEqual(active['threadId'], 'demo-workspace')
        self.assertEqual(active['text'], 'slow')
        self.assertTrue(await self.assistant.cancel('running-id'))
        await asyncio.wait_for(task, 2)
        self.adapter.interrupt.assert_awaited_once_with('demo-workspace')

    async def test_gateway_echoes_request_id_and_rejects_invalid_target(self):
        server = HarnessServer(fake=True)
        server.assistant = self.assistant
        ws = AsyncMock()
        await server.handle_command(ws, {'type': 'session.read', 'threadId': 'demo-workspace', 'requestId': 'read-A', 'limit': 30})
        event = ws.send_json.call_args.args[0]
        self.assertEqual(event['requestId'], 'read-A')
        self.assertEqual(event['payload']['threadId'], 'demo-workspace')
        await server.handle_command(ws, {'type': 'sessions.list', 'requestId': 'list-B'})
        self.assertEqual(ws.send_json.call_args.args[0]['requestId'], 'list-B')
        await server.handle_command(ws, {'type': 'session.send', 'threadId': 'invalid', 'clientMessageId': 'bad', 'text': 'hello', 'requestId': 'send-C'})
        event = ws.send_json.call_args.args[0]
        self.assertEqual(event['type'], 'command.error')
        self.assertEqual(event['requestId'], 'send-C')
        self.assertEqual(event['payload']['clientMessageId'], 'bad')
        self.assertEqual(server.command_tasks, set())

    async def test_gateway_send_and_retry_use_the_original_session_and_text(self):
        server = HarnessServer(fake=True)
        server.assistant = self.assistant
        ws = AsyncMock()
        await server.handle_command(ws, {'type': 'session.send', 'threadId': 'demo-filters', 'clientMessageId': 'retry-me', 'text': 'original text'})
        await asyncio.gather(*server.command_tasks)
        await server.handle_command(ws, {'type': 'chat.retry', 'clientMessageId': 'retry-me'})
        await asyncio.gather(*server.command_tasks)
        retry = self.assistant._records['retry-me-retry']
        self.assertEqual(retry.thread_id, 'demo-filters')
        self.assertEqual(retry.text, 'original text')


if __name__ == '__main__':
    unittest.main()
