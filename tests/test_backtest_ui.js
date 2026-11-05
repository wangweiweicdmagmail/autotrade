// Standalone frontend verification: no browser, credentials or trading endpoints.
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
    requests.push({ url, body: JSON.parse(request.body) });
    return { ok: true, json: async () => ({ done: true, marker: 'private-result' }) };
  },
});
const ui = { ...options.methods, wbModel: 'fixture_pullback', autoModel: 'ema9_pyramid',
  wbSelectedModel: { execution_profile: 'single_entry' }, wbSymbol: 'NVDA', wbDay: '2026-10-30',
  wbStartTime: '10:32', wbEquity: 100000, wbRiskUsd: 100, equity: 900000, riskUsd: 999,
  wbLoading: false, wb: { stale: true } };
(async () => {
  // Concurrent clicks issue one atomic request and never call load/step/run_all.
  await Promise.all([ui.wbRun(), ui.wbRun()]);
  assert.equal(requests.length, 1);
  assert.equal(requests[0].url, '/api/bt/run');
  assert.equal(requests[0].body.start_time, '10:32');
  assert.equal(requests[0].body.strategy, 'fixture_pullback');
  assert.deepEqual(requests[0].body.params, { initial_risk_usd: 100, equity: 100000 });
  assert.equal(ui.wb.done, true);
  assert.equal(ui.wbLoading, false);
  assert.equal(ui.wbDirectionText(0), '中性 / 未知');
  ui.wbStartTime = '25:00'; await ui.wbRun(); assert.equal(requests.length, 1);
  const template = fs.readFileSync(path.join(__dirname, '../console/static/index.html'), 'utf8');
  assert.ok(template.includes('@click="wbRun"'));
  assert.ok(!template.includes('@click="wbStep'));
  assert.ok(!template.includes('@click="wbLoad'));
  assert.ok(!template.includes('@click="wbRunAll'));
  console.log('Atomic backtest UI, manual startup, parameter isolation and click guard passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
