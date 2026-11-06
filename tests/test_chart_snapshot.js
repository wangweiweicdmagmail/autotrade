'use strict';
const test=require('node:test');
const assert=require('node:assert/strict');
const {createPriceUpdater,isPriceUpdate,chartSession}=require('../service/chart_snapshot');
const {priorBodyEma10}=require('../service/candle_metrics');
const T=Date.parse('2026-11-05T10:00:00-05:00')/1000;
function rows(){return Array.from({length:180},(_,i)=>({time:T-i*300,open:100+i,close:101+i,max:102+i,min:99+i,volume:10}));}
test('指标事件不重算价格；根内只更新实时值，闭合历史保留',()=>{
  assert.equal(isPriceUpdate(['study_x']),false);
  assert.equal(isPriceUpdate(['$prices','study_x']),true);
  let calls=0;
  const update=createPriceUpdater(()=>{calls++;return 2;});
  const state={};const ps=rows();
  update(state,ps);
  const history=state.breakoutBars,closed=state.bar_c;
  const initial=calls;
  ps[0]={...ps[0],close:101.7,volume:20};
  update(state,ps);
  assert.equal(calls,initial+1);
  assert.equal(state.breakoutBars,history);
  assert.equal(state.bar_c,closed);
  assert.equal(state.bar.close,101.7);
  assert.equal(state.bar.body_ema10,priorBodyEma10(ps,0));
});
test('历史价格修正和新根切换均刷新缓存，盘前预载新增也不能丢',()=>{
  const update=createPriceUpdater(()=>2),state={},ps=rows();
  update(state,ps);const before=state.breakoutBars;
  ps[2]={...ps[2],close:100.2};update(state,ps);
  assert.notEqual(state.breakoutBars,before);
  assert.equal(state.breakoutBars[1][4],100.2);
  const old=state.breakoutBars;
  ps.push({time:T-181*300,open:100,close:110,max:111,min:99});update(state,ps);
  assert.notEqual(state.breakoutBars,old);
  ps.unshift({time:T+300,open:101,close:102,max:103,min:100});update(state,ps);
  assert.equal(state.bar_c.time,T);
});

test('24h requires the exact session reported by TradingView', () => {
  for (const subsession_id of [undefined, 'extended', '24x7', 'regular']) {
    assert.equal(chartSession({subsession_id}, '24h').accepted, false);
  }
  assert.equal(chartSession({subsession_id: '24h'}, '24h').accepted, true);
  assert.equal(chartSession({subsession_id: '24h'}, '24h', 'series error').accepted, false);
  assert.equal(chartSession({}, 'extended').accepted, true);
});

test('provider default does not require a named subsession, but requires a resolved symbol', () => {
  assert.equal(chartSession({}, 'default').accepted, false);
  for (const subsession_id of [undefined, 'regular']) {
    const infos = { full_name: 'CAPITALCOM:NAS100', subsession_id };
    const session = chartSession(infos, 'default');
    assert.equal(session.accepted, true);
    assert.equal(session.resolved, subsession_id ?? null);
    assert.equal(chartSession(infos, 'default', 'series error').accepted, false);
    assert.equal(chartSession(infos, '24h').accepted, false);
  }
});

test('yesterday levels use the previous regular trading day and skip today', () => {
  const { previousRegularRange } = require('../service/chart_snapshot');
  const friday = Date.parse('2026-10-30T09:30:00-04:00') / 1000;
  const monday = Date.parse('2026-11-02T09:30:00-05:00') / 1000;
  const daily = [{time: friday, high: 110, low: 90}, {time: monday, high: 115, low: 95}];
  assert.deepEqual(previousRegularRange(daily, monday - 3600),
    {day: '2026-11-02', date: '2026-10-30', high: 110, low: 90});
  assert.equal(previousRegularRange(daily, monday + 3600).high, 110);
});
