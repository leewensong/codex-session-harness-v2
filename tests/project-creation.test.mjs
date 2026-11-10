import test from 'node:test';
import assert from 'node:assert/strict';
import { creationReference, sessionTree } from '../app/web/session-tree.mjs';

test('creation targets the actual directory even when project labels are identical', () => {
  const rows = [
    { id:'one', name:'session one', project:'app', projectKey:'dir1', canCreateSession:true },
    { id:'two', name:'session two', project:'app', projectKey:'dir2', canCreateSession:true },
  ];
  const groups=sessionTree(rows);
  assert.equal(creationReference(groups.find(p=>p.key==='dir1')).id,'one');
  assert.equal(creationReference(groups.find(p=>p.key==='dir2')).id,'two');
});
test('unsupported or unknown cwd never silently picks a wrong project', () => {
  assert.equal(creationReference({sessions:[{id:'missing'},{id:'off',canCreateSession:false}]}),null);
  assert.equal(creationReference({sessions:[{id:'missing'},{id:'supported',canCreateSession:true}]}).id,'supported');
});
