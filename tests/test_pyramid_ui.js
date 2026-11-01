// Run with: node tests/test_pyramid_ui.js. No browser, network or trading backend.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
let options;
const requests = [];
vm.runInNewContext(fs.readFileSync(path.join(__dirname, '../console/static/app.js'), 'utf8'), {
  Vue: { createApp(value) { options = value; return { use() { return { mount() {} }; } }; } },
  ElementPlus: {},
  fetch: async (url, request) => {
    requests.push({ url, request });
    return { ok: true, json: async () => ({ ok: true }) };
  },
});
const ui = { ...options.methods, pyramidAddUnlockR: 2 };
const active = { state: 'ACTIVE', entries: 2, enabled: true, unlocked: true, second_mfe_r: 0.5 };
assert.match(ui.autoNext(active), /第二仓达到1R/);
assert.match(ui.autoNext({ ...active, state: 'PENDING', pending_lot_id: 'add' }), /实际成交/);
assert.match(ui.autoNext({ ...active, state: 'CLOSING' }), /退出中/);
assert.match(ui.autoNext({ ...active, enabled: false }), /已暂停/);
assert.equal(ui.autoNext({ ...active, recovery_block_reason: '缺少首仓风险基准' }), '缺少首仓风险基准');
assert.match(ui.autoNext({ ...active, state: 'DONE' }), /已结束/);
assert.match(ui.autoRiskText({ entries: 2 }), /风险记录未提供/);
const formatted = ui.autoRiskText({ last_order_budget: 200, last_order_risk: 198,
  last_order_qty: 66, last_order_stop: 99 });
assert.match(formatted, /预算.*200.*计划风险.*198/);
assert.match(formatted, /66股/);
assert.match(ui.orderRiskText({ signal_snapshot: { risk_budget: 200, planned_risk_usd: 198 } }), /预算.*200.*计划.*198/);
assert.match(ui.autoNext({ ...active, entries: 1, risk_budgets: [100] }), /仅管理/);
ui.expandedAutoOrders = {};
ui.viewAutoOrders({ inst_id: 'model-a:NVDA', symbol: 'NASDAQ:NVDA' });
ui.viewAutoOrders({ inst_id: 'model-b:NVDA', symbol: 'NASDAQ:NVDA' });
assert.equal(ui.expandedAutoOrders['model-a:NVDA'], true);
assert.equal(ui.expandedAutoOrders['model-b:NVDA'], true);
ui.lots = [{ lot_id: 'a', symbol: 'NASDAQ:NVDA' }, { lot_id: 'b', symbol: 'NASDAQ:NVDA' }];
assert.deepEqual(ui.autoLotRows({ symbol: 'NASDAQ:NVDA', lot_ids: ['a'] }), [ui.lots[0]]);
assert.equal(ui.autoLotRows({ symbol: 'NASDAQ:NVDA', lot_ids: [] }).length, 0);
ui.tradeHeaders = () => ({});
ui.$message = { success() {}, error(error) { throw new Error(error); } };
(async () => {
  await ui.stopAutoInstance({ symbol: 'NASDAQ:NVDA', strategy: 'fixture_model', strategy_instance_id: 'second' }, false);
  assert.match(requests[0].url, /strategy=fixture_model&instance_id=second/);
  console.log('Pyramid UI state, ownership and risk display checks passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
