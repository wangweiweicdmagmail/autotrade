const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { loadPineModels, outputFromPeriod } = require('../service/pine_models');
const models = loadPineModels();
assert.equal(models[0].name, 'EMA9-ST-ATR');
assert.deepEqual(outputFromPeriod(models[0], {
  $time: 1000, Direction: 1, ATR: 2, Touch_EMA9: 1, EMA_9: 100, Long_Stop: 99, Short_Stop: 101,
}), { time: 1000, direction: 1, atr: 2, touch: 1, ema_9: 100, long_stop: 99, short_stop: 101 });
const other = { id: 'fixture_model', realtime_key: 'fixture', confirmed_key: 'fixture_c',
  fields: { entry_signal: 'signal', direction: 'dir', entry_price: 'price', long_stop: 'sl', short_stop: 'ss' },
  plots: { signal: 'Signal', dir: 'Bias', price: 'Price', sl: 'StopLong', ss: 'StopShort' } };
assert.equal(outputFromPeriod(other, { $time: 10, Signal: 0, Bias: -1, Price: 100, StopLong: 99, StopShort: 101 }).signal, 0);
assert.equal(outputFromPeriod(other, { StopLong: Infinity }).sl, null);
const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'autotrade-pine-'));
try {
  const file = path.join(dir, 'models.json');
  fs.writeFileSync(file, JSON.stringify({ version: 1, strategies: [models[0], other] }));
  assert.equal(loadPineModels(file).length, 2);
  fs.writeFileSync(file, JSON.stringify({ version: 1, strategies: [models[0], { ...other, realtime_key: 'bar' }] }));
  assert.throws(() => loadPineModels(file), /snapshot key/);
} finally {
  fs.rmSync(dir, { recursive: true, force: true });
}
console.log('Pine model mappings and configuration checks passed');
