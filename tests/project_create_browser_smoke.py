"""Headless local Chromium smoke for project creation. Only the fake fixture server."""
import asyncio
import base64
import json
from pathlib import Path
from aiohttp import ClientSession


class CDP:
    def __init__(self, ws): self.ws=ws; self.counter=0; self.errors=[]
    async def call(self, method, params=None):
        self.counter+=1; rid=self.counter
        await self.ws.send_json({'id':rid,'method':method,'params':params or {}})
        while True:
            event=await self.ws.receive_json(timeout=15)
            if event.get('method')=='Runtime.exceptionThrown': self.errors.append(event['params'])
            if event.get('id')==rid:
                if 'error' in event: raise RuntimeError(event['error'])
                return event.get('result',{})
    async def js(self, expression):
        result=await self.call('Runtime.evaluate',{'expression':expression,'returnByValue':True,'awaitPromise':True})
        if result.get('exceptionDetails'): raise RuntimeError(result['exceptionDetails'])
        return result.get('result',{}).get('value')
    async def wait(self, expression):
        for _ in range(100):
            value=await self.js(expression)
            if value:return value
            await asyncio.sleep(.1)
        raise AssertionError('UI deadline: '+expression)


async def main():
    async with ClientSession() as client:
        tabs=await (await client.get('http://127.0.0.1:8054/json/list')).json()
        target=next(tab for tab in tabs if tab['type']=='page')
        async with client.ws_connect(target['webSocketDebuggerUrl']) as ws:
            cdp=CDP(ws)
            await cdp.call('Runtime.enable'); await cdp.call('Page.enable')
            await cdp.call('Emulation.setDeviceMetricsOverride',{'width':1280,'height':800,'deviceScaleFactor':1,'mobile':False})
            await cdp.call('Page.navigate',{'url':'http://127.0.0.1:8053/'})
            await cdp.wait("document.querySelectorAll('.project-create:not(:disabled)').length >= 2 && !document.getElementById('refresh').disabled")
            initial=await cdp.js("({groups:document.querySelectorAll('.tree-project').length, plus:document.querySelectorAll('.project-create').length, labels:[...document.querySelectorAll('.tree-project-heading')].map(e=>e.textContent), rows:document.getElementById('listSummary').textContent})")
            assert initial['groups']==initial['plus']
            groups=await cdp.js("[...document.querySelectorAll('.tree-project')].filter(e=>e.querySelector('.tree-project-name').textContent.startsWith('app')).map(e=>({key:e.dataset.projectKey,label:e.querySelector('.tree-project-name').textContent}))")
            assert len(groups)==2,groups
            key=groups[1]['key']; selector=json.dumps(f'.tree-project[data-project-key="{key}"]')
            await cdp.js(f"document.querySelector({selector}).querySelector('.tree-project-heading').click()")
            assert await cdp.js(f"document.querySelector({selector}).querySelector('.tree-project-heading').getAttribute('aria-expanded')")=='false'
            await cdp.js(f"document.querySelector({selector}).querySelector('.project-create').click(); document.querySelector({selector}).querySelector('.project-create').click()")
            await cdp.wait("document.getElementById('sessionTitle').textContent==='新会话' && !document.getElementById('messages').getAttribute('aria-busy').includes('true') && !document.getElementById('refresh').disabled")
            created=await cdp.js("({id:document.querySelector('.tree-session.selected').dataset.sessionId,group:document.querySelector('.tree-session.selected').closest('.tree-project').dataset.projectKey,expanded:document.querySelector('.tree-session.selected').closest('.tree-project').querySelector('.tree-project-heading').getAttribute('aria-expanded'),messages:document.querySelectorAll('#messages article').length,focus:document.activeElement.id,rows:document.getElementById('listSummary').textContent})")
            assert created['group']==key and created['expanded']=='true'
            assert created['messages']==0 and created['focus']=='input'
            assert created['rows']=='8 / 8 个会话',created
            await cdp.js("document.getElementById('refresh').click()")
            await cdp.wait("!document.getElementById('refresh').disabled && document.getElementById('listSummary').textContent==='8 / 8 个会话'")
            assert await cdp.js("document.querySelector('.tree-session.selected').dataset.sessionId")==created['id']
            await cdp.call('Page.reload')
            await cdp.wait("document.getElementById('sessionTitle')?.textContent==='新会话' && !document.getElementById('refresh').disabled")
            assert await cdp.js("document.querySelector('.tree-session.selected').dataset.sessionId")==created['id']
            await cdp.js("document.querySelector('.tree-session.selected').closest('.tree-project').querySelector('.project-create').click()")
            await cdp.wait("document.getElementById('listSummary').textContent==='9 / 9 个会话' && !document.getElementById('refresh').disabled")
            second=await cdp.js("document.querySelector('.tree-session.selected').dataset.sessionId")
            assert second!=created['id']
            # The first user message uses fake only, not any real provider.
            await cdp.js("document.getElementById('input').value='creation smoke';document.getElementById('input').dispatchEvent(new Event('input',{bubbles:true}));document.getElementById('send').click()")
            await cdp.wait("document.getElementById('messages').textContent.includes('Fake Codex 已收到：creation smoke') && document.getElementById('cancel').hidden")
            screenshot=await cdp.call('Page.captureScreenshot',{'format':'png'})
            Path('data/ui-fixtures/project-create-desktop.png').write_bytes(base64.b64decode(screenshot['data']))
            await cdp.call('Emulation.setDeviceMetricsOverride',{'width':390,'height':844,'deviceScaleFactor':1,'mobile':False})
            await cdp.js("document.getElementById('showProjects').click()")
            await cdp.wait("document.getElementById('workspace').classList.contains('projects-open')")
            mobile=await cdp.js("({width:document.documentElement.clientWidth,scrollWidth:document.body.scrollWidth,plus:document.querySelectorAll('.project-create:not(:disabled)').length})")
            assert mobile['width']==mobile['scrollWidth'] and mobile['plus']>=2
            assert not cdp.errors,cdp.errors
            print(json.dumps({'ok':True,'initial':initial,'created':created,'refreshAndReloadKeepSelection':True,'secondId':second,'fakeSend':True,'mobile':mobile,'consoleErrors':len(cdp.errors)},ensure_ascii=False))

if __name__=='__main__': asyncio.run(main())
