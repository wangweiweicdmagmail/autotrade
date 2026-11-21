if (!globalThis.autotradeTvBridge) {
  globalThis.autotradeTvBridge = true;
  document.documentElement.dataset.autotradeTvBridge = '8';

  window.addEventListener('autotrade:local-tv-request', async event => {
    const { id, type = 'switch-symbol', symbol, symbols, entries, layout } = event.detail || {};
    if (typeof id !== 'string' || !['switch-symbol', 'switch-grid', 'mark-entries', 'chart-status'].includes(type)) return;
    let result;
    try {
      result = await chrome.runtime.sendMessage({ type, symbol, symbols, entries, layout });
    } catch {
      result = { ok: false, reason: '本机扩展连接已失效，请刷新 AutoTrade 页面。' };
    }
    window.dispatchEvent(new CustomEvent('autotrade:local-tv-result', { detail: { id, ...result } }));
  });
  window.dispatchEvent(new Event('autotrade:tv-bridge-ready'));
}
