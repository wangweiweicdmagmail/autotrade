// Verify UI scope and reporting with no browser, network or production state.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
let options;
const requests = [], confirmations = [];
const storage = new Map();
const speechQueue = [];
let speechCancels = 0;
let responseBody = { ok: true };
vm.runInNewContext(fs.readFileSync(path.join(__dirname, '../console/static/app.js'), 'utf8'), {
  Vue: { createApp(value) { options = value; return { use() { return { mount() {} }; } }; } },
  ElementPlus: {}, localStorage: { getItem: key => storage.get(key), setItem: (key, value) => storage.set(key, value) },
  window: { speechSynthesis: {
    cancel() { speechCancels += 1; speechQueue.length = 0; },
    speak(utterance) { speechQueue.push(utterance); },
  } },
  SpeechSynthesisUtterance: class { constructor(text) { this.text = text; } },
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
assert.equal(initial.bottomTab, 'rhythm');
assert.deepEqual(Array.from(initial.workspaceTabs.execution.slice(0, 2)), ['rhythm', 'vwap1m']);
assert.equal(initial.workspaceExpanded, true);
const openingCandidatesVisible = time => options.computed.showOpeningVwapCandidates.call({now: Date.parse(time) / 1000});
assert.equal(openingCandidatesVisible('2026-11-17T14:29:59Z'), false);
assert.equal(openingCandidatesVisible('2026-11-17T14:30:00Z'), true);
assert.equal(openingCandidatesVisible('2026-11-17T14:44:59Z'), true);
assert.equal(openingCandidatesVisible('2026-11-17T14:45:00Z'), false);
assert.equal(openingCandidatesVisible('2026-07-21T13:30:00Z'), true);
assert.equal(openingCandidatesVisible('2026-11-21T14:35:00Z'), false);
const nav = fs.readFileSync(path.join(__dirname, '../console/static/index.html'), 'utf8')
  .split('<nav class="workspace-nav"')[1].split('</nav>')[0];
assert.deepEqual(Array.from(nav.matchAll(/<button :aria-pressed="bottomTab === '([^']+)'"/g), match => match[1]).slice(0, 2),
  ['rhythm', 'vwap1m']);
const spoken = [];
const entryResults = {
  ...options.methods, lastExternalEntryResult: {}, speak: text => spoken.push(text),
};
const externalResults = [
  { id: 'old', ts: 100, symbol: 'AMD', kind: 'closed', qty: 8 },
  { id: 'cancel', ts: 200, symbol: 'NVDA', kind: 'cancelled' },
  { id: 'partial', ts: 200, symbol: 'AMD', kind: 'partial', qty: 2 },
  { id: 'close', ts: 201, symbol: 'QQQ', kind: 'closed', qty: 5 },
];
entryResults.checkExternalEntryResults(externalResults, 202);
entryResults.checkExternalEntryResults(externalResults, 203);
entryResults.lastExternalEntryResult = {};
entryResults.checkExternalEntryResults(externalResults, 204);
assert.deepEqual(spoken, ['N V D A 外部挂单已自动撤销。Q Q Q 外部持仓已自动平仓，成交 5 股']);
const failurePopups = [];
const strategyNotices = {
  ...options.methods, ...initial, $notify: notice => failurePopups.push(notice),
};
const failure = {id: 'failed-order', event: 'pyramid_order_failed', symbol: 'NASDAQ:SPCX', error: '风险额度不足'};
strategyNotices.autoNotices = [{...failure, id: 'historical-order'}];
strategyNotices.checkAutoNotices();
assert.equal(speechQueue.length, 0);
strategyNotices.autoNotices.push(failure);
strategyNotices.checkAutoNotices();
strategyNotices.checkAutoNotices();
assert.equal(failurePopups.length, 1);
assert.match(failurePopups[0].message, /SPCX.*风险额度不足/);
assert.equal(speechQueue.length, 1);
assert.match(speechQueue[0].text, /S P C X 自动策略下单失败/);
strategyNotices.speak('普通通知');
assert.equal(speechCancels, 1);
assert.equal(speechQueue.length, 2);
speechQueue[0].onerror();
strategyNotices.speak('后续普通通知');
assert.equal(strategyNotices.prioritySpeechCount, 0);
assert.equal(speechCancels, 2);
assert.equal(speechQueue[0].text, '后续普通通知');
const breakout = {id: 'breakout', ticker: 'SPCX', side: 'long', status: 'preview'};
strategyNotices.updateOpeningBreakouts([breakout]);
const failedBreakout = {...breakout, status: 'submission_failed'};
strategyNotices.updateOpeningBreakouts([failedBreakout]);
strategyNotices.updateOpeningBreakouts([failedBreakout]);
assert.equal(failurePopups.length, 2);
assert.equal(speechQueue.length, 1);
assert.match(speechQueue[0].text, /S P C X 开盘小区间突破下单失败/);
speechQueue[0].onend();
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
ui.selectWorkspace('execution'); assert.equal(ui.bottomTab, 'orders'); assert.equal(ui.workspaceExpanded, true);
assert.equal(typeof ui.toggleWorkspace, 'undefined');
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
for (const label of ['方向 / 状态','股数 / 占比','开仓 / 止损','开仓时间 · 美东','平仓时间 · 美东','平仓原因']) assert.ok(orderPage.includes(label));

// A mixed live/confirmed ST alert must show both, without claiming the live ST flipped short.
flipUI.lastScanAlert={};flipUI.scanAlerts=[{ticker:'QCOM',side:'long',qty:608,
  confirmed_direction:-1,realtime_direction:1,confirmed_bar_time:1793901900,scanned_at:2}];
flipUI.checkScanAlert();
assert.match(flipNotices.at(-1).message,/实时ST 多头.*收盘确认ST 空头/);

{
  const range = require('../console/static/range-distribution');
  const time = Date.parse('2026-11-05T09:45:00-05:00') / 1000;
  function quote(close, open, speed, ema7 = close) {
    return {
      previous_regular_range: {day: '2026-11-05', date: '2026-11-04', high: 120, low: 80},
      bar: {time, close, open}, e9: {time, ema_slope_atr: speed, ema_9: ema7},
      attack: {time, pmh: 106, pml: 95, opening3High: 105, opening3Low: 93},
    };
  }
  const wrong = quote(108, 109, 2);
  wrong.e9.time -= 300;
  const map = range.build({AMD: quote(106, 100, -1), NVDA: quote(108, 109, 2),
    COIN: quote(90, 95, -3.4), WRONG: wrong}, ['AMD', 'NVDA', 'COIN', 'WRONG']);
  assert.deepEqual(map.rows.map(r => [r.ticker, r.minor, r.tone]),
    [['AMD', 1, 'flat'], ['NVDA', 0, 'down'], ['COIN', 2, 'down']]);
  const zoneMarket = {
    ABOVE: quote(121, 120, 2, 108), INSIDE: quote(100, 99, 1, 108),
    BELOW: quote(79, 80, -1, 108), HIGH_EDGE: quote(120, 119, 0),
    LOW_EDGE: quote(80, 81, 0),
  };
  const zoneMap = range.build(zoneMarket, Object.keys(zoneMarket), 820, 760);
  assert.deepEqual(zoneMap.groups.map(group => [group.name, group.count]),
    [['昨高上方', 1], ['昨日区间内', 3], ['昨低下方', 1]]);
  for (const major of [0, 1, 2]) {
    const focused = range.build(zoneMarket, Object.keys(zoneMarket), 820, 760, major);
    assert.deepEqual(focused.visibleGroups.map(group => group.major), [major]);
    assert.equal(focused.visibleRows.length, zoneMap.groups[major].count);
    assert.equal(focused.rows.length, zoneMap.rows.length);
    const [panel] = focused.visibleGroups;
    assert.deepEqual([panel.frameX, panel.frameY, panel.frameWidth, panel.frameHeight], [0, 0, 820, 760]);
    assert.ok(focused.visibleRows.every(row => row.major === major && row.x >= panel.left && row.x <= panel.right));
    assert.deepEqual(range.referenceLines(focused.rows.find(row => row.major !== major), focused), []);
  }
  const bySymbol = Object.fromEntries(zoneMap.rows.map(row => [row.symbol, row]));
  assert.equal(bySymbol.ABOVE.major, 0);
  assert.equal(bySymbol.INSIDE.major, 1);
  assert.equal(bySymbol.BELOW.major, 2);
  assert.equal(bySymbol.HIGH_EDGE.major, 1);
  assert.equal(bySymbol.LOW_EDGE.major, 1);
  assert.equal(bySymbol.ABOVE.normalizedPosition, bySymbol.BELOW.normalizedPosition);
  assert.ok(bySymbol.INSIDE.anchorX > bySymbol.ABOVE.anchorX);
  assert.ok(bySymbol.INSIDE.anchorX > bySymbol.BELOW.anchorX);
  assert.ok(bySymbol.ABOVE.anchorY < bySymbol.BELOW.anchorY);
  assert.ok(zoneMap.groups[1].bottom - zoneMap.groups[1].top > zoneMap.groups[0].bottom - zoneMap.groups[0].top);
  assert.equal(zoneMap.groups[0].left, zoneMap.groups[2].left);
  for (const row of zoneMap.rows) {
    const group = zoneMap.groups[row.major];
    assert.ok(row.y >= group.top && row.y <= group.bottom);
    assert.ok(row.x >= group.left && row.x <= group.right);
  }
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
  assert.ok(layout.rows.some(row => row.displaced));
  for (const row of layout.rows) {
    assert.ok(row.x >= layout.bounds.left && row.x <= layout.bounds.right);
    assert.ok(row.y >= layout.bounds.top && row.y <= layout.bounds.bottom);
  }
  assert.deepEqual(layout.rows.map(row => row.ticker), ['INSIDE1', 'INSIDE2', 'SLOW', 'MID', 'FAST', 'FAR']);
  const nearBoundary = Object.fromEntries(Array.from({length: 10}, (_, i) =>
    ['NEAR' + i, quote(108, 107, 1, 106.01)]));
  const nearLayout = range.build(nearBoundary, Object.keys(nearBoundary), 360);
  for (const row of nearLayout.rows) {
    assert.equal(row.minor, 0);
    assert.ok(row.y < nearLayout.groups[row.major].upperY, row.ticker);
  }
  const reference = quote(110, 109, 1, 100);
  reference.attack = {time, pmh: 104, pml: 100, opening3High: 103, opening3Low: 101};
  const central = range.build({TEST: reference}, ['TEST']).rows[0];
  assert.equal(central.normalizedPosition, -1);
  assert.equal(central.tone, 'flat'); // The close is outside, but Pine EMA7 is still at the boundary.
  reference.e9.ema_9 = 110;
  const advanced = range.build({TEST: reference}, ['TEST']);
  assert.equal(advanced.rows[0].normalizedPosition, 4);
  assert.equal(advanced.rows[0].distance, 3);
  reference.e9.ema_9 = 98;
  const down = range.build({TEST: reference}, ['TEST']).rows[0];
  assert.equal(down.normalizedPosition, -2);
  assert.equal(down.distance, 1);
  const unequalPrice = quote(220, 218, 1, 220);
  unequalPrice.attack = {time, pmh: 208, pml: 200, opening3High: 206, opening3Low: 202};
  assert.equal(range.build({TEST: unequalPrice}, ['TEST']).rows[0].normalizedPosition, 4);
  const zeroRange = quote(100, 100, 0);
  zeroRange.attack.pmh = zeroRange.attack.pml = zeroRange.attack.opening3High = zeroRange.attack.opening3Low = 100;
  const missing = quote(110, 109, 1);
  delete missing.e9.ema_9;
  assert.deepEqual(range.build({ZERO: zeroRange, MISSING: missing}, ['ZERO', 'MISSING']).pending, ['ZERO', 'MISSING']);
  const fallback = quote(110, 109, 1);
  fallback.bar_c = {...fallback.bar};
  fallback.e9_c = {...fallback.e9};
  fallback.attack_c = {...fallback.attack};
  fallback.e9.time -= 300;
  assert.equal(range.build({TEST: fallback}, ['TEST']).rows[0].confirmed, true);
  const selected = {...advanced.rows[0], prior: {high: 108, low: 96}};
  assert.deepEqual(range.referenceLines(selected, advanced).map(line => [line.label, line.position, line.tone]),
    [['昨高', 3, 'up'], ['昨低', -3, 'down']]);
  const early = quote(109, 108, 1);
  early.bar.time = Date.parse("2026-11-05T14:40:00Z") / 1000;
  early.e9.time = early.attack.time = early.bar.time;
  early.attack.opening3High = 110;
  early.attack.opening3Low = 93;
  const provisional = range.build({EARLY: early}, ['EARLY']).rows[0];
  assert.equal(provisional.forming, true);
  assert.equal(provisional.target, null);
  assert.equal(provisional.ready, false);
  assert.deepEqual([provisional.lower, provisional.upper], [95, 106]);
  early.bar.time += 300;
  early.e9.time = early.attack.time = early.bar.time;
  const complete = range.build({EARLY: early}, ['EARLY']).rows[0];
  assert.equal(complete.forming, false);
  assert.deepEqual([complete.lower, complete.upper], [93, 110]);
}

// Entry-feed folding is local presentation; signal objects and execution are untouched.
const signalRows = [{time:100,rows:[{kind:'golden',row:{id:'keep'}}]}];
let scrollAfterUnfold = 0;
const foldedFeed = {...options.methods, openingFeedCollapsed:false, openingFeedPreferenceSet:false,
  workspaceLayouts:null, entrySignalGroups:signalRows, $nextTick:fn=>fn(),
  scrollOpeningFeedToLatest:()=>{scrollAfterUnfold++;}};
foldedFeed.onBottomTabChange('rhythm');
assert.equal(foldedFeed.openingFeedCollapsed,true);
foldedFeed.toggleOpeningFeed();
assert.equal(foldedFeed.openingFeedCollapsed,false);
assert.equal(scrollAfterUnfold,1);
foldedFeed.onBottomTabChange('orders');
assert.equal(foldedFeed.openingFeedCollapsed,false);
foldedFeed.toggleOpeningFeed();
assert.equal(foldedFeed.openingFeedCollapsed,true);
assert.equal(foldedFeed.entrySignalGroups,signalRows);
assert.equal(storage.get('opening_feed_collapsed'),'1');
