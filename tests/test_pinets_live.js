'use strict';

const assert = require('node:assert/strict');
const { EventEmitter, once } = require('node:events');
const { test } = require('node:test');
const { createLocalIndicators } = require('../service/pinets_live');
const { createCalculator, getDefinition } = require('../service/pinets_runtime');
const pinetsVersion = require('../package.json').dependencies.pinets;

const symbol = 'NASDAQ:MSFT';
const start = Date.parse('2026-11-04T14:30:00Z') / 1000;
const infos = { timezone: 'America/New_York', minmov: 1, pricescale: 100, subsession_id: 'extended' };

function candle(index) {
  const open = 150 + index * 0.01;
  return { time: start + index * 300, open, high: open + 0.2, low: open - 0.2,
    close: open + 0.05, volume: 1000 };
}
const candles = Array.from({ length: 601 }, (_, i) => candle(80 - i));
const daily = Array.from({ length: 12 }, (_, i) => ({ ...candle(i), time: start + (i - 10) * 86400 }));
function raw(rows) { return { bar: rows[0], bar_c: rows[1] }; }

test('EMA7 v2 signals on an intrabar touch even after the latest price leaves the line', async () => {
  const calculate = createCalculator(getDefinition('e7v2'), { version: pinetsVersion });
  for (const direction of [1, -1]) {
    const bars = Array.from({ length: 80 }, (_, i) => ({ ...candle(direction * i * 10), time: start + i * 300 }));
    const next = { ...candle(direction * 800), time: start + 80 * 300 };
    const metadata = { ...infos, minmove: infos.minmov, session: 'extended' };
    const current = async bar => (await calculate({ symbol, metadata, bars: [...bars, bar] }, { tail: 1 }))[0];
    const { entry_price: line } = await current(next);
    const open = line + direction * 0.15;
    const close = line + direction * 0.10;
    const extreme = line + direction * 0.02;
    const untouched = { ...next, open, close, high: Math.max(open, extreme), low: Math.min(open, extreme) };
    const before = await current(untouched);
    assert.equal(before.direction, direction);
    assert.equal(before.entry_signal, 0);

    const touchField = direction === 1 ? 'low' : 'high';
    for (const touchPrice of [line, line - direction * 0.04]) {
      const touched = await current({ ...untouched, [touchField]: touchPrice });
      assert.equal(touched.direction, direction);
      assert.equal(touched.entry_price, line);
      assert.equal(touched.entry_signal, 1);
    }
    const stop = direction === 1 ? before.long_stop : before.short_stop;
    const beyondStop = stop - direction * 0.01;
    assert.equal((await current({ ...untouched, [touchField]: beyondStop, close: beyondStop })).entry_signal, 0);
  }
});

test('production worker returns all seven indicators against the same realtime and confirmed bars', async t => {
  const events = new EventEmitter();
  const local = createLocalIndicators({ workers: 1, onResult: (...args) => events.emit('result', ...args) });
  t.after(() => local.close());
  const frame = raw(candles);
  const at = frame.bar.time * 1000 + 500;
  const result = once(events, 'result', { signal: AbortSignal.timeout(6000) });
  local.observe(symbol, candles, infos, frame, daily, at);
  const [, { outputs }] = await result;
  assert.deepEqual(Object.keys(outputs).sort(), ['attack', 'e20', 'e7v2', 'e9', 'fvg', 'smart', 'trend']);
  for (const rows of Object.values(outputs)) {
    assert.equal(rows[0].time, frame.bar.time);
    assert.equal(rows[1].time, frame.bar_c.time);
  }
  const snapshot = local.snapshot(symbol, frame, at);
  assert.equal(snapshot.observedAt, at);
  assert.deepEqual(snapshot.outputs.e7v2, outputs.e7v2);
  const expired = local.snapshot(symbol, frame, at + 300000);
  assert.equal(expired.outputs.e7v2[0], null);
  assert.equal(expired.outputs.e7v2[1].time, frame.bar_c.time);

  const next = [candle(81), ...candles.slice(0, -1)];
  const nextFrame = raw(next);
  const nextResult = once(events, 'result', { signal: AbortSignal.timeout(6000) });
  local.observe(symbol, next, infos, nextFrame, daily, at + 300000);
  const [, { outputs: advanced }] = await nextResult;
  assert.deepEqual(Object.keys(advanced).sort(), Object.keys(outputs).sort());
  for (const rows of Object.values(advanced)) {
    assert.equal(rows[0].time, nextFrame.bar.time);
    assert.equal(rows[1].time, nextFrame.bar_c.time);
  }
});

test('new bars and rebuilt charts cannot consume a delayed realtime result', async t => {
  const fake = new EventEmitter();
  let latestJob;
  fake.postMessage = job => { latestJob = job; };
  fake.terminate = async () => {};
  const local = createLocalIndicators({ workers: 1, makeWorker: () => fake });
  t.after(() => local.close());
  const at = candles[0].time * 1000 + 500;
  local.observe(symbol, candles, infos, raw(candles), daily, at);
  const resultFor = job => ({
    results: { e7v2: job.dataset.bars.slice(-2).reverse().map(bar => ({ time: bar.time, entry_signal: 1 })) },
  });
  const old = latestJob;
  const next = [candle(81), ...candles];
  local.observe(symbol, next, infos, raw(next), daily, at + 300000);
  fake.emit('message', resultFor(old));
  assert.equal(local.snapshot(symbol, raw(next), at + 300000).outputs.e7v2, undefined);
  const pending = latestJob;
  local.reset(symbol);
  fake.emit('message', resultFor(pending));
  assert.deepEqual(local.snapshot(symbol, raw(next), at + 300000).outputs, {});
});
