const input = document.querySelector('#origin');
const status = document.querySelector('#status');
const button = document.querySelector('button');

Promise.all([
  chrome.storage.local.get('consoleOrigin'),
  fetch(chrome.runtime.getURL('defaults.json')).then(response => response.json()).catch(() => ({})),
]).then(([{ consoleOrigin }, defaults]) => {
  if (!input.value) input.value = consoleOrigin || defaults.origin || '';
});
chrome.storage.session.get(['chartTabId','focusChartTabId']).then(({ chartTabId,focusChartTabId }) => {
  status.textContent = '八格：'+(Number.isInteger(chartTabId)?'已绑定':'未绑定')+'；六格：'+(Number.isInteger(focusChartTabId)?'已绑定':'未绑定');
});

document.querySelector('form').addEventListener('submit', async event => {
  event.preventDefault();
  button.disabled = true;
  try {
    const address = new URL(input.value);
    if (!['http:', 'https:'].includes(address.protocol) || address.username || address.password) {
      throw new Error('请输入有效的 HTTP 或 HTTPS 控制台地址。');
    }
    const match = `${address.protocol}//${address.hostname}/*`;
    if (!await chrome.permissions.request({ origins: [match] })) {
      throw new Error('需要允许扩展访问这个控制台地址。');
    }
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    const result = await chrome.runtime.sendMessage({
      type: 'bind-chart', origin: address.origin, tabId: tab?.id, layout:document.querySelector('#layout').value,
    });
    if (!result?.ok) throw new Error(result?.reason || '绑定失败。');
    status.textContent = `已绑定 ${result.shown}。返回 AutoTrade 点选标的即可切图。`;
  } catch (error) {
    status.textContent = error.message;
  } finally {
    button.disabled = false;
  }
});
