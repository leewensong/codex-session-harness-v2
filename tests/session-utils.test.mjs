import test from 'node:test';
import assert from 'node:assert/strict';
import { timestamp, statusInfo, visibleSessions, projectsFor, markdown } from '../app/web/session-utils.mjs';

const sessions = [
  { id: 'a', name: 'Desktop workspace', project: 'app', projectKey: 'dir1', status: 'idle', updatedAt: 1700000000 },
  { id: 'b', name: '搜索体验', preview: 'recent content', project: 'app', projectKey: 'dir2', status: 'inProgress', updatedAt: 1700000100000 },
  { id: 'c', name: '异常记录', project: 'server', projectKey: 'dir3', status: { type: 'error' }, updatedAt: '2026-10-08T00:00:00Z' },
];
test('timestamps normalize seconds, milliseconds and ISO', () => {
  assert.equal(timestamp(1700000000), timestamp(1700000000000));
  assert.equal(timestamp('2026-10-08T00:00:00Z'), Date.parse('2026-10-08T00:00:00Z'));
  assert.equal(timestamp('not a date'), 0);
});
test('real Codex status variants normalize', () => {
  assert.equal(statusInfo('inProgress').key, 'running');
  assert.equal(statusInfo('notLoaded').key, 'idle');
  assert.equal(statusInfo({ type: 'error' }).key, 'error');
  assert.equal(statusInfo(null).key, 'unknown');
});
test('same-named projects stay separate', () => {
  const projects = projectsFor(sessions);
  assert.equal(projects.length, 3);
  assert.notEqual(projects.find(p => p.key === 'dir1').displayLabel, projects.find(p => p.key === 'dir2').displayLabel);
});
test('project, query and status filters compose', () => {
  assert.deepEqual(visibleSessions(sessions, { project: 'dir2', query: 'RECENT', status: 'running' }).map(s => s.id), ['b']);
  assert.deepEqual(visibleSessions(sessions, { query: 'c' }).map(s => s.id), ['c', 'b', 'a']);
  assert.deepEqual(visibleSessions(sessions, { status: 'error' }).map(s => s.id), ['c']);
});
test('recent sorting handles mixed timestamps without mutating input', () => {
  assert.deepEqual(visibleSessions(sessions).map(s => s.id), ['c', 'b', 'a']);
  assert.equal(sessions[0].id, 'a');
});
test('markdown supports headings, lists, inline and fenced code', () => {
  const html = markdown('## Title\n\n- **bold** and `code`\n\n```js\nconst a = 1;\n```');
  assert.match(html, /<h3>Title<\/h3>/);
  assert.match(html, /<ul><li><strong>bold<\/strong>/);
  assert.match(html, /<code>code<\/code>/);
  assert.match(html, /<pre><code>const a = 1;/);
});
test('raw HTML and scripts remain escaped', () => {
  const html = markdown('<script>alert(1)</script>\n<img src=x onerror=alert(2)>');
  assert.ok(!html.includes('<script>'));
  assert.ok(!html.includes('<img'));
  assert.match(html, /&lt;script&gt;/);
});
test('only http and https absolute URLs become links', () => {
  assert.ok(!markdown('[bad](javascript:alert(1))').includes('<a '));
  assert.ok(!markdown('[bad](data:text/html,test)').includes('<a '));
  assert.ok(!markdown('[bad](file:///etc/passwd)').includes('<a '));
  assert.match(markdown('[docs](https://example.com/a?q=%22)'), /rel="noopener noreferrer"/);
});
test('code fences and labels cannot inject markup', () => {
  const html = markdown('```<img src=x>\n</code><script>evil()</script>\n```');
  assert.ok(!html.includes('<script>'));
  assert.ok(!html.includes('<img'));
  assert.match(html, /&lt;\/code&gt;/);
});
