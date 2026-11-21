const bridgeId = 'autotrade-tv-console';
if (typeof importScripts === 'function') importScripts('chart-grid.js');
const tradingViewHosts = new Set(['www.tradingview.com', 'cn.tradingview.com']);
let pendingSwitch = Promise.resolve();
const latestSwitchByWindow = new Map();

function chartAddress(raw) {
  const url = new URL(raw);
  if (url.protocol !== 'https:' || !tradingViewHosts.has(url.hostname)
      || !/^\/chart\/(?:[A-Za-z0-9_-]+\/?)?$/.test(url.pathname)) {
    throw new Error('请在要绑定的本机 TradingView 图表标签页打开扩展。');
  }
  return url.origin + url.pathname;
}

async function bindChart(message) {
  const address = new URL(message.origin);
  if (address.origin !== message.origin || !['http:', 'https:'].includes(address.protocol)) {
    throw new Error('控制台地址无效。');
  }
  const tab = await chrome.tabs.get(message.tabId);
  const chart = chartAddress(tab.url);
  const role=message.layout==='focus'?'focus':'candidates';
  const checked = await evaluateChart(tab.id, chart, null, null, false, false, role);
  if (!checked.ok) return checked;
  if(role==='focus'&&checked.pane_count!==6)return{ok:false,reason:'请在两行三列六宫格布局中绑定大盘窗口'};
  if(role==='candidates'&&![6,8].includes(checked.pane_count))return{ok:false,reason:'请在八宫格布局中绑定候选窗口'};
  const previous=await chrome.storage.session.get(['consoleOrigin','chartTabId','focusChartTabId']);
  const sameOrigin=previous.consoleOrigin===address.origin;
  const other=sameOrigin?(role==='focus'?previous.chartTabId:previous.focusChartTabId):null;
  if(other===tab.id)throw Error('八格和六格必须绑定不同的图表标签页');
  const matches = [`${address.protocol}//${address.hostname}/*`];
  const scripts = await chrome.scripting.getRegisteredContentScripts({ ids: [bridgeId] });
  if (scripts.length) {
    await chrome.scripting.updateContentScripts([{ id: bridgeId, matches }]);
  } else {
    await chrome.scripting.registerContentScripts([{
      id: bridgeId, matches, js: ['console-bridge.js'], runAt: 'document_idle',
    }]);
  }
  await chrome.storage.local.set({ consoleOrigin: address.origin });
  const tabs = await chrome.tabs.query({ url: matches }).catch(() => []);
  const consoles=tabs.filter(item=>item.url&&new URL(item.url).origin===address.origin);
  if(previous.consoleOrigin&&previous.consoleOrigin!==address.origin)
    await chrome.storage.session.set({chartTabId:null,chart:null,focusChartTabId:null,focusChart:null});
  await chrome.storage.session.set({[role==='focus'?'focusChartTabId':'chartTabId']:tab.id,
    [role==='focus'?'focusChart':'chart']:chart,consoleOrigin:address.origin,
    consoleTabId:consoles.length===1?consoles[0].id:null});
  await Promise.allSettled(consoles.map(item =>
    chrome.scripting.executeScript({ target: { tabId: item.id }, files: ['console-bridge.js'] }),
  ));
  return checked;
}

async function chartInConsoleWindow(sender, role = null) {
  if(role==='focus'||role==='candidates') {
    const binding=await chrome.storage.session.get(['chartTabId','chart','focusChartTabId','focusChart','consoleOrigin','consoleTabId']);
    const id=role==='focus'?binding.focusChartTabId:binding.chartTabId;
    const address=role==='focus'?binding.focusChart:binding.chart;
    if(!Number.isInteger(id)||!address)throw Error('请先绑定本机TV'+(role==='focus'?'六格':'八格')+'窗口');
    if(sender.frameId!==0||sender.origin!==binding.consoleOrigin||!Number.isInteger(sender.tab?.windowId))
      throw Error('当前控制台与本机图表绑定不符');
    const [source,tab]=await Promise.all([chrome.tabs.get(sender.tab.id),chrome.tabs.get(id)]);
    const across=source.windowId!==tab.windowId;
    if(source.windowId!==sender.tab.windowId||new URL(source.url).origin!==binding.consoleOrigin
      ||(across&&source.id!==binding.consoleTabId)||chartAddress(tab.url)!==address)
      throw Error('标签页归属或图表地址已变化，请重新绑定');
    return {tab,address,boundAcrossWindows:across,role};
  }
  const { chartTabId, chart, consoleOrigin, consoleTabId } = await chrome.storage.session.get([
    'chartTabId', 'chart', 'consoleOrigin', 'consoleTabId',
  ]);
  if (!Number.isInteger(chartTabId) || !chart) {
    throw new Error('请先绑定本机 TradingView 图表。');
  }
  if (sender.frameId !== 0 || sender.origin !== consoleOrigin || !Number.isInteger(sender.tab?.windowId)) {
    throw new Error('当前控制台地址或 Chrome 窗口与本机图表绑定不符。');
  }
  const windowId = sender.tab.windowId;
  const tabs = await chrome.tabs.query({ windowId });
  const consoleTab = tabs.find(tab => tab.id === sender.tab.id);
  if (!consoleTab || !consoleTab.url || new URL(consoleTab.url).origin !== consoleOrigin) {
    throw new Error('AutoTrade 标签页已离开原窗口，请重新点击标的。');
  }
  const charts = [];
  for (const tab of tabs) {
    let address;
    try { address = chartAddress(tab.url); } catch { continue; }
    if (tab.id === chartTabId && address !== chart) continue;
    charts.push({ tab, address });
  }
  const split = consoleTab.splitViewId;
  const partner = Number.isInteger(split) && split >= 0 && charts.find(({ tab }) => tab.splitViewId === split);
  if (partner) return partner;
  if (charts.length === 1) return charts[0];
  const bound = charts.find(({ tab }) => tab.id === chartTabId);
  if (bound) return bound;
  if (charts.length) throw new Error('当前窗口有多个 TradingView 图表，请将目标图表与 AutoTrade 分屏或绑定该图表。');
  if (tabs.some(tab => tab.id === chartTabId)) throw new Error('已绑定的 TradingView 图表不可用，请重新绑定。');
  if(sender.tab.id===consoleTabId) {
    const tab=await chrome.tabs.get(chartTabId);
    if(chartAddress(tab.url)!==chart)throw Error('已绑定的TV图表地址已变化，请重新绑定');
    return {tab,address:chart,boundAcrossWindows:true};
  }
  throw new Error('请在当前 AutoTrade 的 Chrome 窗口打开 TradingView 图表。');
}

async function switchSymbol(message, sender) {
  if(message.type==='mark-entries') {
    if(!Array.isArray(message.entries)||message.entries.length>200)return{ok:false,reason:'成交标记数量无效'};
    const {tab,address,boundAcrossWindows}=await chartInConsoleWindow(sender,message.layout||null);
    return evaluateChart(tab.id,address,message.entries,sender.tab,'markers',boundAcrossWindows,message.layout||null);
  }
  if(message.type==='switch-grid') {
    const symbols=message.symbols;
    if(!Array.isArray(symbols)||!(message.layout==='focus'?[6]:[6,8]).includes(symbols.length)||symbols.some(s=>typeof s!=='string'||!/^[A-Z0-9_.-]{1,24}:[A-Z0-9_.!+/-]{1,32}$/.test(s)))
      return {ok:false,reason:'需要与宫格数量一致的完整标的代码。'};
    const {tab,address,boundAcrossWindows}=await chartInConsoleWindow(sender,message.layout||null);
    return evaluateChart(tab.id,address,symbols,sender.tab,true,boundAcrossWindows,message.layout||null);
  }
  const symbol = typeof message.symbol === 'string' ? message.symbol.trim().toUpperCase() : '';
  if (!/^(?:[A-Z0-9_.-]{1,24}:)?[A-Z0-9_.!+/-]{1,32}$/.test(symbol)) {
    return { ok: false, reason: '标的代码无效。' };
  }
  const {tab, address, boundAcrossWindows} = await chartInConsoleWindow(sender);
  return evaluateChart(tab.id, address, symbol, sender.tab,false,boundAcrossWindows);
}

async function evaluateChart(tabId, chart, symbol, sourceTab = null, grid = false, boundAcrossWindows = false, role = null) {
  const target = { tabId };
  try {
    await chrome.debugger.attach(target, '1.3');
  } catch {
    return { ok: false, reason: '无法连接本机图表。请检查标签页和 Chrome 调试权限。' };
  }
  try {
    if (sourceTab) {
      const [source, targetTab] = await Promise.all([chrome.tabs.get(sourceTab.id), chrome.tabs.get(tabId)]);
      const binding=boundAcrossWindows?await chrome.storage.session.get(['chartTabId','chart','focusChartTabId','focusChart','consoleTabId','consoleOrigin']):null;
      const explicit=!!(binding&&binding[role==='focus'?'focusChartTabId':'chartTabId']===tabId&&binding[role==='focus'?'focusChart':'chart']===chart&&binding.consoleTabId===source.id
        &&new URL(source.url).origin===binding.consoleOrigin);
      if (source.windowId !== sourceTab.windowId || (targetTab.windowId !== sourceTab.windowId&&!explicit)) {
        return { ok: false, reason: '标签页归属已变化，请重新点击标的。' };
      }
    }
    const inspector=grid==='markers'?markTradingViewEntries:inspectTradingViewGrid;
    const expression = `(${inspector.toString()})(${JSON.stringify(symbol)}, ${JSON.stringify(chart)}`;
    let expected = null;
    for (let attempt = 0; attempt <= 15; attempt += 1) {
      let timer;
      const call=chrome.debugger.sendCommand(target, 'Runtime.evaluate', {
        expression: `${expression}, ${attempt === 0}, ${JSON.stringify(expected)}, ${JSON.stringify(role)})`,returnByValue:true,awaitPromise:true,
      });
      let response;
      try {
        response=grid==='markers'?await Promise.race([call,new Promise((_,reject)=>{
          timer=setTimeout(()=>reject(Error('原生成交标记仍在绘制；切到TV后可点同步重试。')),6500);
        })]):await call;
      }finally{if(timer)clearTimeout(timer);}
      const result = response.result?.value;
      if (!result) return { ok: false, reason: 'TradingView 未返回可核对的图表状态。' };
      if (!result.pending) return result;
      if (!grid && !expected) expected = result.panes.map(pane => pane.symbol);
      if (attempt === 15) {
        return { ok: false, reason: 'TradingView 未确认切换到目标标的。', shown: result.shown };
      }
      // Poll from the extension so inactive chart tabs do not throttle the timer.
      await new Promise(resolve => setTimeout(resolve, 200));
    }
  } finally {
    await chrome.debugger.detach(target).catch(() => {});
  }
}

async function chartStatus(sender) {
  const { consoleOrigin } = await chrome.storage.local.get('consoleOrigin');
  if (!consoleOrigin || sender.frameId !== 0 || sender.origin !== consoleOrigin) return { ok: false };
  try {
    await chartInConsoleWindow(sender);
    return { installed: true, ready: true };
  } catch (error) {
    return { installed: true, ready: false, reason: error.message };
  }
}

chrome.runtime.onMessage.addListener((message, sender, reply) => {
  if (message?.type === 'bind-chart' && sender.url === chrome.runtime.getURL('popup.html')) {
    bindChart(message).then(reply, error => reply({ ok: false, reason: error.message }));
    return true;
  }
  if (message?.type === 'chart-status') {
    chartStatus(sender).then(reply, () => reply({ ok: false }));
    return true;
  }
  if (!['switch-symbol','switch-grid','mark-entries'].includes(message?.type)) return false;
  const windowId = sender.tab?.windowId;
  const queueKey=windowId+':'+message.type+':'+(message.layout||'default');
  const request = {};
  latestSwitchByWindow.set(queueKey, request);
  const result = pendingSwitch.then(() => request === latestSwitchByWindow.get(queueKey)
    ? switchSymbol(message, sender)
    : { ok: false, reason: '已收到更新的切换请求。' });
  pendingSwitch = result.catch(() => {});
  result.then(reply, error => reply({ ok: false, reason: error.message || '无法读取本机 TradingView 图表，请重新绑定。' }))
    .finally(() => { if (latestSwitchByWindow.get(queueKey) === request) latestSwitchByWindow.delete(queueKey); });
  return true;
});
