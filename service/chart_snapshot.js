'use strict';
const {priorBodyEma10, priorRegularEngulfing4} = require('./candle_metrics');
const {emaFromPeriods} = require('./ema');

function isPriceUpdate(changes) {
  return !Array.isArray(changes) || changes.includes('$prices');
}

// Closed history changes only at a new candle or a historical correction.
// Keep live OHLC/ATR current without rebuilding 160 historical rows per tick.
function createPriceUpdater(atr) {
  let previousHistory = null;
  let bodyBaseline = null;
  let engulfing = null;
  return function update(snapshot, periods) {
    if (!periods.length) return;
    const history = periods.slice(1).map(p =>
      [p.time,p.open,p.max,p.min,p.close,p.volume].join(',')).join(';');
    const b = periods[0];
    if (history !== previousHistory) {
      previousHistory = history;
      snapshot.breakoutBars = periods.slice(1,161).map((p,i) => [
        p.time,p.open,p.max,p.min,p.close,atr(periods,i+1),
        priorBodyEma10(periods,i+1),priorRegularEngulfing4(periods,i+1),
      ]);
      snapshot.closedPrices = new Map(periods.slice(1,21).map(p => [p.time,p.close]));
      snapshot.ema_9_c = emaFromPeriods(periods,1,9);
      const bc = periods[1];
      snapshot.bar_c = bc ? {
        time:bc.time,open:bc.open,high:bc.max,low:bc.min,close:bc.close,volume:bc.volume,
        atr_sma10:atr(periods,1),body_ema10:priorBodyEma10(periods,1),
        prior_engulfing4:priorRegularEngulfing4(periods,1),
      } : null;
      const b2 = periods[2];
      snapshot.bar_c2 = b2 ? {time:b2.time,open:b2.open,high:b2.max,low:b2.min,close:b2.close,volume:b2.volume} : null;
      bodyBaseline = priorBodyEma10(periods,0);
      engulfing = priorRegularEngulfing4(periods,0);
    }
    snapshot.breakoutChartPeriods = periods.slice(0,175);
    snapshot.ema_9 = emaFromPeriods(periods,0,9);
    snapshot.bar = {
      time:b.time,open:b.open,high:b.max,low:b.min,close:b.close,volume:b.volume,
      atr_sma10:atr(periods,0),body_ema10:bodyBaseline,prior_engulfing4:engulfing,
    };
  };
}
function chartSession(infos, requested, error = null) {
  const resolved = infos?.subsession_id || null;
  const symbol = infos?.full_name || infos?.name || null;
  const accepted = requested === 'default' ? Boolean(symbol) : requested !== '24h' || resolved === '24h';
  return {
    requested, resolved, error,
    symbol,
    timezone: infos?.timezone || null,
    accepted: !error && accepted,
  };
}

const easternDay = new Intl.DateTimeFormat('en-CA', {
  timeZone: 'America/New_York', year: 'numeric', month: '2-digit', day: '2-digit',
});

function previousRegularRange(daily, barTime) {
  if (!daily?.length || !Number.isFinite(barTime)) return null;
  const day = easternDay.format(barTime * 1000);
  for (let i = daily.length - 1; i >= 0; i--) {
    const { time, high, low } = daily[i];
    const date = easternDay.format(time * 1000);
    if (date >= day) continue;
    if (!Number.isFinite(high) || !Number.isFinite(low) || low <= 0 || high <= low) return null;
    return { day, date, high, low };
  }
  return null;
}

module.exports = {createPriceUpdater,isPriceUpdate,chartSession,previousRegularRange};
