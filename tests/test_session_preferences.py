import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.adapters.codex.adapter import CodexSessionAdapter
from app.adapters.codex.config import CodexPaths
from app.adapters.codex.local_store import read_local_session
from app.adapters.codex.rpc import CodexRpcClient
from app.adapters.fake import FakeAdapter
from app.adapters.local_registry import ConfigurableClaudeSdkAdaptor
from app.gateway.server import HarnessServer
from app.orchestrator.session_manager import ManagedAssistant
from app.session_preferences import SessionPreferences, observed_claude_settings


class PreferencesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'prefs.json'
        self.store = SessionPreferences(self.path)
        self.catalog = {'models': [{'model': 'known', 'supportedReasoningEfforts': ['low', 'high']}], 'efforts': ['low', 'medium', 'high']}
        self.support = {'model': True, 'reasoningEffort': True}

    def save(self, session, value):
        return self.store.save(session, value, support=self.support, catalog=self.catalog)

    def test_settings_persist_and_sessions_are_isolated(self):
        self.save('codex:id', {'model': 'known', 'reasoningEffort': 'high'})
        self.assertEqual(self.store.get('claude:id')['model'], None)
        self.assertEqual(SessionPreferences(self.path).get('codex:id')['reasoningEffort'], 'high')
        self.save('codex:id', {'model': None, 'reasoningEffort': None})
        self.assertEqual(self.store.get('codex:id'), {'model': None, 'reasoningEffort': None})

    def test_bad_values_and_unsupported_combinations_are_rejected(self):
        for value in [{'model': '--exec evil'}, {'model': 0}, {'reasoningEffort': 'bogus'}, {'model': 'known', 'reasoningEffort': 'medium'}, {'path': 'evil'}]:
            with self.assertRaises(ValueError): self.save('s', value)
        with self.assertRaises(ValueError):
            self.store.save('s', {'model': 'custom:model'}, support={}, catalog=self.catalog)
        self.assertFalse(self.path.exists())

    def test_claude_model_is_observed_not_guessed_from_thinking_blocks(self):
        row = {'records': [
            {'record': {'type': 'assistant', 'message': {'model': 'actual', 'content': [{'type': 'thinking', 'thinking': 'lots of text'}]}}},
            {'record': {'type': 'assistant', 'isSidechain': True, 'message': {'model': 'subagent'}}},
        ]}
        self.assertEqual(observed_claude_settings(row), {'model': 'actual', 'reasoningEffort': None, 'source': 'claude-jsonl'})

    def test_claude_compaction_placeholder_does_not_replace_actual_model(self):
        rows = {'records': [
            {'record': {'type': 'assistant', 'message': {'model': 'actual-model'}}},
            {'record': {'type': 'assistant', 'message': {'model': '<synthetic>'}}},
        ]}
        self.assertEqual(observed_claude_settings(rows)['model'], 'actual-model')
        self.assertIsNone(observed_claude_settings({'records': rows['records'][1:]})['model'])

    def test_codex_reads_last_turn_model_and_effort_not_global_config(self):
        root=Path(self.temp.name); sessions=root/'sessions'; sessions.mkdir()
        sid='12345678-1234-1234-1234-123456789abc'
        (sessions/f'rollout-{sid}.jsonl').write_text('\n'.join(json.dumps(r) for r in [
            {'type':'session_meta','payload':{'cwd':str(root)}},
            {'type':'turn_context','payload':{'model':'old','effort':'low'}},
            {'type':'turn_context','payload':{'model':'latest','collaboration_mode':{'settings':{'reasoning_effort':'high'}}}},
        ]),encoding='utf-8')
        result=read_local_session(CodexPaths(root/'codex',root,sessions,root/'locks'),sid)
        self.assertEqual(result['observedSettings']['model'],'latest')
        self.assertEqual(result['observedSettings']['reasoningEffort'],'high')


class TurnSettingsTests(unittest.IsolatedAsyncioTestCase):
    async def test_codex_rpc_combines_model_effort_and_native_images(self):
        client=CodexRpcClient.__new__(CodexRpcClient)
        client.request=AsyncMock(return_value={})
        client.policy=SimpleNamespace(turn_settings=lambda: {'approvalPolicy':'never'})
        await client.start_turn('sid','hello',images=['D:/image.png'],settings={'model':'chosen','reasoningEffort':'high'})
        method,payload=client.request.await_args.args
        self.assertEqual(method,'turn/start')
        self.assertEqual(payload['model'],'chosen'); self.assertEqual(payload['effort'],'high')
        self.assertEqual(payload['input'][-1],{'type':'localImage','path':'D:/image.png'})

    async def test_claude_sdk_factory_is_task_local_and_does_not_mutate_default(self):
        adaptor=ConfigurableClaudeSdkAdaptor.__new__(ConfigurableClaudeSdkAdaptor)
        import contextvars
        adaptor._turn_options=contextvars.ContextVar('test-turn',default=None)
        adaptor.model='default'
        sdk=SimpleNamespace(ClaudeSDKClient=object,ClaudeAgentOptions=lambda **kw: kw)
        adaptor.sdk_module=sdk
        async def one(model):
            token=adaptor._turn_options.set({'model':model,'effort':'high'})
            try:
                await asyncio.sleep(0)
                return adaptor._sdk().ClaudeAgentOptions(model='default',cwd='project')
            finally: adaptor._turn_options.reset(token)
        a,b=await asyncio.gather(one('A'),one('B'))
        self.assertEqual(a['model'],'A'); self.assertEqual(b['model'],'B')
        self.assertIs(adaptor._sdk(),sdk); self.assertEqual(adaptor.model,'default')

    async def test_configured_claude_turn_passes_to_sdk_and_cleans_context(self):
        import contextvars
        adaptor=ConfigurableClaudeSdkAdaptor.__new__(ConfigurableClaudeSdkAdaptor)
        adaptor._turn_options=contextvars.ContextVar('test-configured',default=None)
        received={}
        async def send(_self,session_id,text,**kwargs):
            received.update(_self._turn_options.get())
            return {'status':'completed','reply':'ok'}
        with patch('agent_adaptors.claude_sdk.ClaudeSdkAdaptor.send_turn',new=send):
            await adaptor.send_configured_turn('sid','hello',settings={'model':'custom','reasoningEffort':'low'})
        self.assertEqual(received,{'model':'custom','effort':'low'})
        self.assertIsNone(adaptor._turn_options.get())

    async def test_browser_probe_does_not_create_or_send_any_turn(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); a=FakeAdapter(cwd=directory)
            a.create_session=AsyncMock(side_effect=AssertionError('no writes'))
            a.send_turn=AsyncMock(side_effect=AssertionError('no writes'))
            m=ManagedAssistant(adapter=a,project_dir=root,skill_path=root/'missing',data_dir=root,emit=AsyncMock())
            self.assertTrue(await m.activate_browser())
            a.create_session.assert_not_awaited(); a.send_turn.assert_not_awaited()

    async def test_gateway_save_applies_only_next_target_turn_and_reset(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); a=FakeAdapter(cwd=directory)
            m=ManagedAssistant(adapter=a,project_dir=root,skill_path=root/'unused',data_dir=root,emit=AsyncMock())
            await m.activate_browser()
            server=HarnessServer(fake=True); server.assistant=m; ws=AsyncMock()
            await server.handle_command(ws,{'type':'session.config.set','threadId':'demo-workspace', 'settings':{'model':'custom-model','reasoningEffort':'high'},'requestId':'setting1'})
            saved=ws.send_json.call_args.args[0]
            self.assertEqual(saved['type'],'session.config'); self.assertTrue(saved['payload']['saved'])
            self.assertNotIn('demo-workspace',a.applied_settings)
            await server.handle_command(ws,{'type':'session.send','threadId':'demo-workspace','text':'hello','clientMessageId':'test'})
            await asyncio.gather(*server.command_tasks)
            self.assertEqual(a.applied_settings['demo-workspace'],{'model':'custom-model','reasoningEffort':'high'})
            self.assertNotIn('demo-filters',a.applied_settings)
            await server.handle_command(ws,{'type':'session.config.set','threadId':'demo-workspace','settings':{'model':None,'reasoningEffort':None}})
            self.assertEqual(m.preferences.get('demo-workspace')['model'],None)

    async def test_unavailable_provider_rejects_setting_instead_of_ignoring(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); a=FakeAdapter(cwd=directory); a.settings_support={}
            m=ManagedAssistant(adapter=a,project_dir=root,skill_path=root/'unused',data_dir=root,emit=AsyncMock())
            server=HarnessServer(fake=True); server.assistant=m; ws=AsyncMock()
            await server.handle_command(ws,{'type':'session.config.set','threadId':'demo-workspace','settings':{'model':'chosen'}})
            self.assertEqual(ws.send_json.call_args.args[0]['type'],'command.error')

if __name__=='__main__': unittest.main()
