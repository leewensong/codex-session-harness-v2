import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from app.adapters.codex.config import CodexPaths
from app.adapters.codex.local_store import list_local_sessions, read_local_session
from app.adapters.codex.rpc import CodexRpcClient


class LocalCodexStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.home = root / 'codex-home'
        self.sessions = self.home / 'sessions' / '2026' / '10' / '08'
        self.locks = self.home / 'thread-writer-locks'
        self.sessions.mkdir(parents=True)
        self.locks.mkdir(parents=True)
        self.thread_id = '12345678-1234-1234-1234-123456789abc'
        self.rollout = self.sessions / f'rollout-2026-10-08T12-00-00-{self.thread_id}.jsonl'
        self.rollout.write_text('\n'.join([
            json.dumps({'type': 'session_meta', 'payload': {'cwd': 'D:\\\\Newproj\\\\demo', 'timestamp': '2026-10-08T12:00:00Z', 'cli_version': 'test'}}),
            json.dumps({'type': 'response_item', 'payload': {'type': 'message', 'role': 'user', 'content': [{'type': 'text', 'text': 'hello'}]}}),
            json.dumps({'type': 'response_item', 'payload': {'type': 'message', 'role': 'assistant', 'content': [{'type': 'text', 'text': 'world'}]}}),
            json.dumps({'type': 'response_item', 'payload': {'type': 'function_call', 'name': 'hidden'}}),
        ]), encoding='utf-8')
        (self.home / 'session_index.jsonl').write_text(json.dumps({'id': self.thread_id, 'thread_name': 'Local demo', 'updated_at': '2026-10-08T12:01:00Z'}) + '\n', encoding='utf-8')
        self.paths = CodexPaths(root / 'codex', self.home, self.home / 'sessions', self.locks)

    def tearDown(self):
        self.temp.cleanup()

    def test_list_reads_index_without_app_server(self):
        result = list_local_sessions(self.paths, limit=10)
        self.assertEqual(result['source'], 'local-rollout')
        self.assertEqual(result['sessions'][0]['name'], 'Local demo')
        self.assertEqual(result['sessions'][0]['project'], 'demo')
        self.assertEqual(result['sessions'][0]['preview'], 'hello')

    def test_read_returns_only_visible_messages_and_honours_limit(self):
        result = read_local_session(self.paths, self.thread_id, limit=1)
        self.assertEqual(result['source'], 'local-rollout')
        self.assertEqual(result['messages'], [{'role': 'assistant', 'text': 'world'}])

    def test_missing_local_session_returns_none(self):
        self.assertIsNone(read_local_session(self.paths, 'missing', limit=30))

    def test_sqlite_catalog_includes_threads_missing_from_jsonl_index(self):
        sqlite_thread = '22345678-1234-1234-1234-123456789abc'
        state = sqlite3.connect(self.home / 'state_5.sqlite')
        state.execute('''
            CREATE TABLE threads (
                id TEXT PRIMARY KEY, name TEXT, title TEXT, updated_at INTEGER,
                updated_at_ms INTEGER, archived INTEGER, rollout_path TEXT,
                cwd TEXT, preview TEXT, model TEXT, reasoning_effort TEXT,
                source TEXT, cli_version TEXT
            )
        ''')
        state.execute('''
            INSERT INTO threads VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (sqlite_thread, 'SQLite session', 'fallback title', 1791450000,
              1791450000000, 1, '', r'\\?\D:\Newproj\CodexHTML',
              'sqlite preview', 'gpt-test', 'high', 'vscode', 'test'))
        state.commit()
        state.close()

        history = sqlite3.connect(self.home / 'thread_history_1.sqlite')
        history.execute('CREATE TABLE thread_items (thread_id TEXT, rollout_ordinal INTEGER, item_json TEXT)')
        history.execute('INSERT INTO thread_items VALUES (?, ?, ?)', (sqlite_thread, 1, json.dumps({
            'type': 'userMessage', 'content': [{'type': 'text', 'text': 'from sqlite'}]
        })))
        history.execute('INSERT INTO thread_items VALUES (?, ?, ?)', (sqlite_thread, 2, json.dumps({
            'type': 'agentMessage', 'text': 'sqlite reply'
        })))
        history.commit()
        history.close()

        paths = CodexPaths(
            self.paths.bin, self.home, self.paths.sessions_dir, self.paths.lock_dir,
            self.home / 'state_5.sqlite', self.home / 'thread_history_1.sqlite',
        )
        result = list_local_sessions(paths, limit=100)
        session = next(row for row in result['sessions'] if row['id'] == sqlite_thread)
        self.assertEqual(session['project'], 'CodexHTML')
        self.assertTrue(session['archived'])
        self.assertEqual(session['model'], 'gpt-test')

        history_result = read_local_session(paths, sqlite_thread, limit=10)
        self.assertEqual(history_result['source'], 'local-sqlite')
        self.assertEqual(history_result['messages'], [
            {'role': 'user', 'text': 'from sqlite'},
            {'role': 'assistant', 'text': 'sqlite reply'},
        ])
        self.assertEqual(history_result['observedSettings']['reasoningEffort'], 'high')


class RpcTimeoutTests(unittest.IsolatedAsyncioTestCase):
    async def test_list_uses_short_read_timeout(self):
        client = CodexRpcClient.__new__(CodexRpcClient)
        client.timeout = 30.0
        client.request = AsyncMock(return_value={})
        await CodexRpcClient.list_sessions(client, limit=10)
        self.assertEqual(client.request.await_args.kwargs['timeout'], 8.0)

    async def test_read_uses_short_read_timeout(self):
        client = CodexRpcClient.__new__(CodexRpcClient)
        client.timeout = 30.0
        client.request = AsyncMock(return_value={})
        await CodexRpcClient.read_thread(client, 'thread')
        self.assertEqual(client.request.await_args.kwargs['timeout'], 8.0)


if __name__ == '__main__':
    unittest.main()
