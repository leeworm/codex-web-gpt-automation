// This changes only the current temporary conversation, never account settings.
export async function ensureTemporaryChatPersonalization(Runtime, logger = () => {}) {
  const result = await Runtime.evaluate({
    expression: `(${enableTemporaryPersonalization.toString()})()`,
    awaitPromise: true,
    returnByValue: true,
  });
  if (result.exceptionDetails || result.result?.value?.enabled !== true) {
    throw new Error(`Temporary chat personalization was not confirmed: ${result.exceptionDetails?.exception?.description || result.exceptionDetails?.text || 'missing confirmation'}`);
  }
  logger('[browser] Temporary chat personalization: enabled');
  return result.result.value;
}

export async function enableTemporaryPersonalization() {
  const visible = element => element.isConnected && element.getClientRects().length > 0;
  // ChatGPT localizes the Temporary Chat personalization labels.  Keep the
  // browser contract bounded to labels observed/supported by this repository,
  // while accepting both the older and current English non-personalized term.
  const labels = {
    enabled: new Set(['Personalized', '맞춤화', '개인화', '개인화됨']),
    disabled: new Set([
      'Unpersonalized',
      'Non-personalized',
      '맞춤화 안 함',
      '개인화 안 함',
      '개인화되지 않음',
    ]),
  };
  // The current Korean composer labels the popup trigger "개인화됨", while
  // its accessible name may describe the menu generically. Check both names.
  const texts = element => [element.getAttribute('aria-label'), element.innerText]
    .filter(value => typeof value === 'string')
    .map(value => value.trim());
  const rowText = element => (element.querySelector('.truncate')?.textContent || element.innerText.split('\n')[0] || '').trim();
  const buttons = kind => [...document.querySelectorAll('button[aria-haspopup="menu"]')].filter(element =>
    visible(element) && texts(element).some(value => labels[kind].has(value)));
  const temporary = () => location.origin === 'https://chatgpt.com' &&
    new URL(location.href).searchParams.get('temporary-chat') === 'true';
  const assertTemporary = () => { if (!temporary()) throw new Error('Expected an owned temporary ChatGPT conversation'); };
  const selectedRows = () => [...document.querySelectorAll('[role="menuitemradio"]')].filter(element =>
    visible(element) && labels.enabled.has(rowText(element)));
  assertTemporary();
  let enabledButtons = [];
  let disabledButtons = [];
  for (let attempt = 0; attempt < 100; attempt++) {
    assertTemporary();
    enabledButtons = buttons('enabled');
    disabledButtons = buttons('disabled');
    if (enabledButtons.length > 1 || disabledButtons.length > 1) {
      throw new Error('Temporary personalization control missing or ambiguous');
    }
    if (enabledButtons.length === 1 || disabledButtons.length === 1 || selectedRows().length > 0) break;
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  if (enabledButtons.length > 1 || disabledButtons.length > 1) throw new Error('Temporary personalization control missing or ambiguous');
  if (enabledButtons.length === 1 && disabledButtons.length === 0) return { enabled: true, changed: false };
  if (selectedRows().length === 0) {
    const triggers = disabledButtons;
    if (triggers.length !== 1 || enabledButtons.length !== 0) throw new Error('Temporary personalization control missing or ambiguous');
    triggers[0].click();
  }
  for (let attempt = 0; attempt < 40; attempt++) {
    assertTemporary();
    const rows = selectedRows();
    if (rows.length > 1) throw new Error('Ambiguous personalization option');
    if (rows.length === 1) {
      const menu = rows[0].closest('[role="menu"]');
      const other = [...(menu?.querySelectorAll('[role="menuitemradio"]') || [])].filter(element =>
        visible(element) && labels.disabled.has(rowText(element)));
      if (other.length !== 1) throw new Error('Not the temporary personalization menu');
      const checked = rows[0].getAttribute('aria-checked');
      if (checked !== 'true' && checked !== 'false') throw new Error('Unknown personalization state');
      if (checked === 'false') rows[0].click();
      for (let confirmation = 0; confirmation < 40; confirmation++) {
        assertTemporary();
        if (buttons('enabled').length === 1 && buttons('disabled').length === 0) {
          return { enabled: true, changed: checked === 'false' };
        }
        await new Promise(resolve => setTimeout(resolve, 100));
      }
      throw new Error('Personalization did not become enabled');
    }
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  throw new Error('Temporary personalization menu did not appear');
}
