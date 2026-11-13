const { readFileSync } = require('node:fs');
const { runInNewContext } = require('node:vm');
const { join } = require('node:path');
const test = require('node:test');
const assert = require('node:assert/strict');

const source = readFileSync(join(__dirname, '../controller/tv_paper_bridge.js'), 'utf8');
function mock() {
  let account = 'dedicated-paper';
  const calls = [];
  const events = ['orderUpdate', 'positionUpdate', 'executionUpdate'];
  const broker = {
    metainfo: () => ({ id: 'Paper' }),
    currentAccount: () => account,
    isConnected: () => true,
    positions: async () => [],
    orders: async () => [],
    createInitialPreOrder: async () => ({}),
    placeOrder: async order => { calls.push(order); return true; },
  };
  for (const name of events) {
    const subscribers = new Map();
    broker[name] = {
      subscribe: (owner, fn) => subscribers.set(owner, fn),
      unsubscribe: (owner, fn) => { if (subscribers.get(owner) === fn) subscribers.delete(owner); },
      emit: () => [...subscribers.values()].forEach(fn => fn('untrusted event payload')),
    };
  }
  const window = {
    TradingViewApi: { trading: () => ({ activeBroker: () => broker }) },
    __autotradePaperChanged: signal => calls.push(signal),
  };
  const bridge = runInNewContext(source, {
    window, location: { hostname: 'www.tradingview.com', pathname: '/chart/example/' },
  });
  return { bridge, broker, calls, events, switchAccount: () => { account = 'other'; } };
}

test('paper events wake reconciliation, with one subscription per delegate and clean removal', async () => {
  const { bridge, broker, calls, events } = mock();
  await bridge('dedicated-paper', 'attach');
  await bridge('dedicated-paper', 'attach');
  events.forEach(name => broker[name].emit());
  assert.deepEqual(calls, ['changed', 'changed', 'changed']);
  await bridge('dedicated-paper', 'detach');
  events.forEach(name => broker[name].emit());
  assert.equal(calls.length, 3);
});

test('account change during market preflight prevents submission', async () => {
  const { bridge, broker, calls, switchAccount } = mock();
  broker.createInitialPreOrder = async () => { switchAccount(); return {}; };
  await assert.rejects(bridge('dedicated-paper', 'open', {
    symbol: 'NASDAQ:GOOGL', side: 1, qty: 10, previousQty: 0, stop: 100,
  }), /账户不匹配/);
  assert.deepEqual(calls, []);
});
