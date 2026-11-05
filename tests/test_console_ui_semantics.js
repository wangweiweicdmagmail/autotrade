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
  const directions = { 'NASDAQ:NVDA': -1, 'NASDAQ:QQQ': -1 };
  responseBody = { model: '模型乙', symbols: ['NASDAQ:NVDA'], directions, risk: 200 };
  ui.$confirm = async message => {
    assert.match(message, /模型乙.*QQQ 当前方向：空.*NASDAQ:NVDA 空/);
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
