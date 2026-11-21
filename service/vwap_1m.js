'use strict';

// Read-only counterpart of indicators/vwap-ema7-position-1m.pine.
const eastern = new Intl.DateTimeFormat('en-CA', {
  timeZone: 'America/New_York', year: 'numeric', month: '2-digit', day: '2-digit',
  weekday: 'short', hour: '2-digit', minute: '2-digit', hourCycle: 'h23',
});

function calculateVwap1m(bars, { stMultiplier = 4 } = {}) {
  if (!Number.isFinite(stMultiplier) || stMultiplier <= 0) throw new Error('Invalid ST multiplier');
  let previousClose, atr, upperBand, lowerBand, line;
  let samples = 0, sumTR = 0;
  let complete = false;
  let date, openingTime, weight = 0, mean = 0, m2 = 0, ema;
  let recent = [];
  const vwapHistory = new Map();
  let result = null;
  for (const bar of bars) {
    if (![bar.time, bar.open, bar.high, bar.low, bar.close].every(Number.isFinite)) continue;
    const parts = Object.fromEntries(eastern.formatToParts(bar.time * 1000).map(({ type, value }) => [type, value]));
    const minute = +parts.hour * 60 + +parts.minute;
    if (minute < 570 || minute >= 960 || ['Sat', 'Sun'].includes(parts.weekday)) continue;
    const day = `${parts.year}-${parts.month}-${parts.day}`;
    if (day !== date) {
      date = day;
      complete = false;
      openingTime = null;
      weight = mean = m2 = 0;
      recent = [];
      vwapHistory.clear();
    }
    if (minute === 570) { complete = true; openingTime = bar.time; }
    const tr = previousClose == null ? bar.high - bar.low :
      Math.max(bar.high - bar.low, Math.abs(bar.high - previousClose), Math.abs(bar.low - previousClose));
    let direction = null;
    if (samples < 10) {
      sumTR += tr;
      samples++;
      if (samples === 10) atr = sumTR / 10;
    } else {
      atr += (tr - atr) / 10;
    }
    if (atr != null) {
      const midpoint = (bar.high + bar.low) / 2;
      const basicUpper = midpoint + stMultiplier * atr;
      const basicLower = midpoint - stMultiplier * atr;
      const previousUpper = upperBand;
      upperBand = upperBand == null || basicUpper < upperBand || previousClose > upperBand ? basicUpper : upperBand;
      lowerBand = lowerBand == null || basicLower > lowerBand || previousClose < lowerBand ? basicLower : lowerBand;
      direction = line == null ? -1 : line === previousUpper ?
        (bar.close > upperBand ? 1 : -1) : (bar.close < lowerBand ? -1 : 1);
      line = direction === 1 ? lowerBand : upperBand;
    }
    previousClose = bar.close;
    ema = ema == null ? bar.close : ema + (bar.close - ema) * 0.25;
    if (Number.isFinite(bar.volume) && bar.volume > 0) {
      const sample = (bar.high + bar.low + bar.close) / 3;
      const delta = sample - mean;
      weight += bar.volume;
      mean += delta * bar.volume / weight;
      m2 += bar.volume * delta * (sample - mean);
    }
    recent.push({ time: bar.time, ema });
    if (recent.length > 8) recent.shift();
    const prior = recent.find(sample => bar.time - sample.time === 420);
    const hasVwap = complete && weight > 0;
    const width = hasVwap ? Math.sqrt(Math.max(0, m2 / weight)) : null;
    const position = width > 0 ? (ema - mean) / width : null;
    const vwapReferenceTime = openingTime == null ? null : Math.max(openingTime, bar.time - 1800);
    const previousVwap = vwapHistory.get(vwapReferenceTime);
    const vwapSpeed = width > 0 && previousVwap != null ? (mean - previousVwap) / width : null;
    if (hasVwap) vwapHistory.set(bar.time, mean);
    for (const time of vwapHistory.keys()) {
      if (vwapReferenceTime != null && time < vwapReferenceTime) vwapHistory.delete(time);
    }
    result = { time: bar.time, date, ema, st_direction: direction, st_price: line ?? null,
      vwap: hasVwap ? mean : null,
      upper: width == null ? null : mean + width, lower: width == null ? null : mean - width,
      position, speed: prior ? ema - prior.ema : null,
      vwap_speed_30m: Number.isFinite(vwapSpeed) ? vwapSpeed : null,
      vwap_reference: previousVwap ?? null,
      vwap_reference_time: previousVwap == null ? null : vwapReferenceTime,
      vwap_reference_mode: previousVwap == null ? null : bar.time - openingTime < 1800 ? 'opening' : 'rolling',
      sigma: width,
      zone: position == null ? null : position > 1 ? 1 : position < -1 ? -1 : 0 };
  }
  return result;
}

function candidateProtectionSnapshot(bars, now) {
  // Independent closed-only ST(10,3); distribution ST(10,4) is unchanged.
  const closed = bars.filter(bar => Number.isInteger(bar.time) && bar.time % 60 === 0 && bar.time + 60 <= now);
  const last = closed.at(-1);
  if (!last || now-(last.time+60) >= 105) return null;
  const parts = Object.fromEntries(eastern.formatToParts(last.time * 1000).map(({type,value})=>[type,value]));
  const minute = +parts.hour*60 + +parts.minute;
  if (minute < 570 || minute >= 960 || ['Sat','Sun'].includes(parts.weekday)) return null;
  const opening = last.time-(minute-570)*60;
  const tail = closed.filter(bar=>bar.time>=opening).slice(-11);
  // A normal overnight break is not a missing regular-session candle. Prior
  // regular minutes still warm ATR/ST, including the first minutes after open.
  if ((tail.length<11 && tail[0]?.time!==opening) ||
      tail.some((bar, i) => i && bar.time !== tail[i-1].time+60)) return null;
  const result = calculateVwap1m(closed, {stMultiplier: 3});
  if (!result || result.time !== last.time || !Number.isFinite(result.st_price) || ![-1,1].includes(result.st_direction)) return null;
  return {version: 1, time: result.time, price: result.st_price, direction: result.st_direction,
    atr_period: 10, multiplier: 3, interval_seconds: 60, session: 'regular'};
}

function createVwapMinuteBatch(symbols) {
  const expected = new Set(symbols);
  const pending = new Map();
  const dirty = new Set();
  let newest = 0;
  let frame = null;

  function observe(symbol, row) {
    if (!expected.has(symbol) || !Number.isInteger(row?.time) || row.time <= 0 ||
        row.time % 60 !== 0 || row.time < (frame?.time ?? 0) || row.time < newest - 120) return;
    if (row.time > newest) {
      newest = row.time;
      for (const time of pending.keys()) {
        if (time < newest - 120) { pending.delete(time); dirty.delete(time); }
      }
    }
    if (![-1, 1].includes(row.st_direction) ||
        ![row.position, row.speed, row.vwap, row.upper, row.lower, row.ema].every(Number.isFinite)) {
      pending.get(row.time)?.delete(symbol);
      dirty.add(row.time);
      return;
    }

    let batch = pending.get(row.time);
    if (!batch) {
      batch = new Map();
      pending.set(row.time, batch);
    }
    const next = { ...row, symbol };
    const previous = batch.get(symbol);
    if (previous && Object.keys(next).length === Object.keys(previous).length
        && Object.keys(next).every(key => next[key] === previous[key])) return;
    batch.set(symbol, next);
    dirty.add(row.time);
  }

  function snapshot() {
    // 生产端每秒调用一次；一分钟内复用已就绪且未变化的标的，一次发布完整画面。
    // 新分钟未齐时冻结上一批，不混入前一分钟的单个标的。
    const complete = [...pending.keys()].filter(time => pending.get(time).size === expected.size
      && (time > (frame?.time ?? 0) || (time === newest && dirty.has(time))))
      .sort((a, b) => b - a)[0];
    if (complete !== undefined) {
      frame = { time: complete,
        // 毫秒版本在进程内严格递增，重启后也不从 1 开始，供下游同根去重。
        revision: Math.max(Date.now(), (frame?.revision ?? 0) + 1),
        rows: Array.from(expected, ticker => pending.get(complete).get(ticker)) };
      dirty.delete(complete);
      for (const time of pending.keys()) {
        if (time < complete) { pending.delete(time); dirty.delete(time); }
      }
    }
    return {
      frame,
      progress: {
        time: newest,
        ready: pending.get(newest)?.size ?? 0,
        total: expected.size,
      },
    };
  }

  return { observe, snapshot };
}

function openingVwapSnapshot(bars, observedAt) {
  const last = bars.at(-1);
  if (!last || !Number.isInteger(last.time) || !Number.isFinite(last.close)) return null;
  const parts = Object.fromEntries(eastern.formatToParts(last.time * 1000)
    .map(({ type, value }) => [type, value]));
  const minute = +parts.hour * 60 + +parts.minute;
  if (minute < 570 || minute >= 960 || ['Sat', 'Sun'].includes(parts.weekday)) return null;
  const opening = last.time - (minute - 570) * 60;
  const result = { opening, bar_time: last.time, price: last.close, observed_at: observedAt, signals: [] };
  const initial = bars.filter(bar => opening <= bar.time && bar.time < opening + 300);
  const prefix = [];
  for (const bar of initial) {
    if (bar.time !== opening + prefix.length * 60 ||
        ![bar.open, bar.high, bar.low, bar.close, bar.volume].every(Number.isFinite) ||
        !(0 < bar.low && bar.low <= Math.min(bar.open, bar.close) &&
          bar.high >= Math.max(bar.open, bar.close) && bar.volume >= 0)) break;
    prefix.push(bar);
    if (prefix.length < 3) continue;
    const recent = prefix.slice(-3);
    const before = prefix.slice(0, -1);
    const previous = before.at(-1);
    const bands = calculateVwap1m(prefix);
    if (!(bands?.upper > bands?.lower)) continue;
    const bodyPct = Math.abs(bar.close - bar.open) / bar.open * 100;
    if (!(bodyPct > 0.5)) continue;
    const long = recent.every(row => row.close > row.open) &&
      bar.high > Math.max(...before.map(row => row.high)) &&
      bar.close > previous.high && bar.close > bands.upper;
    const short = recent.every(row => row.close < row.open) &&
      bar.low < Math.min(...before.map(row => row.low)) &&
      bar.close < previous.low && bar.close < bands.lower;
    if (!long && !short) continue;
    const stop = Math.round((Math.max(...recent.map(row => row.high)) +
      Math.min(...recent.map(row => row.low))) * 50) / 100;
    if (!(long ? stop < bar.close : stop > bar.close)) continue;
    result.signals.push({
      bar_time: bar.time, close_at: bar.time + 60, confirmed: last.time >= bar.time + 60,
      side: long ? 'long' : 'short', entry_px: bar.close, stop_px: stop, body_pct: bodyPct,
      vwap_band: long ? bands.upper : bands.lower, previous_extreme: long ? previous.high : previous.low,
      candles: recent.map(({ time, open, high, low, close }) => ({ time, open, high, low, close })),
    });
  }
  return result;
}

module.exports = { calculateVwap1m, candidateProtectionSnapshot, createVwapMinuteBatch, openingVwapSnapshot };
