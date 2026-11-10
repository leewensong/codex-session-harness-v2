"""Opt-in real model test through the exact Harness registry/service boundary.
Run with --send to append ONE harmless marker to the specified user-owned session.
No auto retry, no provider switch, no tool actions.
"""
import argparse
import asyncio
import json
import tempfile
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from agent_adaptors.config import load_registry
from agent_adaptors.service import AdaptorService

async def run(args):
    cfg=json.loads(Path(args.config).read_text(encoding='utf-8-sig'))
    config_dir=Path(args.config).resolve().parent
    entry=next(e for e in cfg['adaptors'] if e['id']=='claude-local')
    with tempfile.TemporaryDirectory(prefix='harness-sdk-e2e-') as temporary:
        # Use identical factory/backend/model, isolate only the test writer lease.
        entry={**entry,'cwd':str((config_dir/entry['cwd']).resolve()),'stateDir':str(Path(temporary)/'state'),'tools':[]}
        test_cfg=Path(temporary)/'adaptors.json'
        test_cfg.write_text(json.dumps({'contractVersion':1,'adaptors':[entry]}),encoding='utf-8')
        registry=load_registry(test_cfg); service=AdaptorService(registry)
        sid=f'claude-local:{args.session}'
        try:
            before=await service.read_full_session(sid)
            resumed=await service.resume_session(sid)
            print(json.dumps({'phase':'before','backend':type(registry.get('claude-local')).__name__,
                              'writable':resumed['writable'],'cwd':resumed['cwd'],
                              'records':len(before['records']),'messages':len(before['messages']),
                              'complete':before['diagnostics']['complete']},ensure_ascii=False),flush=True)
            if not args.send:return
            prompt='Harness SDK 接入验收：不调用任何工具，不修改任何文件。计算 13 × 17，只回复 HARNESS_SDK_APPEND_20261008_OK= 后接计算结果。'
            start=await service.start_turn(sid,prompt,timeout=120)
            cursor=0
            while True:
                result=await service.poll(start['streamId'],cursor=cursor)
                cursor=result['nextCursor']
                if result['status'] not in {'starting','running'}:break
                await asyncio.sleep(.1)
            after=await service.read_full_session(sid)
            texts=[m.get('text','') for m in after['messages']]
            reply=result.get('reply','')
            report={'phase':'after','status':result['status'],'reply':reply,'error':result.get('error'),
                    'newRecords':len(after['records'])-len(before['records']),
                    'inputPersisted':prompt in texts,'replyPersisted':any('HARNESS_SDK_APPEND_20261008_OK=221' in t for t in texts),
                    'historyStartsAtBeginning':bool(texts and texts[0]==before['messages'][0]['text']),
                    'complete':after['diagnostics']['complete'],'chunksRead':cursor}
            print(json.dumps(report,ensure_ascii=False),flush=True)
            assert result['status']=='completed',report
            assert report['inputPersisted'] and report['replyPersisted'] and report['complete'],report
        finally:await service.close()

if __name__=='__main__':
    p=argparse.ArgumentParser()
    p.add_argument('--config',default=str(ROOT.parent/'codex-session-harness-v2/config/adaptors.json'))
    p.add_argument('--session',default='2ad0f3aa-a0cb-4e6c-9af0-85dd6d90c3f1')
    p.add_argument('--send',action='store_true')
    asyncio.run(run(p.parse_args()))
