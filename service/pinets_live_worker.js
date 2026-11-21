'use strict';

const { parentPort } = require('node:worker_threads');
const { createCalculator, getDefinition } = require('./pinets_runtime');

const version = require('../package-lock.json').packages['node_modules/pinets'].version;
const fiveMinuteKeys = ['e9', 'e20', 'e7v2', 'golden', 'trend', 'fvg', 'smart', 'attack'];
const calculators = new Map();
const sessions = new Map();

parentPort.on('message', async ({ dataset, history, advance }) => {
  const results = {};
  const errors = {};
  if ((history && !advance) || !sessions.has(dataset.symbol)) sessions.set(dataset.symbol, {});
  const byIndicator = sessions.get(dataset.symbol);
  for (const key of dataset.intervalSeconds === 60 ? ['vwap_pullback'] : fiveMinuteKeys) {
    if (key === 'attack' && !dataset.daily?.length) continue;
    try {
      if (!calculators.has(key)) {
        const inputs = key === 'attack'
          ? { showLargeRange: false, showSmallRange: false, showOpening3Range: false }
          : key === 'golden' ? { showLevels: false } : {};
        calculators.set(key, createCalculator(getDefinition(key), { version, inputs }));
      }
      const calculate = calculators.get(key);
      let rows;
      if (key === 'e7v2') {
        // Its ATR[1] and stops diverged on updateTail; keep the verified batch path.
        rows = await calculate(dataset, { tail: 2 });
      } else if (byIndicator[key]) {
        try {
          if (advance) {
            const tail = key === 'fvg' ? 0 : key === 'trend' ? dataset.bars.length : ['e9', 'attack'].includes(key) ? 175 : 2;
            rows = await byIndicator[key].advance(dataset.bars, tail);
          } else {
            rows = await byIndicator[key].replaceLast(dataset.bars.at(-1), key === 'fvg' ? 0 : 2);
          }
        } catch (_) {
          delete byIndicator[key];
        }
      }
      if (!rows) {
        const session = await calculate.open(dataset);
        byIndicator[key] = session;
        rows = session.read(key === 'fvg' ? 0 : key === 'trend' ? dataset.bars.length : ['e9', 'attack'].includes(key) ? 175 : 2);
      }
      rows.reverse();
      if (key === 'fvg') {
        let lastDirection = null;
        for (let i = rows.length - 1; i >= 0; i--) {
          if (rows[i].KFVG110) lastDirection = Math.sign(rows[i].KFVG110);
          rows[i].Last_Direction = lastDirection;
        }
        results[key] = rows.slice(0, 2);
      } else {
        results[key] = rows;
      }
    } catch (error) {
      errors[key] = String(error.message).slice(0, 200);
    }
  }
  parentPort.postMessage({ results, errors });
});
