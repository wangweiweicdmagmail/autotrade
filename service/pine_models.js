'use strict';
// 与 Python 共用同一份模型声明；只映射 Pine plot，不产生交易信号。
const fs = require('fs');
const path = require('path');

function loadPineModels(file = path.join(__dirname, 'pine_strategies.json')) {
  const config = JSON.parse(fs.readFileSync(file, 'utf8'));
  if (config.version !== 1 || !Array.isArray(config.strategies)) throw new Error('Invalid Pine model config');
  const ids = new Set(), keys = new Set();
  const reserved = new Set(['bar', 'bar_c', 'bar_c2', 'ts', 'ts_c', 'fvg', 'fvg_c', 'attack', 'attack_c',
    'ss', 'ss_c', 'ema_9', 'ema_9_c', 'price_observed_at']);
  for (const model of config.strategies) {
    if (!/^[a-z][a-z0-9_]*$/.test(model.id) || ids.has(model.id)) throw new Error('Invalid/duplicate Pine model id');
    ids.add(model.id);
    for (const key of [model.realtime_key, model.confirmed_key]) {
      if (!/^[a-z][a-z0-9_]*$/.test(key) || keys.has(key) || reserved.has(key)) throw new Error('Invalid/duplicate Pine snapshot key');
      keys.add(key);
    }
    for (const field of ['entry_signal', 'direction', 'entry_price', 'long_stop', 'short_stop']) {
      if (!model.plots[model.fields[field]]) throw new Error(`Missing Pine plot mapping: ${field}`);
    }
  }
  return config.strategies;
}

function outputFromPeriod(model, period) {
  if (!period) return {};
  const values = { time: period.$time };
  for (const [key, plot] of Object.entries(model.plots)) {
    const value = period[plot];
    values[key] = typeof value === 'number' && Number.isFinite(value) && Math.abs(value) < 1e99 ? value : null;
  }
  return values;
}

function periodFromOutput(model, output) {
  if (!output) return null;
  return {
    $time: output.time,
    ...Object.fromEntries(Object.entries(model.plots).map(([field, plot]) => [plot, output[field]])),
  };
}

module.exports = { loadPineModels, outputFromPeriod, periodFromOutput };
