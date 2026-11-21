const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const source = file => fs.readFileSync(path.join(__dirname, '..', file), 'utf8');
const worker = source('extensions/autotrade-tv/worker.js');
const bridge = source('extensions/autotrade-tv/console-bridge.js');
const {inspectTradingViewGrid,markTradingViewEntries}=require('../extensions/autotrade-tv/chart-grid');
const application = source('console/static/app.js');
const origin = 'http://192.0.2.10:8000';
const chartUrl = 'https://cn.tradingview.com/chart/test/';
const initialSymbol = 'NASDAQ:QQQ';

class LocalEvent extends Event {
  constructor(type, { detail }) {
    super(type);
    this.detail = detail;
  }
}

function client(tabId, name, { canSwitch = true, installed = true, injected = true } = {}) {
  const consoleWindow = new EventTarget();
  consoleWindow.chrome = {};
  consoleWindow.crypto = crypto.webcrypto;
  const downloads = [];
  const localStorage = new Map();
  const document = {
    documentElement: { dataset: {} }, body: { append() {} },
    createElement: () => ({ click() { downloads.push(this.href); }, remove() {} }),
  };
  const snapshots = {};
  const session = {};
  const local = {};
  const runtimeUrl = 'chrome-extension://test/popup.html';
  let handler;
  const consoleTab = { id: 100, windowId: 1, url: origin + '/' };
  const otherOrigin = { id: 101, windowId: 1, url: 'http://192.0.2.10:9000/' };
  const primary = { id: tabId, windowId: 1, url: chartUrl, symbol: initialSymbol };
  const tabs = new Map([consoleTab, otherOrigin, primary].map(tab => [tab.id, tab]));
  let attachCount = 0;
  let latestToast;
  const bindings = new Set();
  const runtime = {
    getURL: () => runtimeUrl,
    onMessage: { addListener(listener) { handler = listener; } },
    sendMessage(message) { return deliver(message, { frameId: 0, origin, tab: { ...consoleTab } }); },
  };
  function deliver(message, sender) {
    return new Promise(resolve => {
      if (!handler(message, sender, resolve)) resolve(undefined);
    });
  }
  const storage = values => ({
    async get() { return { ...values }; },
    async set(update) { Object.assign(values, update); },
  });
  const chrome = {
    runtime,
    tabs: {
      async get(requested) {
        const tab = tabs.get(requested);
        if (!tab) throw new Error('tab not found');
        return { ...tab };
      },
      async query({ windowId }) {
        return Array.from(tabs.values()).filter(tab => windowId === undefined || tab.windowId === windowId);
      },
    },
    storage: { local: storage(local), session: storage(session) },
    scripting: {
      async getRegisteredContentScripts() { return []; },
      async registerContentScripts([script]) { snapshots.bridge = script; },
      async executeScript({ target }) {
        assert.equal(target.tabId, 100);
        vm.runInContext(bridge, bridgeContext);
      },
    },
    debugger: {
      async attach(target) {
        assert.ok(tabs.get(target.tabId)?.symbol);
        assert.equal(bindings.size, 0);
        bindings.add(target.tabId);
        attachCount += 1;
      },
      async sendCommand(target, method, { expression }) {
        assert.equal(method, 'Runtime.evaluate');
        assert.ok(bindings.has(target.tabId));
        const tab = tabs.get(target.tabId);
        tab.grid ||= ['CAPITALCOM:NAS100','CAPITALCOM:SPX500','NASDAQ:MSFT','NASDAQ:AAPL','NASDAQ:TSLA',tab.symbol];
        const columns=tab.columns||(tab.grid.length===8?4:2);
        const charts=tab.grid.map((_,i)=>({
          symbol:()=>tab.grid[i],
          _mainDiv:{getBoundingClientRect:()=>({top:Math.floor(i/columns)*200,left:i%columns*400,width:390,height:190})},
          setSymbol:requested=>{if(canSwitch)setTimeout(()=>{tab.grid[i]=requested;if(i===5)tab.symbol=requested;},5);},
        }));
        const result = await vm.runInNewContext(expression, {
          window: { TradingViewApi: { _chartWidgetCollection: { getAll: () => charts } } },
          location: new URL(tab.url),
          setTimeout: callback => setTimeout(callback, 10),
        });
        return { result: { value: result } };
      },
      async detach(target) { bindings.delete(target.tabId); },
    },
  };
  const bridgeContext = vm.createContext({ chrome, document, window: consoleWindow, Event, CustomEvent: LocalEvent });
  vm.runInNewContext(worker, { chrome, URL, inspectTradingViewGrid, markTradingViewEntries, clearTimeout, setTimeout: callback => setTimeout(callback, 10) });
  if (injected) vm.runInContext(bridge, bridgeContext);
  let options;
  vm.runInNewContext(application, {
    Vue: { createApp(value) { options = value; return { use: () => ({ mount() {} }) }; } },
    ElementPlus: {}, document, window: consoleWindow, CustomEvent: LocalEvent, setTimeout, clearTimeout,
    localStorage: { getItem: key => localStorage.get(key), setItem: (key, value) => localStorage.set(key, value) },
    async fetch(url, options) {
      if (url === '/api/tv/view') {
        const body = JSON.parse(options.body);
        assert.match(body.source_token, /^[a-f0-9]{32}$/);
        assert.equal(document.documentElement.dataset.autotradeTvViewToken, body.source_token);
        snapshots.view = body;
        return { ok: true, json: async () => ({ ok: true, shown: body.symbol }) };
      }
      const key = JSON.parse(source('extensions/autotrade-tv/manifest.json')).key;
      const hex = crypto.createHash('sha256').update(Buffer.from(key, 'base64')).digest('hex').slice(0, 32);
      const id = [...hex].map(digit => String.fromCharCode(97 + parseInt(digit, 16))).join('');
      assert.equal(url, `chrome-extension://${id}/presence.json`);
      return { ok: installed, json: async () => ({ product: 'autotrade-tv' }) };
    },
  });
  const ui = {
    ...options.methods, identity: 'ok', traderName: name, tvSwitchRequest: 0,
    tvExtensionState: 'ready', status: {tv_view_same_window: true,tv_single_chart_cell:6}, tradeHeaders: () => ({}),
    $message: {
      success(message) { latestToast = message; },
      warning(message) { latestToast = message; },
    },
  };
  return {
    bind: (layout='candidates', target=tabId) => deliver({ type: 'bind-chart', origin, tabId:target,layout }, { url: runtimeUrl }),
    ui, deliver, snapshots, downloads, tabs, consoleTab, dataset: document.documentElement.dataset,
    install() { installed = true; },
    get symbol() { return primary.symbol; },
    installGrid(symbols){primary.grid=[...symbols];},
    get grid(){return primary.grid;},
    get attachCount() { return attachCount; },
    get attached() { return bindings.size; },
    get toast() { return latestToast; },
    navigate(url) { primary.url = url; },
  };
}

test('two LAN consoles simultaneously switch and verify their own TradingView charts', async () => {
  const host = client(1, 'master');
  const alice = client(7, 'alice');
  const bob = client(7, 'bob'); // separate Chrome clients can use the same local tab ID.
  assert.equal((await alice.bind()).shown, initialSymbol);
  assert.equal((await bob.bind()).shown, initialSymbol);
  assert.equal(alice.snapshots.bridge.matches[0], 'http://192.0.2.10/*');

  await Promise.all([alice.ui.tvShow('NASDAQ:NVDA'), bob.ui.tvShow('NASDAQ:AMD')]);
  assert.equal(alice.symbol, 'NASDAQ:NVDA');
  assert.equal(bob.symbol, 'NASDAQ:AMD');
  assert.match(alice.toast, /已切换到 NASDAQ:NVDA/);
  assert.match(bob.toast, /已切换到 NASDAQ:AMD/);
  assert.equal(host.symbol, initialSymbol);
  assert.equal(host.attachCount, 0);
  assert.equal(alice.attached + bob.attached, 0);
  assert.deepEqual(alice.grid.slice(0,5),['CAPITALCOM:NAS100','CAPITALCOM:SPX500','NASDAQ:MSFT','NASDAQ:AAPL','NASDAQ:TSLA']);
  assert.deepEqual(bob.grid.slice(0,5),alice.grid.slice(0,5));
});

test('the extension leaves an unbound or navigated tab untouched', async () => {
  const alice = client(7, 'alice');
  await alice.ui.tvShow('NASDAQ:AMD');
  assert.match(alice.toast, /先绑定/);
  assert.equal(alice.attachCount, 0);

  assert.equal((await alice.bind()).ok, true);
  alice.navigate('https://cn.tradingview.com/chart/other/');
  await alice.ui.tvShow('NASDAQ:AMD');
  assert.match(alice.toast, /重新绑定/);
  assert.equal(alice.symbol, initialSymbol);
  assert.equal(alice.attachCount, 1);
});

test('a tab from a different console origin is outside its binding', async () => {
  const alice = client(7, 'alice');
  await alice.bind();
  const result = await alice.deliver({ type: 'switch-symbol', symbol: 'NASDAQ:AMD' },
    { frameId: 0, origin: 'http://192.0.2.10:9000' });
  assert.equal(result.ok, false);
  assert.equal(alice.attachCount, 1);
});

test('a host click identifies its originating tab and refuses an old server', async () => {
  const host = client(1, 'master');
  host.ui.status = {};
  await host.ui.tvShow('NASDAQ:AMD');
  assert.equal(host.snapshots.view, undefined);
  host.ui.status = {tv_view_same_window: true};
  await host.ui.tvShow('NASDAQ:AMD');
  assert.equal(host.snapshots.view, undefined); // An old backend would still write cell 1.
  host.ui.status = {tv_view_same_window: true,tv_single_chart_cell:6};
  await host.ui.tvShow('NASDAQ:AMD');
  assert.equal(host.snapshots.view.symbol, 'NASDAQ:AMD');
  assert.equal(host.dataset.autotradeTvViewToken, undefined);
});

test('one extension routes simultaneous switches to their own windows and prefers the split partner', async () => {
  const alice = client(7, 'alice');
  await alice.bind();
  const otherConsole = {id: 201, windowId: 2, url: origin + '/', splitViewId: 4};
  const partner = {id: 202, windowId: 2, url: chartUrl, symbol: initialSymbol, splitViewId: 4};
  const otherChart = {id: 203, windowId: 2, url: chartUrl, symbol: initialSymbol};
  for (const tab of [otherConsole, partner, otherChart]) alice.tabs.set(tab.id, tab);
  const send = (tab, symbol) => alice.deliver({ type: 'switch-symbol', symbol }, { frameId: 0, origin, tab });
  const results = await Promise.all([send(alice.consoleTab, 'NASDAQ:NVDA'), send(otherConsole, 'NASDAQ:AMD')]);
  assert.deepEqual(results.map(result => result.shown), ['NASDAQ:NVDA', 'NASDAQ:AMD']);
  assert.equal(partner.symbol, 'NASDAQ:AMD');
  assert.equal(otherChart.symbol, initialSymbol);
  assert.equal(alice.symbol, 'NASDAQ:NVDA');

  partner.windowId = 3;
  otherChart.windowId = 3;
  const failed = await send(otherConsole, 'NASDAQ:AAPL');
  assert.match(failed.reason, /当前 AutoTrade 的 Chrome 窗口/);
  assert.equal(alice.symbol, 'NASDAQ:NVDA');
  assert.equal(partner.symbol, 'NASDAQ:AMD');
});

test('the console reports failure when TradingView does not confirm the requested symbol', async () => {
  const alice = client(7, 'alice', { canSwitch: false });
  await alice.bind();
  await alice.ui.tvShow('NASDAQ:AMD');
  assert.match(alice.toast, /未确认切换/);
  assert.equal(alice.symbol, initialSymbol);
  assert.equal(alice.attached, 0);
});

test('a new client downloads once, then detects installation and binding', async () => {
  const alice = client(7, 'alice', { installed: false, injected: false });
  alice.ui.tvExtensionState = 'checking';
  await alice.ui.checkTvExtension();
  assert.equal(alice.ui.tvExtensionState, 'missing');
  assert.equal(alice.ui.tvSetupVisible, true);
  assert.deepEqual(alice.downloads, ['/api/tv/extension.zip']);
  alice.ui.tvSetupPrompted = false; // a subsequent page load in this browser
  await alice.ui.checkTvExtension();
  assert.equal(alice.downloads.length, 1);

  alice.install();
  await alice.ui.checkTvExtension();
  assert.equal(alice.ui.tvExtensionState, 'bind');
  await alice.bind();
  await alice.ui.checkTvExtension();
  assert.equal(alice.ui.tvExtensionState, 'ready');
  assert.equal(alice.ui.tvSetupVisible, false);
});

test('the host console is never prompted to install', async () => {
  const host = client(1, 'master', { installed: false, injected: false });
  await host.ui.checkTvExtension();
  assert.deepEqual(host.downloads, []);
  assert.equal(host.ui.tvSetupVisible, undefined);
});

test('an older extension must upgrade instead of switching the first cell',async()=>{
 const alice=client(7,'alice');await alice.bind();
 const before=[...alice.grid],attached=alice.attachCount;
 alice.dataset.autotradeTvBridge='7';
 await alice.ui.tvShow('NASDAQ:NVDA');
 assert.equal(alice.ui.tvExtensionState,'update');assert.equal(alice.ui.tvSetupVisible,true);
 assert.match(alice.ui.tvExtensionReason,/0\.8\.0/);
 assert.deepEqual(alice.grid,before);assert.equal(alice.attachCount,attached);
});

test('two bound clients mirror eight cells independently and never address the host chart',async()=>{
  const alice=client(7,'alice'),bob=client(7,'bob'),host=client(8,'master');
  await alice.bind();await bob.bind();
  const initial=['NASDAQ:S9','NASDAQ:S7','NASDAQ:S5','NASDAQ:S3','NASDAQ:S10','NASDAQ:S8','NASDAQ:S6','NASDAQ:S4'];
  alice.installGrid(initial);bob.installGrid(initial);
  const a=[...initial],b=[...initial];a[5]='NYSE:UNH';b[2]='NASDAQ:CRWD';
  const result=await Promise.all([
    alice.ui.requestTvBridge({id:'ga',type:'switch-grid',symbols:a},1000),
    bob.ui.requestTvBridge({id:'gb',type:'switch-grid',symbols:b},1000)]);
  assert(result.every(r=>r.ok&&r.panes.length===8));
  assert.deepEqual(alice.grid,a);assert.deepEqual(bob.grid,b);
  assert.equal(host.attachCount,0);assert.equal(host.symbol,initialSymbol);
});

test('an explicitly bound console can use its separate chart window without granting another console that access',async()=>{
 const alice=client(7,'alice');await alice.bind();
 alice.tabs.get(7).windowId=2;
 await alice.ui.tvShow('NASDAQ:AMD');assert.equal(alice.symbol,'NASDAQ:AMD');
 const unbound={id:300,windowId:3,url:origin+'/'};alice.tabs.set(300,unbound);
 const result=await alice.deliver({type:'switch-symbol',symbol:'NASDAQ:NVDA'},{frameId:0,origin,tab:unbound});
 assert.equal(result.ok,false);assert.equal(alice.symbol,'NASDAQ:AMD');
});

test('two clients bind distinct six/eight windows and never fall back to another chart or console',async()=>{
 const alice=client(7,'alice'),bob=client(7,'bob');
 const ranked=[9,7,5,3,10,8,6,4].map(n=>'NASDAQ:S'+n);
 const focus=['NASDAQ:S1','CAPITALCOM:NAS100','CAPITALCOM:SPX500','NASDAQ:S2','NASDAQ:QQQ','AMEX:SPY'];
 for(const c of [alice,bob]) {
   c.installGrid(ranked);c.tabs.get(7).windowId=2;
   c.tabs.set(8,{id:8,windowId:3,url:'https://cn.tradingview.com/chart/focus/',symbol:'NASDAQ:S1',columns:3,grid:[...focus]});
   assert((await c.bind()).ok);assert((await c.bind('focus',8)).ok);
 }
 const a=[...focus];a[0]='NYSE:UNH';const b=[...focus];b[0]='NASDAQ:NVDA';
 const results=await Promise.all([alice.ui.requestTvBridge({id:'a',type:'switch-grid',layout:'focus',symbols:a},3000),
   bob.ui.requestTvBridge({id:'b',type:'switch-grid',layout:'focus',symbols:b},3000)]);
 assert(results.every(r=>r.ok));assert.deepEqual(alice.tabs.get(8).grid,a);assert.deepEqual(bob.tabs.get(8).grid,b);
 assert.deepEqual(alice.grid,ranked);assert.deepEqual(bob.grid,ranked);
 const unbound={id:300,windowId:4,url:origin+'/'};alice.tabs.set(300,unbound);
 const denied=await alice.deliver({type:'switch-grid',layout:'focus',symbols:b},{frameId:0,origin,tab:unbound});
 assert.equal(denied.ok,false);assert.deepEqual(alice.tabs.get(8).grid,a);
 alice.tabs.get(8).url='https://cn.tradingview.com/chart/other/';
 const stale=await alice.ui.requestTvBridge({id:'stale',type:'switch-grid',layout:'focus',symbols:b},3000);
 assert.equal(stale.ok,false);assert.deepEqual(alice.grid,ranked);
});
