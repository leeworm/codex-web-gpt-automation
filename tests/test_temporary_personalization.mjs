import test from 'node:test';
import assert from 'node:assert/strict';
import { enableTemporaryPersonalization, ensureTemporaryChatPersonalization } from '../bin/oracle_temporary_personalization.mjs';

function fixture({
  enabled = false,
  temporary = true,
  ambiguous = false,
  enabledLabel = 'Personalized',
  disabledLabel = 'Unpersonalized',
  triggerAriaLabel = null,
  decoy = false,
  triggerDelayMs = 0,
} = {}) {
  let opened = false;
  let clicks = 0;
  const triggerVisibleAt = Date.now() + triggerDelayMs;
  globalThis.location = { origin: 'https://chatgpt.com', href: `https://chatgpt.com/?temporary-chat=${temporary}` };
  const base = { isConnected: true, getClientRects: () => [1] };
  const trigger = {
    ...base,
    get innerText() { return enabled ? enabledLabel : disabledLabel; },
    getAttribute: name => name === 'aria-haspopup' ? 'menu' : name === 'aria-label' ? (triggerAriaLabel || (enabled ? enabledLabel : disabledLabel)) : null,
    click: () => { opened = true; clicks++; },
  };
  const unrelated = { ...base, innerText: enabledLabel, getAttribute: name => name === 'aria-label' ? '다른 메뉴' : null, click: () => { throw Error('must not click unrelated button'); } };
  const menu = { querySelectorAll: () => [yes, no] };
  const row = (label, selected, click) => ({ ...base, querySelector: () => ({ textContent: label }), getAttribute: () => String(selected()), closest: () => menu, click });
  const yes = row(enabledLabel, () => enabled, () => { enabled = true; opened = false; clicks++; });
  const no = row(disabledLabel, () => !enabled, () => { throw Error('must not disable'); });
  globalThis.document = {
    querySelectorAll: selector => {
      if (selector === 'button') return [...(ambiguous ? [trigger, trigger] : [trigger]), ...(decoy ? [unrelated] : [])];
      if (selector === 'button[aria-haspopup="menu"]') {
        if (Date.now() < triggerVisibleAt) return [];
        return ambiguous ? [trigger, trigger] : [trigger];
      }
      if (selector === '[role="menuitemradio"]') return opened ? [yes, no] : [];
      return [];
    },
  };
  return () => clicks;
}

test('enables only temporary personalization and is idempotent', async () => {
  const clicks = fixture();
  assert.deepEqual(await enableTemporaryPersonalization(), { enabled: true, changed: true });
  assert.equal(clicks(), 2);
  assert.deepEqual(await enableTemporaryPersonalization(), { enabled: true, changed: false });
  assert.equal(clicks(), 2);
});
test('supports the Korean temporary-chat personalization controls', async () => {
  const clicks = fixture({ enabledLabel: '맞춤화', disabledLabel: '맞춤화 안 함' });
  assert.deepEqual(await enableTemporaryPersonalization(), { enabled: true, changed: true });
  assert.equal(clicks(), 2);
  assert.deepEqual(await enableTemporaryPersonalization(), { enabled: true, changed: false });
  assert.equal(clicks(), 2);
});
test('supports the current English non-personalized label', async () => {
  const clicks = fixture({ disabledLabel: 'Non-personalized' });
  assert.deepEqual(await enableTemporaryPersonalization(), { enabled: true, changed: true });
  assert.equal(clicks(), 2);
});
test('recognizes the current Korean popup trigger and ignores a similarly named unrelated button', async () => {
  const clicks = fixture({ enabled: true, enabledLabel: '개인화됨', triggerAriaLabel: '대화 메뉴', decoy: true });
  assert.deepEqual(await enableTemporaryPersonalization(), { enabled: true, changed: false });
  assert.equal(clicks(), 0);
});
test('waits for the temporary personalization control to render', async () => {
  const clicks = fixture({ enabled: true, triggerDelayMs: 250 });
  assert.deepEqual(await enableTemporaryPersonalization(), { enabled: true, changed: false });
  assert.equal(clicks(), 0);
});
test('regular chat is rejected without clicks', async () => {
  const clicks = fixture({ temporary: false });
  await assert.rejects(enableTemporaryPersonalization, /temporary/);
  assert.equal(clicks(), 0);
});
test('ambiguous control is rejected without clicks', async () => {
  const clicks = fixture({ ambiguous: true });
  await assert.rejects(enableTemporaryPersonalization, /ambiguous/);
  assert.equal(clicks(), 0);
});
test('CDP exception cannot become successful confirmation', async () => {
  await assert.rejects(() => ensureTemporaryChatPersonalization({ evaluate: async () => ({ exceptionDetails: { text: 'failed' } }) }), /not confirmed/);
});
