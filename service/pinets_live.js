'use strict';

const os = require('node:os');
const path = require('node:path');
const { Worker } = require('node:worker_threads');

const BAR_LIMIT = 601;
const MIN_BARS = 401;
const MAX_COHERENT_LAG_MS = 1600;
const REFRESH_MS = 850;
const JOB_TIMEOUT_MS = 12000;

function candle(row) {
  return { time: row.time, open: row.open, high: row.max ?? row.high, low: row.min ?? row.low,
    close: row.close, volume: row.volume };
}

function createLocalIndicators({ onError = () => {}, onReady = () => {}, onResult = () => {},
  intervalSeconds = 300, barLimit = BAR_LIMIT, minBars = MIN_BARS,
  indicatorKeys = null, historyTail = null,
  workers = Math.min(8, Math.max(1, os.availableParallelism() - 2)),
  makeWorker = () => new Worker(path.join(__dirname, 'pinets_live_worker.js')) } = {}) {
  const states = new Map();
  const pending = new Map();
  const slots = Array.from({ length: workers }, () => ({}));
  const readyKeys = new Set();
  let stopped = false;

  function reset(symbol) {
    pending.delete(symbol);
    const slot = states.get(symbol)?.slot ?? slots[states.size % slots.length];
    states.set(symbol, { slot, revision: 0, publishedRevision: 0, nextAt: 0,
      history: {}, confirmed: {}, confirmedHistoryKeys: {}, busy: false });
  }

  function observe(symbol, periods, infos, raw, daily = [], now = Date.now()) {
    if (stopped || !periods.length) return;
    if (!states.has(symbol)) reset(symbol);
    const state = states.get(symbol);
    const metadata = { timezone: infos?.timezone, minmove: infos?.minmov ?? infos?.minmove,
      pricescale: infos?.pricescale, type: infos?.type, session: infos?.subsession_id };
    const bars = periods.slice(0, barLimit).reverse().map(candle);
    const last = bars.at(-1);
    const priceKey = JSON.stringify(last);
    if (state.priceKey !== priceKey) {
      state.priceKey = priceKey;
      state.observedAt = now;
    }
    if (bars.length < minBars || !metadata.timezone || !(metadata.minmove > 0) || !(metadata.pricescale > 0)) return;
    // The daily study reads high[1]/low[1]. The current daily bar's
    // changing prices cannot change those outputs, but a new day can.
    const dailyKey = JSON.stringify([daily.slice(0, -1), daily.at(-1)?.time]);
    const historyKey = JSON.stringify(bars.slice(0, -1)) + dailyKey;
    const key = priceKey + historyKey;
    if (key === state.key) return;
    state.key = key;
    const urgent = !state.latest || state.latest.raw.bar_c?.time !== raw.bar_c?.time;
    const job = { symbol, state, revision: ++state.revision, historyKey, dailyKey, urgent,
      observedAt: state.observedAt, raw: { ...raw }, dataset: { symbol, bars, daily, metadata, intervalSeconds,
        ...(indicatorKeys ? { indicatorKeys, historyTail } : {}) } };
    state.latest = job;
    pending.set(symbol, job);
    pump();
  }

  function startWorker(slot) {
    const worker = makeWorker();
    slot.worker = worker;
    worker.on('message', result => {
      if (slot.worker === worker) finish(slot, result);
    });
    worker.on('error', error => loseWorker(slot, worker, error.message));
    worker.on('exit', code => loseWorker(slot, worker, `worker exited (${code})`));
  }

  function loseWorker(slot, worker, reason) {
    if (slot.worker !== worker) return;
    slot.worker = null;
    void worker.terminate();
    if (slot.job) finish(slot, { errors: { worker: reason } });
  }

  function pump(now = Date.now()) {
    if (stopped) return;
    for (const slot of slots) {
      if (slot.job) continue;
      let candidate;
      for (const job of pending.values()) {
        if (job.state.slot !== slot || job.state.busy || (!job.urgent && job.state.nextAt > now)) continue;
        if (!candidate || job.urgent && !candidate.urgent) candidate = job;
        if (candidate.urgent) break;
      }
      if (!candidate) continue;
      pending.delete(candidate.symbol);
      const { state } = candidate;
      state.busy = true;
      state.nextAt = now + REFRESH_MS;
      slot.job = candidate;
      const missing = indicatorKeys ? indicatorKeys.some(key => !state.history[key]) : intervalSeconds === 60 ? !state.history.vwap_pullback :
        !state.history.e9 || !state.history.trend || (candidate.dataset.daily.length > 0 && !state.history.attack);
      candidate.history = state.historyKey !== candidate.historyKey || missing;
      const previous = state.completed;
      const oldBars = previous?.dataset.bars;
      const bars = candidate.dataset.bars;
      const advance = candidate.history && oldBars && previous.dailyKey === candidate.dailyKey &&
        oldBars.at(-1).time + intervalSeconds === bars.at(-1).time &&
        JSON.stringify(oldBars.slice(oldBars.length + 1 - bars.length, -1)) === JSON.stringify(bars.slice(0, -2));
      try {
        if (!slot.worker) startWorker(slot);
        slot.worker.postMessage({ dataset: candidate.dataset, history: candidate.history, advance: Boolean(advance) });
        slot.timer = setTimeout(() => loseWorker(slot, slot.worker, '本地计算超时'), JOB_TIMEOUT_MS);
      } catch (error) {
        if (slot.worker) loseWorker(slot, slot.worker, error.message);
        else finish(slot, { errors: { worker: error.message } });
      }
    }
  }

  function finish(slot, { results = {}, errors = {} }) {
    clearTimeout(slot.timer);
    const job = slot.job;
    slot.job = null;
    if (!job) return;
    const { state, symbol } = job;
    state.busy = false;
    if (states.get(symbol) !== state || stopped) return;
    for (const [key, error] of Object.entries(errors)) onError(symbol, key, error);
    const valid = {};
    for (const [key, rows] of Object.entries(results)) {
      if (rows[0]?.time !== job.raw.bar.time || rows[1]?.time !== job.raw.bar_c?.time) continue;
      valid[key] = rows;
      if (!readyKeys.has(key)) {
        readyKeys.add(key);
        onReady(key, readyKeys.size);
      }
    }
    const cached = state.historyKey === job.historyKey ? state.history : {};
    const history = { ...cached };
    for (const [key, rows] of Object.entries(valid)) {
      if (job.history) history[key] = rows.slice(1);
    }
    const outputs = Object.fromEntries(Object.entries(valid).map(([key, rows]) => [key, [
      rows[0], ...(history[key]?.[0]?.time === rows[1].time ? history[key] : rows.slice(1)),
    ]]));
    if (Object.keys(valid).length) {
      state.historyKey = job.historyKey;
      state.history = history;
      state.completed = { ...job, outputs };
      const confirmedKey = JSON.stringify(candle(job.raw.bar_c));
      if (state.confirmedKey !== confirmedKey) {state.confirmed = {};state.confirmedHistoryKeys={};}
      state.confirmedKey = confirmedKey;
      for (const [key, rows] of Object.entries(outputs)) {
        state.confirmed[key] = rows.slice(1);state.confirmedHistoryKeys[key]=job.historyKey;
      }
      onResult(symbol, { receivedAt: job.observedAt, completedAt: Date.now(), outputs });
    }
    if (Object.keys(errors).length && !pending.has(symbol) && state.latest.revision === job.revision) {
      state.nextAt = Date.now() + 5000;
      pending.set(symbol, { ...job, urgent: false });
    }
    pump();
  }

  function snapshot(symbol, raw, now = Date.now()) {
    const state = states.get(symbol);
    const completed = state?.completed;
    const latest = state?.latest;
    const coherent = completed && completed.raw.bar.time === raw.bar.time &&
      completed.revision >= state.publishedRevision &&
      (completed.revision === latest.revision || now - completed.observedAt <= MAX_COHERENT_LAG_MS);
    const base = coherent ? completed.raw : raw;
    if (state && latest) state.publishedRevision = coherent ? completed.revision : latest.revision;
    // A confirmation remains readable while the realtime calculation catches up,
    // but only if it matches the confirmed bar carried in the published snapshot.
    const outputs = {};
    const confirmed = base.bar_c && JSON.stringify(candle(base.bar_c)) === state?.confirmedKey
      ? state.confirmed : {};
    const live = coherent && now < (base.bar.time + intervalSeconds) * 1000 ? completed.outputs : {};
    for (const key of new Set([...Object.keys(live), ...Object.keys(confirmed)])) {
      outputs[key] = [live[key]?.[0] ?? null, ...(confirmed[key] || [])];
    }
    return { base, outputs, observedAt: coherent ? completed.observedAt : state?.observedAt };
  }

  function confirmedHistory(symbol,raw,key='trend'){
    const state=states.get(symbol),job=state?.completed;
    if(!job||!raw.bar_c||job.historyKey!==state.latest?.historyKey
      ||state.confirmedHistoryKeys[key]!==job.historyKey
      ||JSON.stringify(candle(raw.bar_c))!==state.confirmedKey)return null;
    const rows=state.confirmed[key];
    return rows?.length?{historyKey:job.historyKey,bars:job.dataset.bars,rows}:null;
  }

  const interval = setInterval(pump, 50);
  interval.unref();
  async function close() {
    stopped = true;
    clearInterval(interval);
    await Promise.all(slots.map(slot => {
      clearTimeout(slot.timer);
      const worker = slot.worker;
      slot.worker = null;
      return worker?.terminate();
    }));
  }
  return { reset, observe, snapshot, confirmedHistory, close };
}

module.exports = { BAR_LIMIT, createLocalIndicators, candle };
