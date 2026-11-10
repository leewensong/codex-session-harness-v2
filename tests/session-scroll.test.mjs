import assert from 'node:assert/strict';
import test from 'node:test';
import { readFileSync } from 'node:fs';

test('ready conversation history jumps immediately rather than smoothly scrolling', () => {
  const css = readFileSync(new URL('../app/web/workspace.css', import.meta.url), 'utf8');
  const messagesRule = css.match(/\.messages\s*\{([^}]+)\}/)?.[1];
  assert.ok(messagesRule, 'messages scroll container has an explicit rule');
  assert.match(messagesRule, /scroll-behavior:\s*auto\s*;/);
  assert.doesNotMatch(messagesRule, /scroll-behavior:\s*smooth/);
});
