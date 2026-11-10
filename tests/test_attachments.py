import asyncio
import base64
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from aiohttp import FormData
from aiohttp.test_utils import TestClient, TestServer
from app.attachments import AttachmentStore, attachment_prompt
from app.gateway.server import HarnessServer, build_app
from app.orchestrator.session_manager import ManagedAssistant
from app.adapters.fake import FakeAdapter

PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVQIHWP4z8DwHwAFgAI/ScLbtAAAAABJRU5ErkJggg==')


class AttachmentStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name); self.store=AttachmentStore(self.root/'uploads')

    def test_opaque_id_paths_names_and_scope_are_validated(self):
        a=self.store.save(b'file','../../metadata.json',scope='codex',thread_id='A')
        self.assertEqual(a.name,'metadata.json'); self.assertEqual(self.store.get(a.id).path.read_bytes(),b'file')
        with self.assertRaises(ValueError): self.store.get('../secrets')
        with self.assertRaises(ValueError): self.store.resolve([a.id],scope='codex',thread_id='B')
        with self.assertRaises(ValueError): self.store.resolve([a.id,a.id],scope='codex',thread_id='A')

    def test_svg_is_download_only_and_invalid_image_is_rejected(self):
        a=self.store.save(b'<svg onload="evil()"/>','evil.svg',scope='codex',thread_id='A')
        self.assertEqual(a.kind,'file'); self.assertNotIn('previewUrl',a.public())
        with self.assertRaises(ValueError): self.store.save(b'not png','image.png',scope='codex',thread_id='A')

    def test_image_only_history_and_manifests_round_trip(self):
        a=self.store.save(PNG,'one.png',scope='codex',thread_id='A')
        result=self.store.decorate({'messages':[{'role':'user','text':attachment_prompt('',[a]).strip(),'images':[{'url':'data:image/png;base64,'+base64.b64encode(PNG).decode()}]}]},scope='codex',thread_id='A')
        self.assertEqual(result['messages'][0]['text'],'')
        self.assertEqual(result['messages'][0]['attachments'],[a.public()])

    def test_markdown_local_images_stay_inside_workspace_and_remote_not_fetched(self):
        (self.root/'good.png').write_bytes(PNG)
        result=self.store.decorate({'cwd':str(self.root),'messages':[{'role':'assistant','text':'![image](good.png)\n![remote](https://example.test/x.png)\n```\n![code](good.png)\n```'}]},scope='codex',thread_id='A')
        items=result['messages'][0]['attachments']
        self.assertIn('previewUrl',items[0]); self.assertEqual(items[1]['externalUrl'],'https://example.test/x.png')
        self.assertIn('![code]',result['messages'][0]['text'])


class AttachmentHttpTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        root=Path(self.temp.name)
        self.server=HarnessServer(fake=True,browse_only=True)
        self.server.assistant=ManagedAssistant(adapter=FakeAdapter(cwd=str(root)),project_dir=root,skill_path=root/'unused',data_dir=root/'state',emit=self.server.broadcast)
        self.server.attachments=AttachmentStore(root/'uploads')
        self.client=TestClient(TestServer(build_app(self.server)))
        await self.client.start_server(); self.addAsyncCleanup(self.client.close)

    async def test_upload_preview_and_attachment_only_send(self):
        form=FormData(); form.add_field('files',PNG,filename='tiny.png',content_type='image/png')
        response=await self.client.post('/api/attachments?threadId=demo-workspace',data=form,headers={'X-Harness-Upload':'1'})
        self.assertEqual(response.status,201); attachment=(await response.json())['attachments'][0]
        preview=await self.client.get(attachment['previewUrl']); self.assertEqual(preview.status,200)
        self.assertEqual(await preview.read(),PNG); self.assertEqual(preview.headers['X-Content-Type-Options'],'nosniff')
        ws=AsyncMock()
        await self.server.handle_command(ws,{'type':'session.send','threadId':'demo-workspace','text':'','clientMessageId':'attachment-only','attachmentIds':[attachment['id']]})
        await asyncio.gather(*self.server.command_tasks)
        self.assertEqual(self.server.assistant._records['attachment-only'].status,'completed')
        await self.server.handle_command(ws,{'type':'session.read','threadId':'demo-workspace','limit':30})
        rows=ws.send_json.call_args.args[0]['payload']['messages']
        self.assertEqual(next(r for r in rows if r.get('attachments'))['attachments'][0]['id'],attachment['id'])

    async def test_cross_origin_upload_and_wrong_session_are_rejected(self):
        form=FormData(); form.add_field('files',b'hello',filename='notes.txt')
        response=await self.client.post('/api/attachments?threadId=demo-workspace',data=form,headers={'X-Harness-Upload':'1','Origin':'https://attacker.test'})
        self.assertEqual(response.status,403)
        a=self.server.attachments.save(b'hello','notes.txt',scope=self.server.attachment_scope(),thread_id='demo-workspace')
        ws=AsyncMock()
        await self.server.handle_command(ws,{'type':'session.send','threadId':'demo-filters','text':'hello','clientMessageId':'bad','attachmentIds':[a.id]})
        self.assertEqual(ws.send_json.call_args.args[0]['type'],'command.error')
        self.assertNotIn('bad',self.server.assistant._records)

    async def test_download_non_image_never_uses_inline_content_type(self):
        a=self.server.attachments.save(b'<html><script>evil</script></html>','page.html',scope='scope',thread_id='A')
        response=await self.client.get(a.public()['url'])
        self.assertEqual(response.content_type,'application/octet-stream')
        self.assertTrue(response.headers['Content-Disposition'].startswith('attachment;'))
        response=await self.client.get('/api/attachments/'+a.id+'/preview'); self.assertEqual(response.status,404)

if __name__=='__main__': unittest.main()
