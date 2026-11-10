"""Hermetic SDK tests: never call a model unless explicit e2e script is used."""
import asyncio
import json
import tempfile
import types
import unittest
from pathlib import Path

from agent_adaptors.claude_sdk import ClaudeSdkAdaptor
from agent_adaptors.contracts import AdaptorError
from agent_adaptors.config import load_registry
from agent_adaptors.history import project_dir_name
from agent_adaptors.registry import AdaptorRegistry
from agent_adaptors.service import AdaptorService

SID = '123e4567-e89b-12d3-a456-426614174000'

class ResultMessage:
    session_id = SID
    result = 'SDK_APPEND_OK'
    subtype = 'success'
    is_error = False

class FakeOptions:
    def __init__(self, **kwargs): self.kwargs = kwargs

class FakeClient:
    instances = []
    error = False
    slow = False
    def __init__(self, *, options):
        self.options=options; self.queries=[]; self.disconnected=False
        type(self).instances.append(self)
    async def connect(self): pass
    async def query(self, text): self.queries.append(text)
    async def disconnect(self): self.disconnected=True
    async def interrupt(self): pass
    async def receive_response(self):
        if self.slow: await asyncio.sleep(3)
        yield types.SimpleNamespace(session_id=SID, event={'type':'content_block_delta','delta':{'type':'text_delta','text':'SDK_APPEND_OK'}})
        r=ResultMessage(); r.is_error=self.error
        yield r

SDK=types.SimpleNamespace(ClaudeAgentOptions=FakeOptions, ClaudeSDKClient=FakeClient)

class SdkHarnessTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.root=Path(self.tmp.name)
        self.cwd=self.root/'real-project'; self.cwd.mkdir()
        self.projects=self.root/'projects'
        self.directory=self.projects/project_dir_name(str(self.cwd)); self.directory.mkdir(parents=True)
        self.path=self.directory/f'{SID}.jsonl'
        rows=[]
        for i in range(75):
            rows.append({'type':'user','sessionId':SID,'cwd':str(self.cwd),'uuid':f'u{i}','message':{'content':f'message-{i}'}})
        rows += [{'type':'assistant','sessionId':SID,'cwd':str(self.cwd),'uuid':'tool1','message':{'content':[{'type':'tool_use','name':'Write','input':{'path':'snake.html'}}]}},
                 {'type':'custom-title','sessionId':SID,'customTitle':'test'}]
        self.path.write_text('\n'.join(json.dumps(r) for r in rows)+'\n',encoding='utf-8')
        self.adaptor=ClaudeSdkAdaptor(cwd=str(self.root),state_dir=str(self.root/'state'),projects_dir=str(self.projects),
                                     sdk_module=SDK,allow_external_sessions=True,model='configured-model',tools=[])
        FakeClient.instances=[]; FakeClient.error=False; FakeClient.slow=False
    async def asyncTearDown(self):
        await self.adaptor.close(); self.tmp.cleanup()
    async def test_external_resume_uses_original_uuid_and_actual_cwd(self):
        chunks=[]
        async def delta(text):chunks.append(text)
        result=await self.adaptor.send_turn(SID,'append',on_delta=delta)
        c=FakeClient.instances[0]
        self.assertEqual(result['status'],'completed')
        self.assertEqual(c.options.kwargs['resume'],SID)
        self.assertNotIn('session_id',c.options.kwargs)
        self.assertEqual(c.options.kwargs['cwd'],str(self.cwd))
        self.assertEqual(c.queries,['append'])
        self.assertTrue(c.disconnected)
        self.assertEqual(chunks,['SDK_APPEND_OK'])
        self.assertNotIn(SID,self.adaptor.sessions)
    async def test_external_disabled_remains_read_only(self):
        self.adaptor.allow_external_sessions=False
        with self.assertRaises(AdaptorError):await self.adaptor.send_turn(SID,'append')
        self.assertFalse(FakeClient.instances)
    async def test_full_read_not_last_50_and_preserves_tool_and_metadata(self):
        row=await self.adaptor.read_full_session(SID)
        self.assertEqual(len(row['records']),77)
        self.assertEqual(len(row['messages']),76)
        self.assertEqual(row['messages'][0]['text'],'message-0')
        self.assertEqual(row['messages'][-1]['role'],'tool')
        self.assertEqual(row['records'][-1]['record']['type'],'custom-title')
        self.assertTrue(row['diagnostics']['complete'])
    async def test_provider_error_with_success_subtype_is_not_completed(self):
        FakeClient.error=True
        result=await self.adaptor.send_turn(SID,'append')
        self.assertEqual(result['status'],'error')
        self.assertEqual(len(FakeClient.instances),1)
        self.assertFalse(self.adaptor.active)
    async def test_timeout_disconnects_without_retry(self):
        FakeClient.slow=True
        result=await self.adaptor.send_turn(SID,'append',timeout=.02)
        self.assertEqual(result['status'],'cancelled')
        self.assertEqual(len(FakeClient.instances),1)
        self.assertTrue(FakeClient.instances[0].disconnected)
        self.assertFalse(self.adaptor.active)
    async def test_service_full_read_and_stream_use_existing_namespace(self):
        registry=AdaptorRegistry(); registry.register('claude-local',self.adaptor)
        service=AdaptorService(registry)
        sid=f'claude-local:{SID}'
        row=await service.read_full_session(sid)
        self.assertEqual(row['id'],sid)
        result=await service.send_turn(sid,'append')
        self.assertEqual(result['status'],'completed')
        self.assertEqual(result['chunks'][0]['text'],'SDK_APPEND_OK')
    async def test_newly_appended_records_visible_in_next_full_read(self):
        with self.path.open('a',encoding='utf-8') as f:
            f.write(json.dumps({'type':'assistant','sessionId':SID,'uuid':'new','message':{'content':'NEW_REPLY'}})+'\n')
        row=await self.adaptor.read_full_session(SID)
        self.assertEqual(row['messages'][-1]['text'],'NEW_REPLY')
    async def test_bad_record_does_not_claim_complete(self):
        with self.path.open('a',encoding='utf-8') as f:f.write('{broken\n')
        row=await self.adaptor.read_full_session(SID)
        self.assertFalse(row['diagnostics']['complete'])
        self.assertEqual(row['diagnostics']['parseWarnings'],1)
    async def test_config_factory_selects_sdk_backend(self):
        cfg=self.root/'adaptors.json'
        cfg.write_text(json.dumps({'contractVersion':1,'adaptors':[{'id':'sdk','type':'claude-code','cwd':str(self.root),'stateDir':str(self.root/'other-state'),'backend':'sdk','allowExternalSessions':True}]}),encoding='utf-8')
        registry=load_registry(cfg)
        try:self.assertIsInstance(registry.get('sdk'),ClaudeSdkAdaptor)
        finally:await registry.close()

if __name__=='__main__':unittest.main()
