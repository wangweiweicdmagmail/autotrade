'use strict';

const assert = require('node:assert/strict');
const { once, EventEmitter } = require('node:events');
const { test } = require('node:test');
const { createEma7Shadow } = require('../service/pinets_shadow');
const { createCalculator, getDefinition } = require('../service/pinets_runtime');

const symbol = 'NASDAQ:MSFT';
const timezone = 'America/New_York';
const metadata = { timezone, minmove: 1, pricescale: 100, session: 'extended' };
const infos = { ...metadata, subsession_id: 'extended' };
const version = require('../package-lock.json').packages['node_modules/pinets'].version;
const start = Date.parse('2026-11-04T14:30:00Z') / 1000;
function bar(index) {
  const open = 150 + index * 0.01;
  return { time: start + index * 300, open, high: open + 0.2, low: open - 0.2,
    close: open + 0.05, volume: 1000 };
}
const bars = Array.from({ length: 601 }, (_, index) => bar(index - 520));
const daily = Array.from({ length: 12 }, (_, index) => ({
  ...bar(index), time: start + (index - 10) * 86400,
}));

test('the same local dataset can run all seven pinned Pine scripts', async () => {
  const dataset = { symbol, metadata, bars, daily };
  for (const key of ['e9', 'e20', 'e7v2', 'trend', 'fvg', 'smart', 'attack']) {
    const definition = getDefinition(key);
    const calculate = createCalculator(definition, { version });
    const [output] = await calculate(dataset, { tail: 1 });
    assert.equal(output.time, bars.at(-1).time, key);
    assert.deepEqual(Object.keys(output).sort(), ['time', ...Object.keys(definition.plots)].sort(), key);
    assert.ok(Object.entries(output).some(([field, value]) => field !== 'time' && Number.isFinite(value)), key);
  }
  const attack = createCalculator(getDefinition('attack'), { version });
  await assert.rejects(attack({ symbol, metadata, bars }), /需要日线/);
});

test('the real worker keeps confirmed EMA7 stable during intrabar updates and advances at rollover', async t => {
  const events = new EventEmitter();
  const records = [];
  const shadow = createEma7Shadow({ record(row) { records.push(row); events.emit('row', row); } });
  t.after(() => shadow.close());
  async function wait(kind, time, predicate = () => true) {
    const matches = row => row.kind === kind && row.bar_time === time && predicate(row);
    const signal = AbortSignal.timeout(8000);
    while (!records.some(matches)) await once(events, 'row', { signal });
    return records.find(matches);
  }
  const current = bars.at(-1), prior = bars.at(-2);
  shadow.observePrice(symbol, [...bars].reverse(), infos);
  const confirmed = await wait('confirmed', prior.time, r => r.status === 'ready');
  const realtime = await wait('realtime', current.time, r => r.status === 'ready');
  assert.equal(realtime.last_confirmed_time, prior.time);
  assert.equal(confirmed.pinets.time, prior.time);
  assert.equal(confirmed.comparison, null);
  shadow.observeStudy(symbol, realtime.pinets, confirmed.pinets);
  const compared = await wait('confirmed', prior.time, r => r.type === 'ema7_shadow_comparison');
  assert.equal(compared.comparison, 'confirmed_bar');
  assert.deepEqual(compared.differences, {});

  const revised = { ...current, close: current.close - 0.01, volume: 1200 };
  shadow.observePrice(symbol, [revised, ...bars.slice(0, -1).reverse()], infos);
  const revision = await wait('realtime', current.time, r => r.status === 'ready' && r.revision === 2);
  assert.equal(revision.last_confirmed_time, prior.time);
  assert.equal(records.filter(r => r.kind === 'confirmed' && r.type === 'ema7_shadow').length, 1);

  const next = bar(81);
  shadow.observePrice(symbol, [next, revised, ...bars.slice(0, -1).reverse()], infos);
  const nextConfirmed = await wait('confirmed', current.time, r => r.status === 'ready');
  assert.deepEqual(nextConfirmed.pinets, revision.pinets);
  const nextRealtime = await wait('realtime', next.time, r => r.status === 'ready');
  assert.equal(nextRealtime.last_confirmed_time, current.time);
});

test('late realtime results and results from a rebuilt chart cannot become ready', async t => {
  const records = [];
  const jobs = [];
  const worker = new EventEmitter();
  worker.postMessage = job => jobs.push(job);
  worker.terminate = async () => {};
  const shadow = createEma7Shadow({ record: row => records.push(row), makeWorker: () => worker });
  t.after(() => shadow.close());
  function complete() {
    const job = jobs.shift();
    worker.emit('message', { output: { time: job.barTime, entry_signal: 1,
      direction: 1, entry_price: 150, long_stop: 149, short_stop: null } });
  }
  const current = bars.at(-1);
  shadow.observePrice(symbol, [...bars].reverse(), infos);
  complete();
  const updated = { ...current, volume: 5000 };
  shadow.observePrice(symbol, [updated, ...bars.slice(0, -1).reverse()], infos);
  complete();
  assert.equal(records.at(-1).status, 'superseded');
  shadow.reset(symbol);
  complete();
  assert.equal(records.at(-1).status, 'superseded');
  assert.equal(records.filter(r => r.kind === 'realtime' && r.status === 'ready').length, 0);
});

test('a failed confirmed calculation retries even when the candle is unchanged', async t => {
  const jobs = [];
  const worker = new EventEmitter();
  worker.postMessage = job => jobs.push(job);
  worker.terminate = async () => {};
  const shadow = createEma7Shadow({ record() {}, makeWorker: () => worker });
  t.after(() => shadow.close());
  const periods = [...bars].reverse();
  shadow.observePrice(symbol, periods, infos);
  const first = jobs.shift();
  assert.equal(first.kind, 'confirmed');
  worker.emit('message', { error: 'temporary failure' });
  const realtime = jobs.shift();
  assert.equal(realtime.kind, 'realtime');
  worker.emit('message', { output: { time: realtime.barTime, entry_signal: 0,
    direction: 1, entry_price: 150, long_stop: 149, short_stop: null } });

  shadow.observePrice(symbol, periods, infos, Date.now() + 1001);
  const retried = jobs.shift();
  assert.equal(retried.kind, 'confirmed');
  assert.equal(retried.barTime, first.barTime);
  assert.ok(retried.revision > first.revision);
  assert.equal(jobs.length, 0);
});
