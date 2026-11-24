'use strict';

const assert = require('node:assert/strict');
const { EventEmitter, once } = require('node:events');
const { test } = require('node:test');
const { createLocalIndicators } = require('../service/pinets_live');
const { createCalculator, getDefinition } = require('../service/pinets_runtime');
const pinetsVersion = require('../package.json').dependencies.pinets;
const fs = require('node:fs');
const path = require('node:path');

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

test('EMA7 v2 extrapolates signed EMA velocity and ATR offset for both directions', async () => {
  const definition = getDefinition('e7v2');
  definition.plots = { ...definition.plots, actual_ema: '实际 EMA7', forecast: '预测重心' };
  const sources = [
    definition.source,
    fs.readFileSync(path.join(__dirname, '../indicators/ema7-autotrade-v2.pine'), 'utf8'),
  ];
  for (const direction of [1, -1]) {
    for (const rebound of [false, true]) {
      const bars = Array.from({ length: 85 }, (_, i) => ({
        ...candle(direction * i * 10), time: start + i * 300,
      }));
      if (rebound) {
        for (let i = 81; i < bars.length; i++) {
          const price = bars[80].close - direction * (i - 80) * 0.2;
          bars[i] = { ...bars[i], open: price, high: price + 0.2, low: price - 0.2, close: price };
        }
      }
      const dataset = { symbol, metadata: { ...infos, minmove: infos.minmov, session: 'extended' }, bars };
      for (const source of sources) {
        for (const offsetMultiplier of [0, 0.2]) {
          const calculate = createCalculator(definition, { version: pinetsVersion, source, inputs: { offsetMultiplier } });
          const rows = await calculate(dataset, { tail: 5 });
          const output = rows.at(-1);
          const previous = rows.at(-2).actual_ema;
          const velocity = (previous - rows.at(-5).actual_ema) / 3;
          const expected = previous + velocity + Math.sign(velocity) * offsetMultiplier * output.atr;
          assert.equal(output.direction, direction);
          assert.equal(Math.sign(velocity), rebound ? -direction : direction);
          assert.ok(Math.abs(output.forecast - previous - velocity) < 1e-9);
          assert.ok(Math.abs(output.entry_price - expected) <= 0.005001);
          if (rebound) assert.equal(output.entry_signal, 0);

          // A current-bar jump may flip ST, but cannot change the closed-bar forecast.
          const last = bars.at(-1);
          const jump = { ...last, high: last.high + 20, close: last.high + 20 };
          const changed = (await calculate({ ...dataset, bars: [...bars.slice(0, -1), jump] }, { tail: 1 }))[0];
          assert.equal(changed.entry_price, output.entry_price);
          assert.equal(changed.forecast, output.forecast);
        }
      }
    }
  }
});

test('EMA7 v2 stop lines tighten only in the active direction and restart on reversal', async () => {
  const definition = getDefinition('e7v2');
  definition.plots = { ...definition.plots, long_line: '多头止损线', short_line: '空头止损线' };
  const increments = [
    ...Array(65).fill(0.1), ...Array(4).fill(-0.2), ...Array(15).fill(0.1),
    ...Array(65).fill(-0.1), ...Array(4).fill(0.2), ...Array(15).fill(-0.1),
    ...Array(45).fill(0.1),
  ];
  let price = 150;
  const bars = increments.map((increment, i) => {
    price += increment;
    return { ...candle(0), time: start + i * 300,
      open: price, high: price + 0.2, low: price - 0.2, close: price };
  });
  const calculate = createCalculator(definition, { version: pinetsVersion });
  const metadata = { ...infos, minmove: infos.minmov, session: 'extended' };
  const rows = await calculate({ symbol, metadata, bars }, { tail: bars.length });
  const tightened = new Set();
  let reversals = 0;
  for (let i = 1; i < rows.length; i++) {
    const row = rows[i];
    if (!(row.entry_price > 0) || !row.direction) continue;
    const previous = rows[i - 1];
    const long = row.direction === 1;
    const key = long ? 'long_line' : 'short_line';
    const candidate = Math.round((row.entry_price - row.direction * 1.2 * row.atr) * 100) / 100;
    const sameSegment = previous.direction === row.direction && Number.isFinite(previous[key]);
    const expected = sameSegment
      ? (long ? Math.max(previous[key], candidate) : Math.min(previous[key], candidate))
      : candidate;
    assert.ok(Math.abs(row[key] - expected) < 1e-8, JSON.stringify({ i, row, expected }));
    assert.equal(row[long ? 'short_line' : 'long_line'], null);
    if (sameSegment && expected !== candidate) tightened.add(row.direction);
    if (previous.direction === -row.direction) reversals++;
  }
  assert.equal(tightened.size, 2);
  assert.ok(reversals >= 2);
});

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

test('production worker returns all eight indicators against the same realtime and confirmed bars', async t => {
  const events = new EventEmitter();
  const local = createLocalIndicators({ workers: 1, onResult: (...args) => events.emit('result', ...args) });
  t.after(() => local.close());
  const frame = raw(candles);
  const at = frame.bar.time * 1000 + 500;
  const result = once(events, 'result', { signal: AbortSignal.timeout(6000) });
  local.observe(symbol, candles, infos, frame, daily, at);
  const [, { outputs }] = await result;
  assert.deepEqual(Object.keys(outputs).sort(), ['attack', 'e20', 'e7v2', 'e9', 'fvg', 'golden', 'smart', 'trend']);
  for (const rows of Object.values(outputs)) {
    assert.equal(rows[0].time, frame.bar.time);
    assert.equal(rows[1].time, frame.bar_c.time);
  }
  const snapshot = local.snapshot(symbol, frame, at);
  const confirmed=local.confirmedHistory(symbol,frame);
  assert(confirmed.rows.length>=500);assert.equal(confirmed.rows[0].time,frame.bar_c.time);
  assert.equal(confirmed.bars.length,601);
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
  const corrected=next.map((p,i)=>i===30?{...p,close:p.close+.1,high:p.high+.1}:p);
  const correction=once(events,'result',{signal:AbortSignal.timeout(6000)});
  local.observe(symbol,corrected,infos,raw(corrected),daily,at+300001);
  assert.equal(local.confirmedHistory(symbol,raw(corrected)),null); // No old ATR paired with corrected close history.
  await correction;
  assert(local.confirmedHistory(symbol,raw(corrected))?.rows.length>=500);
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

test('one-minute worker publishes the exact confirmed opening position alongside unchanged VWAP model output',async t=>{
 const events=new EventEmitter();
 const local=createLocalIndicators({intervalSeconds:60,barLimit:500,minBars:10,workers:1,onResult:(...args)=>events.emit('result',...args)});
 t.after(()=>local.close());
 const open=Date.parse('2026-11-18T14:30:00Z')/1000;
 const previous=Array.from({length:200},(_,i)=>({time:open-86400+i*60,open:200,high:201,low:199,close:200,volume:100}));
 const rows=[...previous,...Array.from({length:21},(_,i)=>({time:open+i*60,open:100,high:i<15?101:104,low:99,close:i<15?100:103,volume:100}))].reverse();
 const frame={bar:rows[0],bar_c:rows[1]},time=(open+1200)*1000+500;
 const result=once(events,'result',{signal:AbortSignal.timeout(6000)});
 local.observe(symbol,rows,{...infos,subsession_id:'regular'},frame,[],time);
 const [,{outputs}]=await result;
 assert.deepEqual(Object.keys(outputs).sort(),['opening_position','vwap_pullback']);
 const position=outputs.opening_position[1];assert.equal(position.time,open+1140);
 assert.equal(position.ready,1);assert.equal(position.position,3);assert.equal(position.range_position,3);
 assert.equal(position.range_bars,15);assert.equal(position.opening_start,open);assert.equal(position.range_confirmed_at,open+900);
 const expected=await createCalculator(getDefinition('vwap_pullback'),{version:pinetsVersion})({symbol,
  bars:[...rows].reverse(),metadata:{timezone:infos.timezone,minmove:1,pricescale:100,type:'stock',session:'regular'}},{tail:2});
 assert.deepEqual(outputs.vwap_pullback,[...expected].reverse());
});

test('the production minute worker releases a first 09:45 batch after an early rollover, with no next quote',async t=>{
 const {Worker}=require('node:worker_threads');
 const {createCoalescedUpdater}=require('../service/chart_snapshot');
 const open=Date.parse('2026-11-18T14:30:00Z')/1000;
 const time=new BigInt64Array(new SharedArrayBuffer(8));
 const setTime=value=>Atomics.store(time,0,BigInt(value));
 const clock=()=>Number(Atomics.load(time,0));
 const events=new EventEmitter();
 const local=createLocalIndicators({intervalSeconds:60,barLimit:500,minBars:10,workers:1,
  makeWorker:()=>new Worker(`const {workerData}=require('node:worker_threads');
   const clock=new BigInt64Array(workerData.clock);Date.now=()=>Number(Atomics.load(clock,0));
   require(workerData.module);`,{eval:true,workerData:{clock:time.buffer,module:path.resolve(__dirname,'../service/pinets_live_worker.js')}}),
  onResult:(...args)=>events.emit('result',...args)});
 t.after(()=>local.close());
 const previous=Array.from({length:200},(_,i)=>({time:open-86400+i*60,open:200,high:201,low:199,close:200,volume:100}));
 const today=Array.from({length:16},(_,i)=>({time:open+i*60,open:100,high:101,low:99,close:100.1,volume:100}));
 const early=[...previous,...today.slice(0,15)].reverse(),current=[...previous,...today].reverse();
 const metadata={...infos,subsession_id:'regular'};
 setTime((open+870)*1000);
 const initial=once(events,'result',{signal:AbortSignal.timeout(6000)});
 local.observe(symbol,early,metadata,raw(early),[],clock());
 assert.equal((await initial)[1].outputs.opening_position[0].ready,0);
 const tasks=[];let receivedAt;
 const update=createCoalescedUpdater(()=>local.observe(symbol,current,metadata,raw(current),[],receivedAt),{
  clock,schedule:(fn,delay)=>{const task={fn,delay};tasks.push(task);return task;}});
 setTime((open+900)*1000-528);receivedAt=clock();update.notify(today[15].time*1000);
 assert.equal(tasks[0].delay,528);
 const next=once(events,'result',{signal:AbortSignal.timeout(6000)});
 setTime((open+900)*1000);tasks.shift().fn();
 const [,{outputs}]=await next;
 assert.equal(outputs.opening_position[1].time,open+840);
 assert.equal(outputs.opening_position[1].ready,1);assert.equal(outputs.opening_position[1].range_bars,15);
 assert.equal(outputs.vwap_pullback[1].time,open+840);
 assert.equal(local.snapshot(symbol,raw(current)).outputs.opening_position[1].ready,1);
 assert.equal(tasks.length,0);
});
