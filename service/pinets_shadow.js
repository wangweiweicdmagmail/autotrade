'use strict';

const path = require('node:path');
const { Worker } = require('node:worker_threads');

const WARMUP_BARS = 400;
const WINDOW_BARS = 601;
const MAX_CONFIRMED_JOBS = 32;
const MAX_TASK_MS = 5000;
const RETRY_MS = 1000;
const COMPARE_FIELDS = ['entry_signal', 'direction', 'entry_price', 'long_stop', 'short_stop', 'atr'];

function sameCandle(bar, previous) {
  return previous && ['time', 'open', 'high', 'low', 'close', 'volume'].every(key => bar[key] === previous[key]);
}

function ready(output) {
  if (!output || !Number.isFinite(output.entry_price) || !(output.entry_price > 0) ||
      ![0, 1].includes(output.entry_signal) || ![-1, 1].includes(output.direction)) return false;
  if (output.direction === 1) return output.long_stop > 0 && output.long_stop < output.entry_price;
  return Number.isFinite(output.short_stop) && output.short_stop > output.entry_price;
}

function createEma7Shadow({ record, makeWorker = () => new Worker(path.join(__dirname, 'pinets_shadow_worker.js')) }) {
  const states = new Map();
  const realtimeQueue = new Map();
  const confirmedQueue = new Map();
  let worker, pending, timer;
  let stopped = false;
  let generation = 0;

  function reset(symbol) {
    for (const [key, job] of confirmedQueue) if (job.symbol === symbol) confirmedQueue.delete(key);
    realtimeQueue.delete(symbol);
    states.set(symbol, { generation: ++generation, revision: 0, confirmedRevision: 0,
      coalesced: 0, tv: {}, confirmed: null, retry: {}, status: null });
  }

  function dispatch() {
    if (stopped || pending) return;
    const queue = confirmedQueue.size ? confirmedQueue : realtimeQueue;
    const next = queue.entries().next();
    if (next.done) return;
    const [key, job] = next.value;
    queue.delete(key);
    pending = job;
    if (!worker) {
      try {
        worker = makeWorker();
      } catch (error) {
        finish({ error: error.message });
        return;
      }
      const instance = worker;
      instance.on('message', result => { if (worker === instance) finish(result); });
      instance.on('error', error => failWorker(instance, error.message));
      instance.on('exit', code => failWorker(instance, `PineTS worker exited (${code})`));
    }
    timer = setTimeout(() => failWorker(worker, 'PineTS worker timed out'), MAX_TASK_MS);
    try { worker.postMessage(job); } catch (error) { failWorker(worker, error.message); }
  }

  function failWorker(instance, message) {
    if (worker !== instance) return;
    worker = null;
    void instance.terminate();
    if (pending) finish({ error: message });
  }

  function finish(result) {
    clearTimeout(timer);
    const job = pending;
    pending = null;
    if (!job) return;
    const state = states.get(job.symbol);
    const ageMs = Date.now() - job.observedAt;
    const outdated = state?.generation !== job.generation ||
      (job.kind === 'realtime' ? state.revision !== job.revision :
        state.confirmedRevision !== job.revision && state.previous?.time === job.barTime);
    const status = result.error ? 'error' : outdated ? 'superseded' :
      result.output?.time !== job.barTime || !ready(result.output) ? 'unready' : 'ready';
    if (status === 'error' && !outdated) {
      state.retry[job.kind] = { time: job.barTime, after: Date.now() + RETRY_MS };
    }
    const reference = state?.generation === job.generation ? state.tv[job.kind] : null;
    const row = {
      type: 'ema7_shadow', symbol: job.symbol, kind: job.kind, status, bar_time: job.barTime,
      generation: job.generation, revision: job.revision, observed_at_ms: job.observedAt,
      elapsed_ms: ageMs, compute_ms: result.computeMs, coalesced: job.coalesced,
      pine_version: result.pineVersion, pinets_version: result.pinetsVersion,
      last_confirmed_time: state?.generation === job.generation ? state.confirmed?.bar_time : undefined,
      pinets: result.output ?? null, error: result.error,
    };
    const compared = withReference(row, reference);
    if (status === 'ready' && job.kind === 'confirmed' &&
        (state.confirmed?.bar_time ?? 0) <= job.barTime) state.confirmed = compared;
    record(compared);
    dispatch();
  }

  function withReference(row, reference) {
    if (row.status !== 'ready' || reference?.output?.time !== row.bar_time || !ready(reference.output)) {
      return { ...row, comparison: null, differences: null };
    }
    const differences = {};
    for (const key of COMPARE_FIELDS) {
      const a = reference.output[key], b = row.pinets[key];
      if (a !== b && !(Number.isFinite(a) && Number.isFinite(b) && Math.abs(a - b) <= 1e-8)) {
        differences[key] = { tv: a, pinets: b };
      }
    }
    return { ...row, comparison: row.kind === 'confirmed' ? 'confirmed_bar' : 'same_bar_only',
      tv_observed_at_ms: reference.observedAt, tv: reference.output, differences };
  }

  function observePrice(symbol, periods, infos, observedAt = Date.now()) {
    if (stopped || periods.length < 2) return;
    if (!states.has(symbol)) reset(symbol);
    const state = states.get(symbol);
    const metadata = {
      timezone: infos?.timezone, minmove: infos?.minmov ?? infos?.minmove, pricescale: infos?.pricescale,
      type: infos?.type, session: infos?.subsession_id,
    };
    const unavailable = periods.length <= WARMUP_BARS ? 'warming_up' :
      metadata.session !== 'extended' || !metadata.timezone || !(metadata.minmove > 0) ||
      !(metadata.pricescale > 0) ? 'metadata_unready' : null;
    if (unavailable) {
      if (state.status !== unavailable) record({ type: 'ema7_shadow', symbol, status: unavailable, bars: periods.length });
      state.status = unavailable;
      return;
    }
    state.status = null;
    const [current, previous] = periods;
    const retryDue = (kind, bar) => state.retry[kind]?.time === bar.time && observedAt >= state.retry[kind].after;
    const previousChanged = !sameCandle(previous, state.previous);
    const realChanged = !sameCandle(current, state.current) || previousChanged || retryDue('realtime', current);
    const confirmedChanged = previousChanged || retryDue('confirmed', previous);
    if (!realChanged && !confirmedChanged) return;
    state.current = { ...current };
    state.previous = { ...previous };
    const bars = periods.slice(0, WINDOW_BARS).reverse().map(row => ({
      time: row.time, open: row.open, high: row.max ?? row.high, low: row.min ?? row.low,
      close: row.close, volume: row.volume,
    }));
    const job = { symbol, generation: state.generation, observedAt, dataset: { symbol, metadata, bars } };
    if (confirmedChanged) {
      const key = symbol + ':' + previous.time;
      if (confirmedQueue.size < MAX_CONFIRMED_JOBS || confirmedQueue.has(key)) {
        delete state.retry.confirmed;
        confirmedQueue.set(key, { ...job, kind: 'confirmed', barTime: previous.time,
          revision: ++state.confirmedRevision, dataset: { ...job.dataset, bars: bars.slice(0, -1) } });
      } else {
        record({ type: 'ema7_shadow', symbol, kind: 'confirmed', status: 'queue_full', bar_time: previous.time });
        // Retry when another chart update arrives.
        state.previous = null;
      }
    }
    if (realChanged) {
      delete state.retry.realtime;
      if (realtimeQueue.has(symbol)) state.coalesced++;
      realtimeQueue.set(symbol, { ...job, kind: 'realtime', barTime: current.time,
        revision: ++state.revision, coalesced: state.coalesced });
    }
    dispatch();
  }

  function observeStudy(symbol, realtime, confirmed, observedAt = Date.now()) {
    const state = states.get(symbol);
    if (!state) return;
    state.tv = { realtime: { output: realtime, observedAt }, confirmed: { output: confirmed, observedAt } };
    if (state.confirmed && !state.confirmed.comparison) {
      const compared = withReference(state.confirmed, state.tv.confirmed);
      if (compared.comparison) {
        state.confirmed = compared;
        record({ ...compared, type: 'ema7_shadow_comparison' });
      }
    }
  }

  async function close() {
    stopped = true;
    clearTimeout(timer);
    pending = null;
    const instance = worker;
    worker = null;
    if (instance) await instance.terminate();
  }

  return { reset, observePrice, observeStudy, close };
}

module.exports = { createEma7Shadow };
