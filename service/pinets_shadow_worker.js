'use strict';

const { parentPort } = require('node:worker_threads');
const { performance } = require('node:perf_hooks');
const { createCalculator, getDefinition } = require('./pinets_runtime');

const version = require('../package-lock.json').packages['node_modules/pinets'].version;
const definition = getDefinition('e7v2');
const calculate = createCalculator(definition, { version });

parentPort.on('message', async job => {
  const started = performance.now();
  try {
    const [output] = await calculate(job.dataset, { tail: 1 });
    if (output?.time !== job.barTime) throw new Error('EMA7 计算结果与输入 K 线不一致');
    parentPort.postMessage({ output, computeMs: performance.now() - started,
      pineVersion: definition.version, pinetsVersion: version });
  } catch (error) {
    parentPort.postMessage({ error: error.message });
  }
});
