'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const { parseArgs, snapshot } = require('../service/pine_paper_daemon');
const model = require('../service/pine_models').loadPineModels()[0];

test('night session is explicit and never silently falls back', () => {
  assert.equal(parseArgs(['node', 'x']).session, '24h');
  assert.throws(() => parseArgs(['node', 'x', '--session', '24x7']));
  assert.throws(() => parseArgs(['node', 'x', '--symbol', 'NVDA']));
});

test('local Pine and OHLC align by timestamp and retain actual resolved source', () => {
  const data = snapshot({infos:{subsession_id:'extended',full_name:'BATS:NVDA'}}, {
    base:{bar:{time:1300,high:103},bar_c:{time:1000},paperBars:[]},observedAt:1500000,
    outputs:{e9:[{time:1600,touch:1},{time:1300,direction:1,touch:0,ema_9:101,long_stop:99},
      {time:1000,direction:-1,touch:1,ema_9:101,short_stop:103}]},
  },model,'24h');
  assert.equal(data.session.requested,'24h');
  assert.equal(data.session.resolved,'extended');
  assert.equal(data.session.symbol,'BATS:NVDA');
  assert.equal(data.session.source,'pinets');
  assert.equal(data.session.tv_study_subscriptions,0);
  assert.equal(data.bar.high,103);
  assert.equal(data.e9.time,1300);
  assert.equal(data.e9.touch,0);
  assert.equal(data.e9_c.time,1000);
  assert.equal(data.e9_c.direction,-1);
});

test('night UI submits only to the independent paper service and blocks double clicks', async () => {
  let options, release;
  const requests = [];
  vm.runInNewContext(fs.readFileSync('console/static/app.js', 'utf8'), {
    Vue: { createApp(value) { options = value; return { use() { return { mount() {} }; } }; } },
    ElementPlus: {}, localStorage: { getItem() { return null; } },
    fetch: async (url, request) => { requests.push({ url, request }); await new Promise(resolve => { release = resolve; }); return { ok: true, json: async () => ({ running: true }) }; },
  });
  const ui = { ...options.methods, nightBusy: false, nightForm: { risk_budget: 50, window_start: '20:00', window_end: '04:00' }, tradeHeaders() { return {}; }, checkWrite403() {}, $message: { error(message) { throw Error(message); } } };
  const first = ui.nightAction('start');
  await ui.nightAction('start');
  assert.equal(requests.length, 1);
  assert.equal(requests[0].url, '/api/night-test/start');
  assert.equal(JSON.parse(requests[0].request.body).risk_budget, 50);
  release();
  await first;
  assert.equal(ui.night.running, true);
  assert.equal(ui.nightBusy, false);
});


test('night pane keeps component boundaries so conditional alerts cannot swallow the form', () => {
  const html = fs.readFileSync('console/static/index.html', 'utf8');
  const pane = html.slice(html.indexOf('label="夜盘模拟"'), html.indexOf('label="回测"'));
  assert.doesNotMatch(pane, /<el-[^>]*\/>/);
  assert.match(pane, /人工启动模拟/);
  assert.match(pane, /Pine 导出属性/);
});
