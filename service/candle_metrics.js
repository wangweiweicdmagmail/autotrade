'use strict';

// periods are newest first. Exclude the evaluated candle, then seed EMA10
// with ten older absolute bodies and give newer closed candles more weight.
function priorBodyEma10(periods, index) {
  if (periods.length < index + 11) return null;
  const alpha = 2 / 11;
  let count = 0;
  let seed = 0;
  let ema = null;
  for (let i = periods.length - 1; i > index; i--) {
    const { open, close } = periods[i];
    if (!Number.isFinite(open) || !Number.isFinite(close)
        || open <= 0 || close <= 0 || open >= 1e99 || close >= 1e99) {
      count = 0;
      seed = 0;
      ema = null;
      continue;
    }
    const body = Math.abs(close - open);
    count++;
    if (count <= 10) {
      seed += body;
      if (count === 10) ema = seed / 10;
    } else {
      ema = alpha * body + (1 - alpha) * ema;
    }
  }
  return ema;
}

const etClock = new Intl.DateTimeFormat('en-GB', {
  timeZone: 'America/New_York', hour: '2-digit', minute: '2-digit', hourCycle: 'h23',
});

// Bits 1/2 indicate an upward/downward engulf in the four previous bars.
// Both candles of each pair must belong to today's regular session.
function priorRegularEngulfing4(periods, index) {
  const target = periods[index]?.time;
  if (!Number.isFinite(target)) return null;
  const [hour, minute] = etClock.format(new Date(target * 1000)).split(':').map(Number);
  const sessionStart = target - (hour * 60 + minute - 570) * 60;
  let mask = 0;
  for (let k = 1; k <= 4; k++) {
    const expected = target - k * 300;
    if (expected <= sessionStart) break;
    const current = periods[index + k];
    const previous = periods[index + k + 1];
    if (current?.time !== expected || previous?.time !== expected - 300) return null;
    const prices = [current.open, current.close, previous.open, previous.close];
    if (prices.some(value => !Number.isFinite(value) || value <= 0 || value >= 1e99)) return null;
    if (previous.close > previous.open && current.close < current.open
        && current.close < previous.open) mask |= 2;
    if (previous.close < previous.open && current.close > current.open
        && current.close > previous.open) mask |= 1;
  }
  return mask;
}

function breakoutContext(periods, index, emaByTime) {
  const bar = periods[index];
  if (!bar) return {};
  const previous = periods[index + 1];
  const model = emaByTime?.get(bar.time);
  const samples = [];
  for (let k = 1; k <= 3; k++) {
    const row = periods[index + k];
    const output = row ? emaByTime?.get(row.time) : null;
    samples.push({
      time: row?.time ?? null, open: row?.open ?? null, close: row?.close ?? null,
      ema7: output?.ema_9 ?? null,
    });
  }
  return {
    context_time: bar.time, previous_time: previous?.time ?? null,
    previous_high: previous?.max ?? null, previous_low: previous?.min ?? null,
    ema7_previous3: samples, ema_slope_atr: model?.ema_slope_atr ?? null,
    ema_slope_time: model?.time ?? null,
  };
}

// Full OHLC history remains available for legacy stop recovery. The richer
// Pine context is only used in the opening-breakout scan window; repeating it
// on all 161 rows per symbol inflated each stdout tick beyond 4MB.
function historicalBreakoutContext(periods, index, emaByTime) {
  const stamp = periods[index]?.time;
  if (!Number.isFinite(stamp)) return {};
  const [hour, minute] = etClock.format(new Date(stamp * 1000)).split(':').map(Number);
  const clock = hour * 60 + minute;
  return clock >= 580 && clock < 630 ? breakoutContext(periods, index, emaByTime) : {};
}

module.exports = { priorBodyEma10, priorRegularEngulfing4, breakoutContext, historicalBreakoutContext };
