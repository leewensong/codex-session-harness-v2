import assert from 'node:assert/strict';
import test from 'node:test';
import { randomFillSync } from 'node:crypto';
import { clientId } from '../app/web/browser-compat.mjs';
import { sessionTree, isProjectExpanded, projectRows } from '../app/web/session-tree.mjs';

const sessions = [
  {id:'one', name:'First session', project:'same', projectKey:'dir1', status:'idle', updatedAt:100},
  {id:'two', name:'Second session', project:'same', projectKey:'dir2', status:'running', updatedAt:200},
  {id:'three', name:'File preview', project:'same', projectKey:'dir1', status:'idle', updatedAt:300},
  {id:'four', name:'Other session', project:'other', projectKey:'dir3', status:'error', updatedAt:150},
];
test('plain LAN HTTP works without randomUUID using secure getRandomValues',()=>{
  const crypto = {getRandomValues: bytes => randomFillSync(bytes)};
  const ids = new Set(Array.from({length:100},()=>clientId(crypto)));
  assert.equal(ids.size,100);
  for(const id of ids) assert.match(id,/^[a-f0-9]{8}-[a-f0-9]{4}-4[a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12}$/);
});
test('secure contexts retain native UUID while missing crypto fails explicitly',()=>{
  assert.equal(clientId({randomUUID:()=> 'native'}),'native');
  assert.throws(()=>clientId({}),/安全随机数/);
});
test('two-layer tree keeps same-named directories separate and sorts by recent activity',()=>{
  const groups = sessionTree(sessions);
  assert.deepEqual(groups.map(p=>p.key),['dir1','dir2','dir3']);
  assert.deepEqual(groups[0].sessions.map(s=>s.id),['three','one']);
  assert.notEqual(groups[0].displayLabel,groups[1].displayLabel);
  assert.deepEqual(sessions.map(s=>s.id),['one','two','three','four']);
});
test('search matches both project and session and hides unrelated groups',()=>{
  assert.deepEqual(sessionTree(sessions,{query:'PREVIEW'}).map(p=>p.key),['dir1']);
  assert.equal(sessionTree(sessions,{query:'same'}).length,2);
  assert.equal(sessionTree(sessions,{query:'no match'}).length,0);
  assert.deepEqual(sessionTree(sessions,{status:'running'}).map(p=>p.key),['dir2']);
});
test('search forces expansion without overwriting remembered collapse state',()=>{
  const expanded = {dir1:false}; const project = {key:'dir1'};
  assert.equal(isProjectExpanded(project,expanded),false);
  assert.equal(isProjectExpanded(project,expanded,{query:'file'}),true);
  assert.equal(isProjectExpanded(project,expanded,{status:'idle'}),true);
  assert.equal(expanded.dir1,false);
});
test('limited project rows always retain the current selection',()=>{
  const project = {sessions:Array.from({length:20},(_,i)=>({id:String(i)}))};
  const rows=projectRows(project,'19',5);
  assert.equal(rows.length,6); assert.equal(rows.at(-1).id,'19');
  assert.equal(projectRows(project,'1',5).length,5);
});
