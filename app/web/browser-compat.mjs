/** Request IDs must also work on plain LAN HTTP, where randomUUID is unavailable.
 * getRandomValues remains available outside secure contexts; never use Math.random.
 */
export function clientId(cryptoProvider = globalThis.crypto) {
  if (typeof cryptoProvider?.randomUUID === 'function') return cryptoProvider.randomUUID();
  if (typeof cryptoProvider?.getRandomValues !== 'function') throw new Error('浏览器不支持安全随机数，请使用现代浏览器');
  const bytes = cryptoProvider.getRandomValues(new Uint8Array(16));
  bytes[6] = (bytes[6] & 15) | 64;
  bytes[8] = (bytes[8] & 63) | 128;
  const hex = [...bytes].map(byte => byte.toString(16).padStart(2, '0')).join('');
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}

export async function copyPlainText(text) {
  try {
    if (globalThis.navigator?.clipboard?.writeText) {
      await navigator.clipboard.writeText(String(text));
      return;
    }
  } catch { /* LAN HTTP or denied Clipboard permission: use a selection copy. */ }
  const previous = document.activeElement;
  const field = document.createElement('textarea');
  field.value = String(text); field.setAttribute('readonly', ''); field.setAttribute('aria-hidden', 'true');
  field.style.cssText = 'position:fixed;left:-10000px;top:0;opacity:0';
  document.body.append(field); field.select();
  let copied = false;
  try { copied = document.execCommand('copy'); }
  finally { field.remove(); previous?.focus?.({ preventScroll: true }); }
  if (!copied) throw new Error('请手动选择并复制文本');
}
