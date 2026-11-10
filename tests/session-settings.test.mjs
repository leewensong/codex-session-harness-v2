import assert from 'node:assert/strict';
import test from 'node:test';
import { effortsFor, describeConfiguration } from '../app/web/session-settings.mjs';
import { localAttachmentUrl, formatBytes } from '../app/web/attachment-ui.mjs';
test('model-specific efforts do not offer another models unsupported levels',()=>{
 const catalog={efforts:['low','high','max'],models:[{model:'A',supportedReasoningEfforts:['low']},{model:'B',supportedReasoningEfforts:[]}]};
 assert.deepEqual(effortsFor(catalog,'A'),['low']); assert.deepEqual(effortsFor(catalog,'B'),[]); assert.deepEqual(effortsFor(catalog,'custom'),catalog.efforts);
});
test('unknown history is distinguished from pending overrides',()=>{
 assert.match(describeConfiguration({observed:{model:'actual',reasoningEffort:null},overrides:{model:'next'},effective:{model:'next',reasoningEffort:'high'}}),/actual.*思考强度未知.*下一轮：next/);
});
test('attachments reject executable URLs and arbitrary paths',()=>{
 assert.equal(localAttachmentUrl('javascript:alert(1)','preview'),null);
 assert.equal(localAttachmentUrl('/api/attachments/../../config/preview','preview'),null);
 assert.equal(localAttachmentUrl('/api/attachments/'+'a'.repeat(32)+'/preview','preview'),'/api/attachments/'+'a'.repeat(32)+'/preview');
 assert.equal(formatBytes(1048576),'1.0 MB');
});
