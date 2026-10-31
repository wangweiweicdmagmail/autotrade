'use strict';

function emaFromPeriods(periods, index, length = 9) {
  if (!Array.isArray(periods) || periods.length <= index || length <= 0) return null;
  const alpha = 2 / (length + 1);
  let ema = null;
  for (let i = periods.length - 1; i >= index; i--) {
    const close = periods[i] && periods[i].close;
    if (!Number.isFinite(close)) {
      ema = null;
      continue;
    }
    ema = ema === null ? close : alpha * close + (1 - alpha) * ema;
  }
  return ema;
}

module.exports = { emaFromPeriods };
