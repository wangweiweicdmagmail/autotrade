'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { test } = require('node:test');
const RangeDistribution = require('../console/static/range-distribution');
const {rhythmTradePreview}=require('../console/static/market-rhythm');
const now = Date.parse('2026-11-05T17:31:00Z') / 1000;
const barTime = now - 60;
const symbol = 'NASDAQ:APP';

function harness(side = 'long') {
  let options;
  const requests = [], messages = [], confirmations = [];
  const direction = side === 'long' ? 1 : -1;
  class Clock extends Date {
    constructor(...args) { super(...(args.length ? args : [now * 1000])); }
    static now() { return now * 1000; }
  }
  const mock = {
    previewDirection: direction,
    confirm: async () => {},
    fetch: async url => {
      if (url.endsWith('/preview')) return { model: 'EMA7 v2', symbols: [symbol], directions: { [symbol]: mock.previewDirection }, risk: 700 };
      if (url === '/api/auto/start') return { started: [symbol], skipped: [] };
      assert.equal(url, '/api/auto/golden-entry');
      return { lot_id: 'test' };
    },
  };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, '../console/static/app.js'), 'utf8'), {
    Date: Clock, RangeDistribution, rhythmTradePreview,
    Vue: { createApp(value) { options = value; return { use() { return this; }, mount() {} }; } },
    ElementPlus: {}, localStorage: { getItem() { return null; }, setItem() {} },
    fetch: async (url, request) => {
      requests.push({ url, body: JSON.parse(request.body) });
      return { ok: true, json: async () => mock.fetch(url) };
    },
  });
  const close = side === 'long' ? 109 : 95;
  const ui = {
    ...options.data(), ...options.methods, now, watchlist: [symbol], rangeSelectedSymbol: symbol,
    autoModels: [{ id: 'ema7_autotrade_v2', name: 'EMA7 v2' }],
    autoModel: 'ema9_pyramid', autoSymbols: '', autoInstanceId: 'other',
    status: { market_running: true, market_stale: false, dry_run: true, connected: true, risk_per_trade_usd: 700, equity: 100000 },
    market: {
      [symbol]: {
        price_observed_at: now - 1,
        bar: { time: barTime, open: close - 0.1, close, low: close - 1, high: close + 1 },
        bar_c: { time: barTime - 300, close },
        ts: { direction: -direction, atr: 2 }, ts_c: { direction: -direction, atr: 2 },
        e9: { time: barTime, ema_9: side === 'long' ? 108 : 96, ema_slope_atr: direction },
        e7v2: { time: barTime, direction, entry_price: close - direction },
        attack: { time: barTime, pmh: 104, pml: 100, opening3High: 103, opening3Low: 101 },
        previous_regular_range: { day: '2026-11-05', date: '2026-11-04', high: 120, low: 80 },
      },
    },
    $message: Object.fromEntries(['success', 'warning', 'info', 'error'].map(level => [level, value => messages.push({ level, value })])),
    $confirm: async (message, title) => { confirmations.push({ message, title }); return mock.confirm(); },
    tradeHeaders: () => ({}), checkWrite403() {},
  };
  for (const [name, getter] of Object.entries(options.computed)) {
    Object.defineProperty(ui, name, { get: () => getter.call(ui), configurable: true });
  }
  return { ui, mock, requests, messages, confirmations, options };
}

test('VWAP pullback ranks same-direction outer-band candidates by position independently of speed', () => {
  const {ui} = harness();
  const row = (symbol, position, st_direction, speed) =>
    ({symbol, position, st_direction, speed, lower: 99, upper: 101, st_price: 100});
  ui.vwapDistributionFrame = {rows: [
    row('NASDAQ:LONG', 1.2, 1, 1.4), row('NASDAQ:SHORT', -1.2, -1, -2),
    row('NASDAQ:SHORTNEAR', -1.1, -1, -99), row('NASDAQ:LONGFAR', 2.3, 1, 0.01),
    row('NASDAQ:SHORTFAR', -3.2, -1, -0.01), row('NASDAQ:INVALID', Infinity, 1, 8),
    row('NASDAQ:WRONGSHORT', 1.2, -1, -5), row('NASDAQ:WRONGLONG', -1.2, 1, 5),
    row('NASDAQ:ONBAND', 1, 1, 3), row('NASDAQ:UNKNOWN', -1.2, 0, -3),
  ]};
  assert.deepEqual(Array.from(ui.vwapPullbackCandidates, item => [item.symbol, item.direction]),
    [['NASDAQ:LONGFAR', 1], ['NASDAQ:LONG', 1], ['NASDAQ:SHORTNEAR', -1],
      ['NASDAQ:SHORT', -1], ['NASDAQ:SHORTFAR', -1]]);
});

test('VWAP frontend accepts a newer same-minute batch and retains the last complete batch during gaps', () => {
  const {ui} = harness();
  const frame = (time, revision) => ({time, revision, rows: [
    {symbol, time, position: revision}, {symbol: 'NASDAQ:NVDA', time, position: -revision},
  ]});
  const receive = value => ui.acceptVwapDistribution({frame: value, progress: {time: value.time, ready: 2, total: 2}});
  const first = frame(1020, 100);
  assert.equal(receive(first), true);
  const second = frame(1020, 101);
  assert.equal(receive(second), true);
  assert.equal(ui.vwapDistributionFrame, second);
  for (const stale of [first, second, frame(960, 999999)]) {
    assert.equal(receive(stale), false);
    assert.equal(ui.vwapDistributionFrame, second);
  }
  assert.equal(ui.acceptVwapDistribution({frame: null, progress: {time: 1080, ready: 1, total: 2}}), false);
  assert.equal(ui.vwapDistributionFrame, second);
  assert.equal(ui.vwapDistributionProgress.ready, 1);
  const next = frame(1080, 1);
  assert.equal(receive(next), true);
  assert.equal(ui.vwapDistributionFrame, next);
});

test('VWAP frontend rejects partial, mixed-minute, duplicate-symbol and invalid version batches', () => {
  const {ui} = harness();
  const valid = {time: 1020, rows: [{symbol, time: 1020}, {symbol: 'NASDAQ:NVDA', time: 1020}]};
  assert.equal(ui.acceptVwapDistribution({frame: valid, progress: {total: 2}}), true);
  const candidates = [
    {...valid, revision: 1, rows: valid.rows.slice(0, 1)},
    {...valid, revision: 1, rows: [valid.rows[0], valid.rows[0]]},
    {...valid, revision: 1, rows: [valid.rows[0], {...valid.rows[1], time: 960}]},
    {...valid, revision: 1, rows: []},
    ...[-1, true, '1', null, Infinity, NaN].map(revision => ({...valid, revision})),
  ];
  for (const frame of candidates) {
    assert.equal(ui.acceptVwapDistribution({frame, progress: {total: 2}}), false);
    assert.equal(ui.vwapDistributionFrame, valid);
  }
  const revision = {...valid, revision: 1};
  assert.equal(ui.acceptVwapDistribution({frame: revision, progress: {total: 2}}), true);
  assert.equal(ui.vwapDistributionFrame, revision);
});

function stalledVwapFrame(ui, missing = 1) {
  const symbols = Array.from({length: 51}, (_, i) => 'NASDAQ:V' + String(i).padStart(2, '0'));
  const row = (symbol, time) => ({symbol, time, st_direction: 1, position: 0.5,
    speed: 0.1, vwap: 100, upper: 102, lower: 98, ema: 101});
  const previous = {time: now - 60, revision: 1000, rows: symbols.map(symbol => row(symbol, now - 60))};
  ui.acceptVwapDistribution({frame: previous, progress: {time: previous.time, ready: 51, total: 51}});
  const market = Object.fromEntries(symbols.map((symbol, i) => [symbol,
    {vwap_1m: row(symbol, i >= 51 - missing ? now - 60 : now)}]));
  const update = {frame: previous, progress: {time: now, ready: 51 - missing, total: 51}};
  return {symbols, row, previous, market, update};
}

test('one delayed VWAP symbol cannot freeze the other fifty after the five-second display grace', () => {
  const {ui, requests} = harness();
  const {symbols, row, previous, market, update} = stalledVwapFrame(ui);
  assert.equal(ui.acceptVwapDistribution(update, market, now + 1), false);
  assert.equal(ui.acceptVwapDistribution(update, market, now + 5.9), false);
  assert.equal(ui.vwapDistributionFrame, previous);
  assert.equal(ui.acceptVwapDistribution(update, market, now + 6), true);
  const partial = ui.vwapDistributionFrame;
  assert.equal(partial.time, now);
  assert.equal(partial.rows.length, 50);
  assert.equal(partial.display_expected, 51);
  assert.equal(partial.display_delayed.join(','), symbols[50]);
  assert.ok(partial.rows.every(value => value.time === now));
  assert.equal(ui.vwapDistributionSourceFrame, previous);
  assert.equal(market[symbols[50]].vwap_1m.time, now - 60);
  assert.equal(ui.acceptVwapDistribution(update, market, now + 7), false);
  assert.equal(ui.vwapDistributionFrame, partial);
  assert.equal(ui.vwapDistributionFrame.display_updated_at, (now + 6) * 1000);
  market[symbols[0]].vwap_1m.position = 0.75;
  assert.equal(ui.acceptVwapDistribution(update, market, now + 8), true);
  assert.equal(ui.vwapDistributionFrame.rows[0].position, 0.75);
  market[symbols[50]].vwap_1m = row(symbols[50], now);
  const complete = {time: now, revision: 1001, rows: symbols.map(symbol => market[symbol].vwap_1m)};
  assert.equal(ui.acceptVwapDistribution({frame: complete, progress: {time: now, ready: 51, total: 51}}, market, now + 9), true);
  assert.equal(ui.vwapDistributionFrame, complete);
  assert.equal(ui.vwapDistributionFrame.display_only, undefined);
  assert.equal(requests.length, 0);
});

test('display fallback holds on a broad outage and tolerates an absent auxiliary EMA speed', () => {
  const {ui} = harness();
  const {previous, market, update, symbols, row} = stalledVwapFrame(ui, 11);
  ui.acceptVwapDistribution(update, market, now + 1);
  assert.equal(ui.acceptVwapDistribution(update, market, now + 10), false);
  assert.equal(ui.vwapDistributionFrame, previous);
  for (const symbol of symbols) market[symbol].vwap_1m = row(symbol, now);
  market[symbols[0]].vwap_1m.speed = null;
  update.progress.ready = 50;
  assert.equal(ui.acceptVwapDistribution(update, market, now + 11), true);
  assert.equal(ui.vwapDistributionFrame.rows.length, 51);
  assert.equal(ui.vwapDistributionFrame.display_delayed.length, 0);
  assert.equal(ui.vwapDistributionFrame.display_auxiliary_waiting.join(','), symbols[0]);
});

test('startup display fallback uses the declared stock universe and excludes default-session indices', () => {
  const {ui} = harness();
  const {symbols, market, update} = stalledVwapFrame(ui);
  ui.vwapDistributionFrame = null;
  ui.vwapDistributionSourceFrame = null;
  update.frame = null;
  ui.status.market_session = {symbols: Object.fromEntries([
    ...symbols.map(symbol => [symbol, {requested: '24h'}]),
    ...['BLACKBULL:WTI', 'CAPITALCOM:NAS100', 'CAPITALCOM:SPX500'].map(symbol => [symbol, {requested: 'default'}]),
  ])};
  ui.acceptVwapDistribution(update, market, now + 1);
  assert.equal(ui.acceptVwapDistribution(update, market, now + 6), true);
  assert.equal(ui.vwapDistributionFrame.rows.length, 50);
  assert.equal(ui.vwapDistributionFrame.display_delayed.join(','), symbols[50]);
});

test('old complete frames cannot overwrite a newer display fallback', () => {
  const {ui} = harness();
  const {previous, market, update} = stalledVwapFrame(ui);
  previous.time = now;
  previous.rows.forEach(row => { row.time = now; });
  update.frame = previous;
  ui.acceptVwapDistribution(update, market, now + 1);
  ui.acceptVwapDistribution(update, market, now + 6);
  const partial = ui.vwapDistributionFrame;
  assert.equal(partial.display_only, true);
  const old = {frame: previous, progress: {time: now, ready: 51, total: 51}};
  assert.equal(ui.acceptVwapDistribution(old, market, now + 7), false);
  assert.equal(ui.vwapDistributionFrame, partial);
});

test('manual market direction switches to current one-minute ST when the server supports it', () => {
  const {ui} = harness();
  const data = ui.market[symbol];
  const market = {symbol, limitType: 'market'};
  assert.equal(ui.calcSlot(market).side, 'short');
  ui.status.manual_market_st_interval_seconds = 60;
  const minute = now - 1;
  data.market_1m = {bar: {time: minute}};
  data.vwap_pullback = {time: minute, direction: 1, candidate_direction: 0};
  assert.equal(ui.calcSlot(market).side, 'long');
  assert.equal(ui.calcSlot({...market, limitType: 'midpoint'}).side, 'short');
  data.vwap_pullback.time -= 60;
  assert.equal(ui.calcSlot(market).side, null);
  assert.equal(ui.calcSlot(market).ready, false);
});

function prepareManualCard(ui, direction = 1) {
  ui.slots[0].limitType='market'; // Hidden legacy contract still has isolated coverage.
  const minute = now - 1;
  ui.status.manual_market_st_interval_seconds = 60;
  ui.market[symbol].market_1m = {bar: {time: minute}, bar_c: {time: minute - 60}};
  ui.market[symbol].vwap_pullback = {time: minute, direction};
  ui.slots[0].symbol = symbol;
  return ui.slots[0];
}

test('range direction requires confirmation and cancel or duplicate clicks cannot enable it', async () => {
  const {ui, mock, confirmations, requests} = harness();
  const source = prepareManualCard(ui);
  mock.confirm = async () => { throw new Error('cancel'); };
  await ui.toggleMarketDirection(0, true);
  assert.equal(ui.slotRows[0].marketDirectionMode, 'st');
  assert.equal(source.directionConfirming, false);
  assert.match(confirmations[0].message, /震荡行情.*区间边缘.*不要追高追低/);
  let accept;
  mock.confirm = () => new Promise(resolve => { accept = resolve; });
  const pending = ui.toggleMarketDirection(0, true);
  assert.equal(ui.slotRows[0].marketDirectionMode, 'st');
  assert.equal(source.directionConfirming, true);
  await ui.toggleMarketDirection(0, true);
  assert.equal(confirmations.length, 2);
  accept();
  await pending;
  assert.equal(ui.slotRows[0].marketDirectionMode, 'manual');
  assert.equal(source.directionConfirming, false);
  await ui.toggleMarketDirection(0, false);
  assert.equal(ui.slotRows[0].marketDirectionMode, 'st');
  assert.equal(confirmations.length, 2);
  assert.equal(requests.length, 0);
});

test('range direction previews and submits either side with matching stops and confirmation evidence', async () => {
  for (const side of ['long', 'short']) {
    const {ui, mock, requests} = harness();
    const source = prepareManualCard(ui, side === 'long' ? -1 : 1);
    mock.fetch = async url => {
      assert.equal(url, '/api/lots/limit-stop');
      return {lot: {lot_id: 'isolated', symbol, side, qty: 175, entry_px: 109, stop_px: side === 'long' ? 105 : 113}};
    };
    await ui.toggleMarketDirection(0, true);
    ui.setManualMarketSide(0, side);
    const row = ui.slotRows[0];
    assert.equal(row.side, side);
    assert.equal(row.stSide, side === 'long' ? 'short' : 'long');
    assert.equal(row.stopPx, side === 'long' ? 105 : 113);
    assert.equal(row.qty, 175);
    assert.equal(row.ready, true);
    await ui.placeOrder(row);
    assert.equal(requests.length, 1);
    assert.equal(requests[0].body.side, side);
    assert.equal(requests[0].body.stop_px, row.stopPx);
    assert.equal(requests[0].body.market_direction_mode, 'manual');
    assert.equal(requests[0].body.range_direction_confirmed, true);
    assert.equal(requests[0].body.force, false);
    assert.match(requests[0].body.reason, /已确认震荡行情/);
    source.limitType = 'midpoint';
    assert.equal(ui.slotRows[0].marketDirectionMode, 'st');
  }
});

test('confirmed ST agrees with backend and range mode cannot bypass stale data', async () => {
  const {ui} = harness();
  prepareManualCard(ui, -1);
  const data = ui.market[symbol];
  data.vwap_pullback_c = {time: now - 61, direction: 1};
  assert.equal(ui.slotRows[0].side, 'long');
  await ui.toggleMarketDirection(0, true);
  ui.setManualMarketSide(0, 'short');
  data.market_1m.bar.time -= 60;
  assert.equal(ui.slotRows[0].side, 'short');
  assert.equal(ui.slotRows[0].stSide, null);
  assert.equal(ui.slotRows[0].ready, false);
});

test('changing stock or order type resets range choice and invalidates pending confirmations', async () => {
  const {ui, mock, options} = harness();
  const source = prepareManualCard(ui);
  await ui.toggleMarketDirection(0, true);
  const reset = options.watch.manualTradeContexts.handler;
  for (const field of ['symbol', 'limitType']) {
    const previous = ui.manualTradeContexts;
    source[field] = field === 'symbol' ? 'NASDAQ:NVDA' : 'ema';
    assert.equal(ui.slotRows[0].marketDirectionMode, 'st');
    reset.call(ui, ui.manualTradeContexts, previous);
    assert.equal(source.manualDirection, null);
    source.symbol = symbol;
    source.limitType = 'market';
    await ui.toggleMarketDirection(0, true);
  }
  await ui.toggleMarketDirection(0, false);
  let accept;
  mock.confirm = () => new Promise(resolve => { accept = resolve; });
  const pending = ui.toggleMarketDirection(0, true);
  const previous = ui.manualTradeContexts;
  source.symbol = 'NASDAQ:NVDA';
  reset.call(ui, ui.manualTradeContexts, previous);
  source.symbol = symbol;
  accept();
  await pending;
  assert.equal(source.manualDirection, null);
});

test('range market submission cancels if selection changes during the existing order confirmation', async () => {
  const {ui, mock, requests} = harness();
  const source = prepareManualCard(ui);
  await ui.toggleMarketDirection(0, true);
  let accept;
  mock.confirm = () => new Promise(resolve => { accept = resolve; });
  const pending = ui.placeOrder(ui.slotRows[0]);
  source.symbol = 'NASDAQ:NVDA';
  accept();
  await pending;
  assert.equal(requests.length, 0);
  assert.equal(ui.placing, false);
});

test('VWAP selection updates the manual trade candidate and preview while the panel stays collapsed', () => {
  const {ui, requests, confirmations} = harness();
  const target = 'NASDAQ:NVDA';
  const minute = now - 1;
  ui.status.candidate_entry_required=true;
  ui.status.candidate_order_policy_version=1;
  ui.status.candidate_protection_live_version=1;
  ui.slots[0].vwapOutsideOnly=false; // This case isolates selection; policy gates have separate tests.
  ui.entryCandidates={ready:true,td_threshold:1,ema9_period:9,ema9_interval_seconds:300,as_of:now,rows:[symbol,target].map((symbol,i)=>({symbol,side:i?'short':'long',direction:i?-1:1,sigma:2,td_count:i?-3:3,td_ready:true,bar_time:now-60,confirmed_at:now}))};
  ui.market[symbol].market_1m = {bar: {time: minute}};
  ui.market[symbol].ts.ema_9=108;
  ui.market[symbol].vwap_pullback = {time: minute, direction: 1};
  ui.market[target] = {...ui.market[symbol],
    ts:{...ui.market[symbol].ts,ema_9:203},
    bar: {time: barTime, open: 201, high: 209, low: 198, close: 202},
    vwap_pullback: {time: minute, direction: -1}};
  const switched = [];
  ui.tvShow = value => switched.push(value);
  ui.slots[0].symbol = symbol;
  ui.deckCollapsed = true;
  const previous = ui.slotRows[0];

  ui.selectVwapStock(target);
  const current = ui.slotRows[0];
  assert.equal(ui.slots[0].symbol, target);
  assert.equal(ui.deckCollapsed, true);
  assert.equal(current.side, 'short');
  assert.equal(current.limitPx, 202);
  assert.ok(current.stopPx > current.limitPx);
  assert.ok(current.qty > 0 && current.ready);
  assert.notEqual(current.stopPx, previous.stopPx);
  assert.notEqual(current.qty, previous.qty);
  assert.equal(switched.at(-1), target);

  ui.selectVwapStock(symbol);
  assert.equal(ui.deckCollapsed, true);
  assert.equal(ui.slotRows[0].side, 'long');
  assert.equal(ui.slotRows[0].limitPx, 109);
  ui.slots[0].limitType = 'channel';
  ui.selectVwapStock(target);
  assert.equal(ui.slots[0].limitType, 'channel');
  assert.equal(requests.length, 0);
  assert.equal(confirmations.length, 0);
});

test('failed strategy start speaks once and a cancelled confirmation stays quiet', async () => {
  const {ui, mock, messages} = harness();
  const spoken = [];
  ui.speak = (text, {priority}) => spoken.push({text, priority});
  const target = {symbols: [symbol], strategy: 'vwap_outer_pullback', direction: 1};
  mock.confirm = async () => { throw new Error('cancel'); };
  await ui.startAuto(target);
  assert.equal(spoken.length, 0);
  mock.confirm = async () => {};
  const normalFetch = mock.fetch;
  mock.fetch = async url => {
    if (url === '/api/auto/start') throw new Error('启动请求失败');
    return normalFetch(url);
  };
  await ui.startAuto(target);
  assert.deepEqual(spoken, [{text: 'A P P 策略启动失败，请检查', priority: true}]);
  assert.deepEqual(messages, [{level: 'error', value: '启动请求失败'}]);
});

test('range readiness requires a live, completed opening range and EMA breakout', () => {
  const { ui } = harness();
  assert.equal(ui.rangeReadyTargets.length, 1);
  assert.ok(ui.rangeGoldenBlock);
  assert.equal(ui.rangeAutoBlock, '');
  ui.market[symbol].e9.ema_9 = 104;
  assert.equal(ui.rangeReadyTargets.length, 0);
  ui.market[symbol].e9.ema_9 = 108;
  ui.market[symbol].price_observed_at = now - 46;
  assert.equal(ui.rangeReadyTargets.length, 0);
  ui.market[symbol].price_observed_at = now - 1;
  ui.status.market_stale = true;
  assert.equal(ui.rangeReadyTargets.length, 0);
  ui.status.market_stale = false;
  ui.now = barTime + 300;
  assert.equal(ui.rangeReadyTargets.length, 0);
  assert.equal(ui.rangeTargetReady({ ...ui.rangeSelectedRow, ready: false }), false);
});

function golden(direction, entrySignal = 1) {
  return { time: barTime, direction, entry_price: 105, long_stop: 103, short_stop: 107, entry_signal: entrySignal };
}

test('range shortcut uses the selected stock’s golden direction and shared entry endpoint', async () => {
  for (const side of ['long', 'short']) {
    const { ui, requests, confirmations } = harness(side);
    const direction = side === 'long' ? -1 : 1;
    ui.market[symbol].golden = golden(direction, 0);
    ui.market[symbol].e9.ema_9 = 102;
    assert.equal(ui.rangeReadyTargets.length, 0);
    assert.equal(ui.rangeGoldenSignal.stop_px, direction === 1 ? 103 : 107);
    await ui.rangeGoldenOrder();
    await ui.rangeGoldenOrder();
    assert.equal(requests.length, 1);
    assert.equal(requests[0].url, '/api/auto/golden-entry');
    assert.deepEqual(requests[0].body, { symbol, bar_time: barTime, direction, manual_window: true });
    assert.equal(confirmations[0].title, '慢速行情 · 手动交易二次确认');
    assert.equal(ui.autoModel, 'ema9_pyramid');
    assert.equal(ui.rangeActionBusy, false);
  }
});

test('golden shortcut cancels if the target, signal or freshness changes during confirmation', async () => {
  for (const change of ['selection', 'signal', 'expiry', 'freshness']) {
    const { ui, mock, requests, confirmations } = harness();
    ui.market[symbol].golden = golden(1);
    let accept;
    mock.confirm = () => new Promise(resolve => { accept = resolve; });
    const first = ui.rangeGoldenOrder();
    await ui.rangeGoldenOrder();
    assert.equal(confirmations.length, 1);
    if (change === 'selection') ui.rangeSelectedSymbol = 'NASDAQ:NVDA';
    if (change === 'signal') ui.market[symbol].golden.direction = 0;
    if (change === 'expiry') ui.now = barTime + 300;
    if (change === 'freshness') ui.now += 15;
    accept();
    await first;
    assert.equal(requests.length, 0);
    assert.equal(ui.rangeActionBusy, false);
  }
});

test('automatic action previews and confirms only EMA7 v2 for the locked symbol', async () => {
  const { ui, requests, confirmations } = harness();
  await ui.rangeStartAuto();
  assert.deepEqual(requests.map(request => request.url), ['/api/auto/start/preview', '/api/auto/start']);
  for (const { body } of requests) {
    assert.equal(body.strategy, 'ema7_autotrade_v2');
    assert.equal(body.instance_id, 'default');
    assert.deepEqual(body.symbols, [symbol]);
  }
  assert.equal(confirmations[0].title, '启动执行实例 · 二次确认');
  assert.equal(ui.autoModel, 'ema9_pyramid');
  assert.equal(ui.autoInstanceId, 'other');
  assert.equal(ui.autoSymbols, '');
});

test('automatic action rejects changed direction, cancelled confirmation and lost lock', async () => {
  for (const mode of ['direction', 'cancel', 'selection', 'freshness']) {
    const { ui, mock, requests } = harness();
    if (mode === 'direction') mock.previewDirection = -1;
    if (mode === 'cancel') mock.confirm = async () => { throw 'cancel'; };
    if (mode === 'selection') mock.confirm = async () => { ui.rangeSelectedSymbol = 'NASDAQ:NVDA'; };
    if (mode === 'freshness') mock.confirm = async () => { ui.now += 60; };
    await ui.rangeStartAuto();
    assert.deepEqual(requests.map(request => request.url), ['/api/auto/start/preview']);
    assert.equal(ui.rangeActionBusy, false);
  }
});

test('golden shortcut blocks missing signals, demo and existing automatic positions', async () => {
  const { ui, requests } = harness();
  await ui.rangeGoldenOrder();
  ui.market[symbol].golden = golden(1, 0);
  ui.demoMode = true;
  await ui.rangeGoldenOrder();
  await ui.rangeStartAuto();
  ui.demoMode = false;
  ui.lots = [{ symbol, state: 'ACTIVE', filled_qty: 1, limit_type: 'auto_ema7_autotrade_v2' }];
  await ui.rangeGoldenOrder();
  assert.equal(requests.length, 0);
});

test('untouched gold signal feed card opens after manual confirmation and shares duplicate protection', async () => {
  const { ui, requests, confirmations } = harness();
  ui.market[symbol].golden = golden(1, 0);
  const [{ row }] = ui.entrySignalGroups[0].rows;
  assert.equal(ui.goldenEntryDisabled(row), false);
  await ui.placeGoldenEntry(row);
  await ui.rangeGoldenOrder();
  assert.equal(confirmations.length, 1);
  assert.deepEqual(requests, [{
    url: '/api/auto/golden-entry',
    body: { symbol, bar_time: barTime, direction: 1, manual_window: true },
  }]);
});

test('gold signal leads its time group and stays visible without allowing entry after rollover', async () => {
  const { ui, requests } = harness();
  ui.openingBreakouts = [{ id: 'breakout', bar_time: barTime }];
  ui.market[symbol].golden = golden(1);
  ui.rememberGoldenFeedRows(ui.goldenSignalRows);
  assert.deepEqual(Array.from(ui.entrySignalGroups[0].rows, item => item.kind), ['golden', 'opening']);
  ui.now = barTime + 300;
  ui.market[symbol].bar = { time: ui.now };
  delete ui.market[symbol].golden;
  const row = ui.goldenFeedRows[0];
  assert.equal(row.touched, true);
  assert.equal(ui.goldenEntryDisabled(row), true);
  await ui.placeGoldenEntry(row);
  assert.equal(requests.length, 0);
  assert.equal(ui.rangeGoldenSignal, undefined);
});

test('confirmed snapshot restores the previous gold signal after refresh', () => {
  const { ui } = harness();
  ui.market[symbol].golden_c = { ...golden(-1), time: barTime - 300 };
  ui.rememberConfirmedGoldenFeedRows();
  assert.equal(ui.latestEntrySignalTime, barTime - 300);
  assert.equal(ui.goldenFeedRows[0].side, 'short');
  assert.equal(ui.goldenEntryDisabled(ui.goldenFeedRows[0]), true);
});
