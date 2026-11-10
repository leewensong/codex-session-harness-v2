import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiohttp.test_utils import TestClient, TestServer
from app.adapters.codex.adapter import CodexSessionAdapter
from app.adapters.fake import FakeAdapter
from app.adapters.local_registry import ConfigurableClaudeSdkAdaptor, NativeCodexAdaptor
from app.adapters.unified import UnifiedHarnessAdapter
from app.created_sessions import CreatedSessions
from app.gateway.server import HarnessServer, build_app
from app.orchestrator.session_manager import ManagedAssistant
from agent_adaptors.registry import AdaptorRegistry
from agent_adaptors.service import AdaptorService


class ProjectCreationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.first = self.root / 'first' / 'app'
        self.second = self.root / 'second' / 'app'
        self.first.mkdir(parents=True); self.second.mkdir(parents=True)
        self.adapter = FakeAdapter(cwd=str(self.first))
        duplicate = next(r for r in self.adapter.examples if r['id'] == 'demo-duplicate')
        duplicate.update(cwd=str(self.second), project='app')
        self.manager = self.new_manager(self.adapter)
        await self.manager.activate_browser()
        self.rows = (await self.manager.list_sessions())['sessions']

    def new_manager(self, adapter):
        return ManagedAssistant(adapter=adapter, project_dir=self.root, skill_path=self.root / 'unused',
                                data_dir=self.root / 'state', emit=AsyncMock())

    def row(self, sid):
        return next(row for row in self.rows if row['id'] == sid)

    async def create(self, sid='demo-workspace', request='create-test'):
        return await self.manager.create_project_session(sid, '新会话', request, project_key=self.row(sid)['projectKey'])

    async def test_creation_is_empty_and_uses_selected_project_not_default(self):
        self.adapter.send_turn = AsyncMock(side_effect=AssertionError('creating must not send'))
        created = await self.create('demo-duplicate')
        self.assertEqual(created['projectKey'], self.row('demo-duplicate')['projectKey'])
        self.assertNotEqual(created['projectKey'], self.row('demo-workspace')['projectKey'])
        self.assertNotIn('cwd', created)
        history = await self.manager.read_session(created['id'])
        self.assertEqual(history['messages'], [])
        self.assertEqual(Path(history['cwd']), self.second)
        self.assertTrue(created['canCreateSession'])
        self.adapter.send_turn.assert_not_awaited()

    async def test_repeated_concurrent_request_and_restart_create_only_once(self):
        original = self.adapter.create_project_session
        self.adapter.create_project_session = AsyncMock(wraps=original)
        one, two = await asyncio.gather(self.create(), self.create())
        self.assertEqual(one['id'], two['id'])
        self.adapter.create_project_session.assert_awaited_once()
        restored = self.new_manager(self.adapter)
        duplicate = await restored.create_project_session('demo-workspace', '新会话', 'create-test', project_key=self.row('demo-workspace')['projectKey'])
        self.assertEqual(duplicate['id'], one['id'])
        self.adapter.create_project_session.assert_awaited_once()
        with self.assertRaisesRegex(ValueError, 'requestId'):
            await restored.create_project_session('demo-duplicate', '新会话', 'create-test', project_key=self.row('demo-duplicate')['projectKey'])

    async def test_created_sessions_survive_missing_provider_index_without_duplicates(self):
        created = await self.create()
        self.assertEqual(sum(s['id'] == created['id'] for s in (await self.manager.list_sessions())['sessions']), 1)
        self.adapter.examples = [s for s in self.adapter.examples if s['id'] != created['id']]
        restored = self.new_manager(self.adapter)
        rows = (await restored.list_sessions())['sessions']
        self.assertEqual(sum(s['id'] == created['id'] for s in rows), 1)
        self.adapter.read_session = AsyncMock(side_effect=RuntimeError('unmaterialized thread'))
        self.assertEqual((await restored.read_session(created['id']))['messages'], [])
        restored.created_sessions.mark_materialized(created['id'])
        with self.assertRaises(RuntimeError):
            await restored.read_session(created['id'])

    async def test_unknown_reference_wrong_project_bad_names_and_missing_directory_rejected(self):
        self.adapter.create_project_session = AsyncMock()
        for sid, name, key in [('unknown', 'valid', None), ('demo-workspace','valid','bad'), ('demo-workspace','\ninvalid',None), ('demo-workspace','x'*161,None)]:
            with self.assertRaises(ValueError):
                await self.manager.create_project_session(sid,name,'test-request',project_key=key)
        self.first.rmdir()
        with self.assertRaisesRegex(ValueError, '目录'):
            await self.create()
        self.adapter.create_project_session.assert_not_awaited()

    async def test_unsupported_adaptor_does_not_silently_create_in_default_project(self):
        self.adapter.supports_project_create = False
        with self.assertRaisesRegex(ValueError,'不支持'):
            await self.create()
        rows = (await self.manager.list_sessions())['sessions']
        self.assertFalse(any(r['canCreateSession'] for r in rows))

    async def test_gateway_rejects_arbitrary_cwd_and_echoes_creation_request_id(self):
        server = HarnessServer(fake=True)
        server.assistant = self.manager
        ws = AsyncMock()
        command = {'type':'session.create', 'referenceId':'demo-workspace', 'projectKey':self.row('demo-workspace')['projectKey'], 'requestId':'gateway-create'}
        await server.handle_command(ws,{**command,'cwd':'D:/arbitrary'})
        self.assertEqual(ws.send_json.call_args.args[0]['type'],'command.error')
        await server.handle_command(ws,command)
        event = ws.send_json.call_args.args[0]
        self.assertEqual(event['type'],'session.created'); self.assertEqual(event['requestId'],'gateway-create')
        self.assertEqual(event['payload']['agentType'],'codex')
        self.assertEqual(event['payload']['session']['projectKey'],command['projectKey'])
        self.assertEqual(server.command_tasks,set())


class NativeCodexCreationTests(unittest.IsolatedAsyncioTestCase):
    async def test_native_start_uses_target_cwd_and_does_not_start_a_turn(self):
        with tempfile.TemporaryDirectory() as directory:
            adapter = CodexSessionAdapter(cwd=directory)
            adapter.client = SimpleNamespace(start=AsyncMock(), start_thread=AsyncMock(return_value={'thread':{'id':'native-created','status':{'type':'notLoaded'}}}), set_name=AsyncMock(), start_turn=AsyncMock(), resume_thread=AsyncMock())
            result=await adapter.create_project_session(name='新会话',cwd=directory+'/target')
            adapter.client.start_thread.assert_awaited_once_with(directory+'/target')
            self.assertIn(result['threadId'],adapter._resumed)
            self.assertEqual(result['cwd'],directory+'/target')
            adapter.client.start_turn.assert_not_awaited(); adapter.client.resume_thread.assert_not_awaited()


class ClaudeProjectCreationTests(unittest.IsolatedAsyncioTestCase):
    async def test_sdk_first_and_subsequent_turns_keep_project_and_uuid_after_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); default=root/'default'; target=root/'target'; state=root/'state'; projects=root/'projects'
            default.mkdir(); target.mkdir(); projects.mkdir()
            calls=[]
            class ResultMessage:
                def __init__(self,sid):
                    self.session_id=sid; self.result='mock reply'; self.is_error=False; self.subtype='success'
            class Client:
                def __init__(self, options): self.options=options; calls.append(options)
                async def connect(self): pass
                async def query(self,text): pass
                async def disconnect(self): pass
                async def receive_response(self):
                    yield ResultMessage(self.options.get('session_id') or self.options['resume'])
            sdk=SimpleNamespace(ClaudeSDKClient=Client,ClaudeAgentOptions=lambda **kw: kw)
            def construct():
                return ConfigurableClaudeSdkAdaptor(cwd=str(default),state_dir=str(state),projects_dir=str(projects),sdk_module=sdk)
            adaptor=construct()
            try:
                created=await adaptor.create_project_session(name='new',cwd=str(target))
                sid=created['id']
                self.assertEqual(calls,[])
                self.assertEqual((await adaptor.read_full_session(sid))['messages'],[])
                self.assertEqual(Path(created['cwd']),target)
            finally: await adaptor.close()
            adaptor=construct()
            try:
                self.assertEqual(Path((await adaptor.resume_session(sid))['cwd']),target)
                first=await adaptor.send_configured_turn(sid,'hello')
                self.assertEqual(first['status'],'completed')
                self.assertEqual(calls[0]['session_id'],sid); self.assertEqual(Path(calls[0]['cwd']),target)
                # Simulate only provider-written JSONL, never a real cloud call.
                history_path=adaptor._project_history(sid).path(sid)
                history_path.parent.mkdir(parents=True)
                history_path.write_text(json.dumps({'type':'user','sessionId':sid,'cwd':str(target),'timestamp':'2026-10-14T00:00:00Z','message':{'role':'user','content':'hello'}})+'\n',encoding='utf-8')
                second=await adaptor.send_configured_turn(sid,'next')
                self.assertEqual(second['status'],'completed')
                self.assertEqual(calls[1]['resume'],sid); self.assertEqual(Path(calls[1]['cwd']),target)
                self.assertEqual(Path((await adaptor.read_session(sid))['cwd']),target)
                rows=(await adaptor.list_sessions(limit=100))['sessions']
                self.assertEqual(Path(next(r for r in rows if r['id']==sid)['cwd']),target)
                self.assertEqual(adaptor.cwd,default)
            finally: await adaptor.close()

    async def test_facade_namespaces_project_creation_without_modifying_shared_registry(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); (root/'project').mkdir()
            adaptor=ConfigurableClaudeSdkAdaptor(cwd=directory,state_dir=str(root/'state'),projects_dir=str(root/'projects'))
            registry=AdaptorRegistry(); registry.register('claude-local',adaptor)
            service=AdaptorService(registry)
            facade=UnifiedHarnessAdapter(registry=registry,service=service,adaptor_id='claude-local')
            try:
                result=await facade.create_project_session(name='New',cwd=str(root/'project'))
                self.assertTrue(result['threadId'].startswith('claude-local:'))
                self.assertEqual((await facade.read_session(result['threadId']))['messages'],[])
                with self.assertRaises(Exception): await facade.read_session('other:'+result['nativeId'])
            finally: await service.close()


class RegistryIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def test_v2_state_override_does_not_touch_or_compete_with_v1_lease(self):
        from app.adapters.unified import build_unified_pool
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            old=ConfigurableClaudeSdkAdaptor(cwd=directory,state_dir=str(root/'v1'),projects_dir=str(root/'projects'))
            config=root/'adaptors.json'
            config.write_text(json.dumps({'contractVersion':1,'adaptors':[{'id':'cc','type':'claude-code','enabled':True,
                'backend':'sdk','cwd':'.','stateDir':'v1','v2StateDir':'v2','projectsDir':'projects'}]}),encoding='utf-8')
            pool=None
            try:
                pool=build_unified_pool(str(config))
                self.assertIn('claude',pool.adapters)
                option=next(o for o in pool.options() if o['type']=='claude')
                self.assertEqual(Path(option['config']['stateDir']),root/'v2')
                self.assertEqual(json.loads(config.read_text())['adaptors'][0]['stateDir'],'v1')
                self.assertEqual(old.state_dir,root/'v1')
            finally:
                if pool: await pool.close()
                await old.close()


class ProjectCreateHttpTests(unittest.IsolatedAsyncioTestCase):
    async def test_websocket_create_refresh_and_read_without_billing(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            server=HarnessServer(fake=True,browse_only=True)
            adapter=FakeAdapter(cwd=directory)
            adapter.send_turn=AsyncMock(side_effect=AssertionError('no model calls during create'))
            server.assistant=ManagedAssistant(adapter=adapter,project_dir=root,skill_path=root/'unused',data_dir=root/'state',emit=server.broadcast)
            # Explicit test port avoids overriding a wildcard 8050 listener on
            # Windows (a loopback bind may otherwise coexist with that service).
            port = 8056
            while True:
                client = TestClient(TestServer(build_app(server), port=port))
                try:
                    await client.start_server()
                    break
                except OSError:
                    await client.close()
                    port += 1
                    if port > 8100:
                        raise
            try:
                ws=await client.ws_connect('/ws')
                await ws.send_json({'type':'sessions.list','requestId':'list'})
                while True:
                    event=await ws.receive_json(timeout=5)
                    if event['type']=='sessions.snapshot': break
                ref=next(r for r in event['payload']['sessions'] if r['id']=='demo-workspace')
                await ws.send_json({'type':'session.create','referenceId':ref['id'],'projectKey':ref['projectKey'],'requestId':'create'})
                event=await ws.receive_json(timeout=5)
                self.assertEqual(event['type'],'session.created')
                created=event['payload']['session']
                await ws.send_json({'type':'session.read','threadId':created['id'],'requestId':'read'})
                history=await ws.receive_json(timeout=5)
                self.assertEqual(history['payload']['messages'],[])
                await ws.send_json({'type':'sessions.list','requestId':'refresh'})
                refreshed=await ws.receive_json(timeout=5)
                self.assertEqual(sum(r['id']==created['id'] for r in refreshed['payload']['sessions']),1)
                adapter.send_turn.assert_not_awaited()
                await ws.close()
            finally: await client.close()

if __name__=='__main__': unittest.main()
