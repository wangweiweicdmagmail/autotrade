// Verify UI scope and reporting with no browser, network or production state.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
let options;
const requests = [], confirmations = [];
let responseBody = { ok: true };
vm.runInNewContext(fs.readFileSync(path.join(__dirname, '../console/static/app.js'), 'utf8'), {
  Vue: { createApp(value) { options = value; return { use() { return { mount() {} }; } }; } },
  ElementPlus: {}, localStorage: { setItem() {} },
  fetch: async (url, request) => { requests.push({ url, request }); return { ok: true, json: async () => responseBody }; },
});
const ui = { ...options.methods, demoMode: false, status: { dry_run: false, connected: true },
  autoModels: [{ id: 'alpha', name: '模型甲' }, { id: 'beta', name: '模型乙' }],
  $confirm: async message => confirmations.push(message), tradeHeaders: () => ({}),
  $message: { success() {}, info() {}, error(message) { throw new Error(message); } } };
const single = { state: 'ACTIVE', execution_profile: 'single_entry', entries: 1,
  enabled: true, risk_budgets: [100], auto_breakeven: false };
assert.match(ui.autoNext(single), /仅管理/);
assert.match(ui.instanceRuleText(single), /关闭/);
assert.equal(ui.instanceMaxEntries(single), 1);
assert.match(ui.autoNext({ ...single, risk_budgets: undefined }), /未提供/);
assert.equal(ui.instanceState({ state: 'ACTIVE', exit_requested_reason: 'reverse' }), '退出中');
assert.equal(ui.instanceState({ state: 'ACTIVE', recovery_block_reason: 'missing' }), '恢复待核验');
assert.equal(ui.entryOrderTypeText({ entry_order_type: 'MKT' }), '市价 MKT');
assert.equal(ui.entryOrderTypeText({ entry_order_type: 'LMT' }), '限价 LMT');
assert.equal(ui.entryOrderTypeText({ limit_type: 'auto_ema9_pyramid' }), '类型未记录');
const stoppedA = { inst_id: 'done-a', state: 'DONE', qty: 0, lot_ids: [], enabled: false };
const watching = { inst_id: 'watch', state: 'WATCHING', qty: 0, lot_ids: [], enabled: true };
const pausedPosition = { inst_id: 'paused-position', state: 'ACTIVE', qty: 10, lot_ids: ['held'], enabled: false };
const closingPosition = { inst_id: 'closing-position', state: 'CLOSING', qty: 5, lot_ids: ['closing'], enabled: false };
const pending = { inst_id: 'pending', state: 'PENDING', qty: 0, lot_ids: ['pending'] };
const stoppedB = { ...stoppedA, inst_id: 'done-b' };
const sourceRows = [stoppedA, watching, pausedPosition, closingPosition, pending, stoppedB];
const monitor = { ...options.methods, lots: [], demoMode: false, autoView: { instances: sourceRows } };
const ordered = options.computed.orderedAutoInstances.call(monitor);
assert.equal(ordered.map(row => row.inst_id).join(','), 'paused-position,closing-position,watch,pending,done-a,done-b');
assert.equal(sourceRows.map(row => row.inst_id).join(','), 'done-a,watch,paused-position,closing-position,pending,done-b');
assert.equal(monitor.instanceInactive(pausedPosition), false);
assert.equal(monitor.instanceInactive(stoppedA), true);
assert.equal(monitor.instanceInactive({ ...stoppedA, qty: undefined }), false);
assert.equal(monitor.instanceInactive({ ...stoppedA, pending_lot_id: 'unresolved' }), false);
assert.equal(monitor.instanceInactive({ ...stoppedA, lot_ids: ['unresolved'] }), false);
assert.equal(monitor.instanceInactive({ ...stoppedA, recovery_block_reason: 'needs reconciliation' }), false);
monitor.lots = [{ lot_id: 'partial', filled_qty: 2, state: 'PENDING' }];
assert.equal(monitor.instanceHasPosition({ qty: 0, lot_ids: ['partial'] }), true);
assert.match(ui.protectionText({ state: 'ACTIVE', filled_qty: 10, stop_px: 99 }), /确认未提供/);
assert.match(ui.protectionText({ state: 'ACTIVE', filled_qty: 10, protection_order_status: 'Submitted', protection_order_qty: 5 }), /数量待核验/);
assert.match(ui.protectionText({ state: 'ACTIVE', filled_qty: 10, protection_order_status: 'Submitted', protection_order_qty: 10 }), /已受理/);
assert.equal(ui.wbCsvCell('=1+1'), '"\'=1+1"');
assert.equal(ui.wbCsvCell(-10.5), '"-10.5"');
ui.wb = { replay_bars: [{ time: 1000 }, { time: 1300 }] }; ui.wbChartSelection = 0;
ui.wbFocusTime(1300); assert.equal(ui.wbChartSelection, 1);
ui.wbExpected = 'changed draft'; ui.wbExpectationSnapshot = 'frozen expectation'; ui.wbReview = '存在差异';
assert.equal(ui.wbReport().manual_review.expected, 'frozen expectation');
// Selecting the promoted screening page must use the execution workspace,
// including after a saved research layout still points to its old location.
const initial = options.data();
ui.workspaceTabs = initial.workspaceTabs;
ui.workspaceLayouts = { research: { tab: 'screen', expanded: true } };
ui.selectWorkspace('execution', 'screen');
assert.equal(ui.bottomTab, 'screen');
assert.equal(ui.workspaceExpanded, true);
ui.selectWorkspace('research');
assert.equal(ui.bottomTab, 'reports');
ui.workspaceTabs = { execution: ['orders'], validation: ['backtest'] };
ui.workspaceGroup = 'execution'; ui.bottomTab = 'orders'; ui.bottomPanelHeight = 300;
ui.workspaceLayouts = {}; ui.workspaceExpanded = false;
ui.selectWorkspace('validation'); assert.equal(ui.bottomTab, 'backtest'); assert.equal(ui.workspaceExpanded, true);
ui.selectWorkspace('execution'); assert.equal(ui.bottomTab, 'orders'); assert.equal(ui.workspaceExpanded, false);
ui.workspaceTabs.execution = ['instances', 'orders', 'fvg'];
ui.selectWorkspace('execution', 'instances');
assert.equal(ui.bottomTab, 'instances');
assert.equal(ui.workspaceExpanded, false);
assert.equal(options.computed.logPanelStyle.call(ui).display, 'none');
ui.selectWorkspace('execution', 'orders');
assert.equal(ui.workspaceExpanded, true);
ui.selectWorkspace('execution', 'instances');
assert.equal(ui.workspaceExpanded, false);
(async () => {
  const instance = { strategy: 'beta', strategy_instance_id: 'independent', symbol: 'NASDAQ:NVDA', qty: 10, lot_ids: ['one'] };
  await ui.stopAutoInstance(instance, true);
  assert.match(confirmations[0], /模型乙.*independent.*NASDAQ:NVDA/);
  assert.match(requests[0].url, /strategy=beta&instance_id=independent/);
  const count = requests.length;
  const notices = [];
  ui.autoBreakevenBusy = {};
  ui.checkWrite403 = () => {};
  ui.$message = { info() {}, success: m => notices.push(['success', m]), warning: m => notices.push(['warning', m]), error: m => { throw Error(m); } };
  const held = { ...instance, state: 'ACTIVE', group_id: 'group-current', avg_cost: 101 };
  let releaseConfirm;
  ui.$confirm = async message => { confirmations.push(message); await new Promise(resolve => { releaseConfirm = resolve; }); };
  const first = ui.breakevenAutoInstance(held);
  await ui.breakevenAutoInstance(held);
  assert.equal(requests.length, count); // Busy starts before confirmation, preventing double clicks.
  responseBody = { ok: true, target_stop: 101, updated: ['one'], unchanged: [] };
  releaseConfirm(); await first;
  assert.equal(requests.length, count + 1);
  assert.match(confirmations.at(-1), /模型乙.*independent.*NASDAQ:NVDA.*交易组均价/);
  assert.match(requests.at(-1).url, /NASDAQ%3ANVDA\/breakeven\?strategy=beta&instance_id=independent/);
  assert.deepEqual(JSON.parse(requests.at(-1).request.body), { group_id: 'group-current', qty: 10, avg_cost: 101 });
  assert.equal(Object.keys(ui.autoBreakevenBusy).length, 0);
  assert.equal(held.breakeven_done, undefined); // Wait for actual state, never mark protection optimistically.
  ui.$confirm = async () => {};
  responseBody = { ok: false, failed: [{ lot_id: 'two', reason: '改单失败' }] };
  await ui.breakevenAutoInstance(held);
  assert.equal(notices.at(-1)[0], 'warning');
  assert.match(notices.at(-1)[1], /two.*改单失败.*成功的修改会保留/);
  const beforeGuard = requests.length;
  for (const row of [{ ...held, state: 'PENDING' }, { ...held, exit_requested_reason: 'exit' }, { ...held, recovery_block_reason: 'unknown' }, { ...held, breakeven_done: true }]) await ui.breakevenAutoInstance(row);
  ui.$confirm = async () => { throw 'cancel'; };
  await ui.breakevenAutoInstance(held);
  assert.equal(requests.length, beforeGuard);
  ui.demoMode = true;
  await ui.breakevenAutoInstance(held);
  await ui.closeLot({ lot_id: 'demo-one' }); await ui.cancelLot({ lot_id: 'demo-one' });
  await ui.breakevenLot({ lot_id: 'demo-one' }); await ui.closeHalf({ lot_id: 'demo-one' });
  assert.equal(requests.length, beforeGuard);
  ui.demoMode = false;
  ui.lots = [];
  const order = { symbol: 'NASDAQ:NVDA', side: 'long', qty: 10 };
  let slowPrompt = '';
  ui.$confirm = async message => { slowPrompt = message; throw 'cancel'; };
  const confirmSlow = ui.confirmSlowMarketEntry.bind(ui);
  assert.equal(await confirmSlow(order, new Date('2026-11-03T14:39:59Z')), true);
  assert.equal(slowPrompt, '');
  assert.equal(await confirmSlow(order, new Date('2026-07-01T13:40:00Z')), false);
  assert.match(slowPrompt, /09:40.*手动交易亏钱概率极大.*NVDA/);
  ui.confirmSlowMarketEntry = body => confirmSlow(body, new Date('2026-11-03T14:40:00Z'));
  await ui.placeOrder({ ...order, ready: true, limitPx: 101, stopPx: 100, limitType: 'market' });
  assert.equal(requests.length, beforeGuard);
  assert.equal(ui.placing, false);
  ui.autoModel = 'beta';
  ui.autoInstanceId = 'independent';
  ui.autoSymbols = 'NVDA';
  ui.autoRiskUsd = 200;
  const directions = { 'NASDAQ:NVDA': -1, 'NASDAQ:QQQ': 0 };
  responseBody = { model: '模型乙', symbols: ['NASDAQ:NVDA'], directions, risk: 200 };
  ui.$confirm = async message => {
    assert.match(message, /模型乙.*QQQ 当前方向：未就绪（仅供参考）.*NASDAQ:NVDA 空/);
    throw 'cancel';
  };
  await ui.startAuto();
  assert.equal(requests.length, beforeGuard + 1);
  assert.equal(requests.at(-1).url, '/api/auto/start/preview');
  ui.$confirm = async () => { responseBody = { started: ['NASDAQ:NVDA'], skipped: [] }; };
  await ui.startAuto();
  assert.equal(requests.at(-1).url, '/api/auto/start');
  assert.deepEqual(JSON.parse(requests.at(-1).request.body).confirmed_directions, directions);
  console.log('Workspace, model-specific semantics, protection evidence, export and demo isolation passed');
})().catch(error => { console.error(error); process.exitCode = 1; });

// Signal timestamps match TradingView candle labels, including older snapshots.
assert.equal(ui.openingBreakoutBarTime({ bar_time: 300, time: 600 }), 300);
assert.equal(ui.openingBreakoutBarTime({ time: 600 }), 300);
const breakoutGroups = options.computed.openingBreakoutGroups.call({
  ...options.methods,
  openingBreakouts: [{ id: 'a', bar_time: 300, time: 600 }, { id: 'b', time: 600 }],
});
assert.equal(breakoutGroups.length, 1);
assert.equal(breakoutGroups[0].time, 300);

// A confirmed ST flip only notifies. It must not invoke confirmation or an exit.
const flipNotices=[], flipSpoken=[];
const flipUI={...options.methods,lastScanAlert:{},scanAlerts:[
  {ticker:'SMH',symbol:'NASDAQ:SMH',side:'short',qty:155,st_line:451.13,close:464.38,scanned_at:1},
],speak:text=>flipSpoken.push(text),$notify:notice=>flipNotices.push(notice),
$confirm:()=>{throw new Error('ST alert must not confirm an exit');},
closePosition:()=>{throw new Error('ST alert must not submit an exit');}};
flipUI.checkScanAlert();flipUI.checkScanAlert();
assert.equal(flipNotices.length,1);
assert.match(flipNotices[0].message,/持仓空头.*收盘确认ST 多头/);
assert.equal(flipSpoken.length,1);

const fvgConsentNotices=[];
const fvgConsentUI={...options.methods,lastFvgReverseAlert:{},
  fvgReverseAlerts:[{lot_id:'asml',ticker:'ASML',side:'long',qty:68,pnl:59.43,alerted_at:1,kind:'reverse_warning'}],
  speak(){},$notify:n=>fvgConsentNotices.push(n),
  $confirm(){throw new Error('FVG must wait for a separate explicit user action');},
  closePosition(){throw new Error('Must not close all sibling lots');},
  breakevenLot(){throw new Error('Must not breakeven automatically');}};
fvgConsentUI.checkFvgReverseAlert();fvgConsentUI.checkFvgReverseAlert();
assert.equal(fvgConsentNotices.length,1);
assert.match(fvgConsentNotices[0].message,/对应订单批次/);

// Order history shows the recorded position side, independent of current ST.
assert.equal(ui.lotSideLabel({side:'long'}),'做多');
assert.equal(ui.lotSideLabel({side:'short'}),'做空');
assert.match(ui.lotEntryAction({side:'short'}),/SELL.*开仓/);
assert.match(ui.lotEntryAction({side:'long'}),/BUY.*开仓/);
assert.equal(ui.lotOpenTime({entry_broker_time:1793902183,entry_time:1793902200}),'2026-11-05 13:09:43');
assert.equal(ui.lotExitReason({state:'CLOSED',exit_reason:'stop'}),'保护止损触发');
assert.match(ui.lotExitReason({state:'CLOSED',exit_reason:'st_flip'}),/ST转向.*历史/);
assert.equal(ui.lotExitReason({state:'CLOSED'}),'原因未记录');
assert.equal(ui.lotExitReason({state:'CLOSING'}),'待成交确认：原因未记录');
assert.match(ui.lotExitReason({state:'CLOSING',close_reason:'manual'}),/^待成交确认/);
const orderPage=fs.readFileSync(path.join(__dirname,'../console/static/index.html'),'utf8');
for (const label of ['开仓方向','开仓时间 · 美东','平仓时间 · 美东','平仓原因']) assert.ok(orderPage.includes(label));

// A mixed live/confirmed ST alert must show both, without claiming the live ST flipped short.
flipUI.lastScanAlert={};flipUI.scanAlerts=[{ticker:'QCOM',side:'long',qty:608,
  confirmed_direction:-1,realtime_direction:1,confirmed_bar_time:1793901900,scanned_at:2}];
flipUI.checkScanAlert();
assert.match(flipNotices.at(-1).message,/实时ST 多头.*收盘确认ST 空头/);

{
  const range = require('../console/static/range-distribution');
  const time = Date.parse('2026-11-05T09:45:00-05:00') / 1000;
  function quote(close, open, speed) {
    return {
      previous_regular_range: {day: '2026-11-05', date: '2026-11-04', high: 120, low: 80},
      bar: {time, close, open}, e9: {time, ema_slope_atr: speed},
      attack: {time, pmh: 106, pml: 95, opening3High: 105, opening3Low: 93},
    };
  }
  const wrong = quote(108, 109, 2);
  wrong.e9.time -= 300;
  const map = range.build({AMD: quote(106, 100, -1), NVDA: quote(108, 109, 2),
    COIN: quote(90, 95, -3.4), WRONG: wrong}, ['AMD', 'NVDA', 'COIN', 'WRONG']);
  assert.deepEqual(map.rows.map(r => [r.ticker, r.major, r.minor, r.tone]),
    [['AMD', 1, 1, 'flat'], ['NVDA', 1, 0, 'down'], ['COIN', 1, 2, 'down']]);
  assert.deepEqual(map.rows[1].target, {label: '昨高', price: 120});
  assert.equal(map.limit, 4);
  assert.deepEqual(map.pending, ['WRONG']);
  const crowded = {
    SLOW: quote(108, 107, 1),
    MID: quote(108, 107, 1.1),
    FAST: quote(108, 107, 1.2),
    FAR: quote(108, 107, -2),
    INSIDE1: quote(100, 99, 0.4),
    INSIDE2: quote(100, 99, 0.41),
  };
  const layout = range.build(crowded, Object.keys(crowded), 480);
  const [outside, inside] = layout.groups[1].lanes;
  assert.equal(outside.layers, 3);
  assert.deepEqual(outside.rows.map(row => [row.ticker, row.layer]),
    [['SLOW', 0], ['MID', 1], ['FAST', 2], ['FAR', 0]]);
  assert.equal(inside.layers, 1);
  assert.equal(range.build(crowded, Object.keys(crowded), 3200).groups[1].lanes[0].layers, 1);
  const early = quote(109, 108, 1);
  early.bar.time -= 3600;
  early.e9.time = early.attack.time = early.bar.time;
  early.attack.opening3High = early.attack.opening3Low = null;
  const provisional = range.build({EARLY: early}, ['EARLY']).rows[0];
  assert.equal(provisional.forming, true);
  assert.equal(provisional.target, null);
}
