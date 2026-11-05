'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const { priorBodyEma10 } = require('../service/candle_metrics.js');

test('实体EMA排除目标K线，最近实体变化具有更高权重', () => {
  const history = Array.from({ length: 22 }, () => ({ open: 100, close: 101 }));
  history[0].close = 1000;
  history[1].close = 103;
  assert.ok(Math.abs(priorBodyEma10(history, 0) - (1 + 2 * 2 / 11)) < 1e-12);
  assert.equal(priorBodyEma10(history, 1), 1);
  history[1].close = 101;
  history[2].close = 103;
  assert.ok(Math.abs(priorBodyEma10(history, 0) - (1 + 2 * 2 / 11 * 9 / 11)) < 1e-12);
  history[0].close = 101;
  assert.ok(Math.abs(priorBodyEma10(history, 0) - (1 + 2 * 2 / 11 * 9 / 11)) < 1e-12);
});

test('实体EMA用绝对实体、十根预热，零基线有效，缺口后重新预热', () => {
  const periods = Array.from({ length: 11 }, (_, i) => ({ open: 100, close: i % 2 ? 99 : 101 }));
  assert.equal(priorBodyEma10(periods, 0), 1);
  assert.equal(priorBodyEma10(periods.slice(1), 0), null);
  periods[3].close = null;
  assert.equal(priorBodyEma10(periods, 0), null);
  for (const bar of periods) bar.close = bar.open;
  assert.equal(priorBodyEma10(periods, 0), 0);
});

const { priorRegularEngulfing4 } = require('../service/candle_metrics.js');
function candle(time, open, close) { return { time, open, close }; }
const opening = Date.parse('2026-11-04T09:30:00-05:00') / 1000;

test('反包仅回看前四根，信号本身及盘前配对均排除', () => {
  const ascending = [
    candle(opening - 300, 100, 103),  // Not part of the regular-session comparisons.
    candle(opening, 102, 99),
    candle(opening + 300, 100, 103),
    candle(opening + 600, 101, 101.5),
    candle(opening + 900, 101.5, 102),
    candle(opening + 1200, 102, 103),
    candle(opening + 1500, 103, 104),
    candle(opening + 1800, 104, 100), // Evaluated candle must not affect its own filter.
  ];
  const periods = ascending.reverse();
  assert.equal(priorRegularEngulfing4(periods, 0), 0);
  assert.equal(priorRegularEngulfing4(periods, 5), 0);
  assert.equal(priorRegularEngulfing4(periods, 4), 1);
});

test('反包用收盘跨前根开盘，不用影线或开盘覆盖，缺根时等待', () => {
  const periods = [
    candle(opening + 900, 105, 106),
    candle(opening + 600, 104, 101),
    candle(opening + 300, 102, 103),
    candle(opening, 101, 102),
  ];
  assert.equal(priorRegularEngulfing4(periods, 0), 2);
  periods[1].close = 102; // Equal to the previous open is not an engulf.
  assert.equal(priorRegularEngulfing4(periods, 0), 0);
  periods[1] = candle(opening + 600, 101, 105);
  periods[2] = candle(opening + 300, 104, 102);
  assert.equal(priorRegularEngulfing4(periods, 0), 1);
  assert.equal(priorRegularEngulfing4(periods.filter((_, i) => i !== 2), 0), null);
});

const { breakoutContext } = require('../service/candle_metrics.js');
test('突破上下文读取对应时间Pine EMA与斜率，缺失不另算', () => {
  const periods = Array.from({length:4}, (_, i) => ({time:opening+(5-i)*300,open:100+i,close:102+i,max:104+i,min:99+i}));
  const models = new Map(periods.map((p,i) => [p.time,{time:p.time,ema_9:70+i,ema_slope_atr:2+i}]));
  const result = breakoutContext(periods, 0, models);
  assert.deepEqual(result.ema7_previous3.map(p => p.ema7), [71,72,73]);
  assert.equal(result.ema_slope_atr, 2);
  assert.equal(result.previous_high, 105);
  models.delete(periods[2].time);
  assert.equal(breakoutContext(periods,0,models).ema7_previous3[1].ema7,null);
});

const {historicalBreakoutContext} = require('../service/candle_metrics.js');
test('历史上下文仅保留突破扫描窗口，OHLC行本身不裁掉', () => {
  const periods = [
    candle(opening+12*300,100,101), // 10:30
    candle(opening+11*300,100,101), // 10:25
    candle(opening+2*300,100,101),  // 09:40
    candle(opening+1*300,100,101),  // 09:35
  ];
  assert.deepEqual(historicalBreakoutContext(periods,0,new Map()),{});
  assert.equal(historicalBreakoutContext(periods,1,new Map()).context_time,opening+11*300);
  assert.equal(historicalBreakoutContext(periods,2,new Map()).context_time,opening+2*300);
  assert.deepEqual(historicalBreakoutContext(periods,3,new Map()),{});
  assert.equal(periods.length,4);
});
