'use strict';

const fs = require('node:fs');
const { createCalculator, getDefinition } = require('./pinets_runtime');

async function calculate({ source, inputs, version, datasets }) {
  const run = createCalculator(getDefinition('e7v2'), { source, inputs, version });
  const results = {};
  for (const dataset of datasets) results[dataset.symbol] = await run(dataset);
  return results;
}

if (require.main === module) {
  Promise.resolve().then(() => calculate(JSON.parse(fs.readFileSync(0, 'utf8')))).then(results => {
    process.stdout.write(JSON.stringify(results));
  }).catch(error => {
    process.stderr.write(String(error.message) + '\n');
    process.exitCode = 1;
  });
}
