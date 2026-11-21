/* Read the Pine outputs on the same candle used for price and candle color. */
const RangeDistribution = (() => {
  const clock = new Intl.DateTimeFormat('en-CA', {
    timeZone: 'America/New_York', year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', hourCycle: 'h23',
  });
  const zones = ['昨高上方', '昨日区间内', '昨低下方'];
  const candleNames = { up: '阳线', down: '阴线', doji: '平线' };
  const dotClearance = 42; // 38px circle plus 4px breathing room
  const validPrice = value => Number.isFinite(value) && value > 0 && value < 1e99;

  function readRow(symbol, data) {
    if (!data) return null;
    for (const suffix of ['', '_c']) {
      const bar = data['bar' + suffix];
      const attack = data['attack' + suffix];
      const ema = data['e9' + suffix];
      if (!Number.isFinite(bar?.time) || !validPrice(bar.close) || !validPrice(bar.open)
          || attack?.time !== bar.time || ema?.time !== bar.time || !Number.isFinite(ema.ema_slope_atr) || !validPrice(ema.ema_9)) continue;
      const parts = Object.fromEntries(clock.formatToParts(bar.time * 1000).map(part => [part.type, part.value]));
      const day = [parts.year, parts.month, parts.day].join('-');
      if (!validPrice(attack.pmh) || !validPrice(attack.pml) || attack.pmh < attack.pml) continue;
      const minutes = Number(parts.hour) * 60 + Number(parts.minute);
      if (minutes < 240) continue;
      const forming = minutes < 585;
      const hasOpening = !forming && validPrice(attack.opening3High) && validPrice(attack.opening3Low)
        && attack.opening3High >= attack.opening3Low;
      if (!forming && !hasOpening) continue;
      const upper = hasOpening ? Math.max(attack.pmh, attack.opening3High) : attack.pmh;
      const lower = hasOpening ? Math.min(attack.pml, attack.opening3Low) : attack.pml;
      const halfHeight = (upper - lower) / 2;
      if (!(halfHeight > 0)) continue;
      const midpoint = (upper + lower) / 2;
      const normalizedPosition = (ema.ema_9 - midpoint) / halfHeight;
      if (!Number.isFinite(normalizedPosition)) continue;
      const minor = normalizedPosition > 1 ? 0 : normalizedPosition < -1 ? 2 : 1;
      const reference = data.previous_regular_range;
      const prior = reference?.day === day && validPrice(reference.high) && validPrice(reference.low)
        && reference.high > reference.low ? reference : null;
      if (!prior) continue;
      const major = bar.close > prior.high ? 0 : bar.close < prior.low ? 2 : 1;
      const candle = bar.close > bar.open ? 'up' : bar.close < bar.open ? 'down' : 'doji';
      let target = null;
      if (prior && !forming && minor === 0) {
        if (ema.ema_9 < prior.low) target = { label: '昨低', price: prior.low };
        else if (ema.ema_9 < prior.high) target = { label: '昨高', price: prior.high };
      } else if (prior && !forming && minor === 2) {
        if (ema.ema_9 > prior.high) target = { label: '昨高', price: prior.high };
        else if (ema.ema_9 > prior.low) target = { label: '昨低', price: prior.low };
      }
      const ticker = symbol.split(':').pop();
      return { symbol, ticker, time: bar.time, confirmed: suffix === '_c', forming,
        ready: !forming && suffix === '' && minor !== 1,
        close: bar.close, ema7: ema.ema_9, speed: ema.ema_slope_atr, major, zone: zones[major], minor, upper, lower, prior, target,
        midpoint, halfHeight, normalizedPosition, distance: Math.max(0, Math.abs(normalizedPosition) - 1),
        candle: candleNames[candle], tone: minor === 1 && ticker !== 'QQQ' ? 'flat' : candle };
    }
    return null;
  }

  function build(market, watchlist, width = 800, height = 560, focus = null) {
    const rows = [], pending = [];
    for (const symbol of watchlist) {
      const row = readRow(symbol, market[symbol]);
      if (row) rows.push(row);
      else pending.push(symbol.split(':').pop());
    }
    rows.sort((a, b) => Math.abs(a.speed) - Math.abs(b.speed) || a.symbol.localeCompare(b.symbol));
    const limit = Math.max(3, Math.ceil(Math.abs(rows.at(-1)?.speed ?? 0)));
    const positionLimit = Math.max(3, Math.ceil(Math.max(0, ...rows.map(row => Math.abs(row.normalizedPosition))) + 0.35));
    const bounds = {
      width: Math.max(240, width || 800), height: Math.max(540, height || 640),
      left: 44, top: 42,
    };
    bounds.right = bounds.width - 26;
    bounds.bottom = bounds.height - 56;
    const single = Number.isInteger(focus) && focus >= 0 && focus < zones.length;
    const visibleRows = rows.filter(row => !single || row.major === focus);
    // SVG paints later points above earlier ones. Keep QQQ visible in overlaps.
    visibleRows.sort((a, b) => Number(a.ticker === 'QQQ') - Number(b.ticker === 'QQQ'));
    const gap = 20;
    const columnWidth = (bounds.width - gap) / 2;
    const halfPanelHeight = (bounds.height - gap) / 2;
    const groups = zones.map((name, major) => {
      const frameX = !single && major === 1 ? columnWidth + gap : 0;
      const frameY = !single && major === 2 ? halfPanelHeight + gap : 0;
      const frameHeight = single || major === 1 ? bounds.height : halfPanelHeight;
      const frameWidth = single ? bounds.width : columnWidth;
      return {
        name, major, frameX, frameY, frameWidth, frameHeight,
        titleY: frameY + 21, left: frameX + 44, right: frameX + frameWidth - 22,
        top: frameY + 48, bottom: frameY + frameHeight - 56,
        tone: major === 0 ? 'up' : major === 2 ? 'down' : 'flat',
        count: rows.filter(row => row.major === major).length,
      };
    });
    const visibleGroups = single ? [groups[focus]] : groups;
    const x = (value, group) => group.left + (value / limit + 1) / 2 * (group.right - group.left);
    const y = (value, group) => group.top + (1 - value / positionLimit) / 2 * (group.bottom - group.top);
    for (const group of visibleGroups) {
      group.upperY = y(1, group);
      group.lowerY = y(-1, group);
      group.yTicks = [positionLimit, 1, 0, -1, -positionLimit].map(value => ({
        value, y: y(value, group),
        showLabel: Math.abs(value) !== 1 || (group.bottom - group.top) / (2 * positionLimit) >= 15,
      }));
      const xValues = group.right - group.left < 220
        ? [-limit, 0, limit] : [-limit, -limit / 2, 0, limit / 2, limit];
      group.xTicks = xValues.map(value => ({ value, x: x(value, group) }));
    }

    // Place QQQ first, then the other stocks from fastest to slowest.
    // Small offsets retain a visible anchor at the exact coordinate.
    const placed = [];
    for (const row of [...visibleRows].reverse()) {
      const group = groups[row.major];
      row.anchorX = x(row.speed, group);
      row.anchorY = y(row.normalizedPosition, group);
      let best = { x: row.anchorX, y: row.anchorY, score: Infinity };
      for (const dy of [0, -22, 22, -44, 44]) {
        for (const dx of [0, -22, 22, -44, 44]) {
          const px = row.anchorX + dx, py = row.anchorY + dy;
          if (px < group.left || px > group.right || py < group.top || py > group.bottom) continue;
          // Keep the circle center in its actual range band when avoiding overlap.
          const band = py < group.upperY ? 0 : py > group.lowerY ? 2 : 1;
          if (band !== row.minor) continue;
          const overlaps = placed.filter(dot => Math.hypot(px - dot.x, py - dot.y) < dotClearance).length;
          const score = overlaps * 100000 + dx * dx + 4 * dy * dy;
          if (score < best.score) best = { x: px, y: py, score };
        }
      }
      row.x = best.x;
      row.y = best.y;
      row.displaced = row.x !== row.anchorX || row.y !== row.anchorY;
      placed.push(row);
    }
    return {
      rows, pending, limit, positionLimit, bounds, groups, visibleRows, visibleGroups,
      forming: visibleRows.some(row => row.forming),
    };
  }

  function referenceLines(row, chart) {
    if (!row?.prior || !chart.visibleGroups.some(group => group.major === row.major)) return [];
    const { groups, positionLimit } = chart;
    const group = groups[row.major];
    return [
      { label: '昨高', price: row.prior.high, tone: 'up' },
      { label: '昨低', price: row.prior.low, tone: 'down' },
    ].map(line => {
      const position = (line.price - row.midpoint) / row.halfHeight;
      return { ...line, position, left: group.left, right: group.right,
        y: group.top + (1 - position / positionLimit) / 2 * (group.bottom - group.top) };
    }).filter(line => Math.abs(line.position) <= positionLimit);
  }

  return { build, referenceLines };
})();

if (typeof module !== 'undefined') module.exports = RangeDistribution;
