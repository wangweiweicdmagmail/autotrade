/* Read the Pine outputs on the same candle used for price and candle color. */
const RangeDistribution = (() => {
  const clock = new Intl.DateTimeFormat('en-CA', {
    timeZone: 'America/New_York', year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', hourCycle: 'h23',
  });
  const zones = ['昨高上方', '昨日区间内', '昨低下方'];
  const lanes = ['小区间上方', '小区间内', '小区间下方'];
  const candleNames = { up: '阳线', down: '阴线', doji: '平线' };
  const dotClearance = 42; // 38px circle plus 4px breathing room
  const validPrice = value => Number.isFinite(value) && value > 0 && value < 1e99;

  function readRow(symbol, data) {
    if (!data) return null;
    const prior = data.previous_regular_range;
    if (!prior || !validPrice(prior.high) || !validPrice(prior.low) || prior.high <= prior.low) return null;
    for (const suffix of ['', '_c']) {
      const bar = data['bar' + suffix];
      const attack = data['attack' + suffix];
      const ema = data['e9' + suffix];
      if (!Number.isFinite(bar?.time) || !validPrice(bar.close) || !validPrice(bar.open)
          || attack?.time !== bar.time || ema?.time !== bar.time || !Number.isFinite(ema.ema_slope_atr)) continue;
      const parts = Object.fromEntries(clock.formatToParts(bar.time * 1000).map(part => [part.type, part.value]));
      const day = [parts.year, parts.month, parts.day].join('-');
      if (day !== prior.day || !validPrice(attack.pmh) || !validPrice(attack.pml) || attack.pmh < attack.pml) continue;
      const minutes = Number(parts.hour) * 60 + Number(parts.minute);
      if (minutes < 240) continue;
      const forming = minutes < 585;
      const hasOpening = minutes >= 570 && validPrice(attack.opening3High) && validPrice(attack.opening3Low)
        && attack.opening3High >= attack.opening3Low;
      if (!forming && !hasOpening) continue;
      const upper = hasOpening ? Math.max(attack.pmh, attack.opening3High) : attack.pmh;
      const lower = hasOpening ? Math.min(attack.pml, attack.opening3Low) : attack.pml;
      const major = bar.close > prior.high ? 0 : bar.close < prior.low ? 2 : 1;
      const minor = bar.close > upper ? 0 : bar.close < lower ? 2 : 1;
      const candle = bar.close > bar.open ? 'up' : bar.close < bar.open ? 'down' : 'doji';
      let target = null;
      if (!forming && minor === 0) {
        if (bar.close < prior.low) target = { label: '昨低', price: prior.low };
        else if (bar.close < prior.high) target = { label: '昨高', price: prior.high };
      } else if (!forming && minor === 2) {
        if (bar.close > prior.high) target = { label: '昨高', price: prior.high };
        else if (bar.close > prior.low) target = { label: '昨低', price: prior.low };
      }
      return { symbol, ticker: symbol.split(':').pop(), time: bar.time, confirmed: suffix === '_c', forming,
        close: bar.close, speed: ema.ema_slope_atr, major, minor, upper, lower, prior, target,
        candle: candleNames[candle], tone: minor === 1 ? 'flat' : candle };
    }
    return null;
  }

  function layOutLane(rows, limit, width, maxLayers) {
    if (maxLayers === 1 || !width || rows.length < 2) return { rows, layers: 1 };

    const lastCenters = Array(maxLayers).fill(-Infinity);
    const layersBySymbol = new Map();
    const leftToRight = [...rows].sort((a, b) => a.speed - b.speed);
    const usableWidth = Math.max(0, width - dotClearance);
    for (const row of leftToRight) {
      const center = speedRatio(row.speed, limit) * usableWidth;
      let layer = lastCenters.findIndex(last => center - last >= dotClearance);
      if (layer === -1) layer = lastCenters.indexOf(Math.min(...lastCenters));
      lastCenters[layer] = center;
      layersBySymbol.set(row.symbol, layer);
    }
    // Keep the absolute-speed paint order even when points share a layer.
    return {
      rows: rows.map(row => ({ ...row, layer: layersBySymbol.get(row.symbol) })),
      layers: lastCenters.filter(Number.isFinite).length,
    };
  }

  function build(market, watchlist, width = 0) {
    const rows = [], pending = [];
    for (const symbol of watchlist) {
      const row = readRow(symbol, market[symbol]);
      if (row) rows.push(row);
      else pending.push(symbol.split(':').pop());
    }
    rows.sort((a, b) => Math.abs(a.speed) - Math.abs(b.speed) || a.symbol.localeCompare(b.symbol));
    const limit = Math.max(3, Math.ceil(Math.abs(rows.at(-1)?.speed ?? 0)));
    const groups = zones.map((name, major) => ({
      name, lanes: lanes.map((label, minor) => ({
        label,
        ...layOutLane(rows.filter(row => row.major === major && row.minor === minor),
          limit, width, minor === 1 ? 1 : 3),
      })),
    }));
    return { groups, rows, pending, limit, forming: rows.some(row => row.forming) };
  }

  function speedRatio(speed, limit) {
    return (speed / limit + 1) / 2;
  }

  function position(speed, limit) {
    const ratio = speedRatio(speed, limit);
    return `calc(${ratio * 100}% + ${(0.5 - ratio) * dotClearance}px)`;
  }

  return { build, position };
})();

if (typeof module !== 'undefined') module.exports = RangeDistribution;
