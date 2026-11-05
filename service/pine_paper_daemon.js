'use strict';
// Read-only, one-symbol Pine subscription for the isolated paper workspace.
const path = require('path');
const fs = require('fs');
const { loadPineModels, outputFromPeriod } = require('./pine_models');

function parseArgs(argv) {
  const args = { symbol: 'NASDAQ:NVDA', model: 'ema9_pyramid', session: '24h', once: false };
  for (let i = 2; i < argv.length; i++) {
    const key = argv[i].replace(/^--/, '');
    if (key === 'once') args.once = true;
    else args[key] = argv[++i];
  }
  if (!['regular', 'extended', '24h'].includes(args.session)) throw new Error('Unsupported session');
  if (!/^[A-Z0-9_]+:[A-Z0-9_.!\-]+$/.test(args.symbol)) throw new Error('Invalid symbol');
  return args;
}

function snapshot(chart, study, model, requested, observedAt) {
  const bars = (chart.periods || []).slice(0, 200).map(row => ({
    time: row.time, open: row.open, high: row.max ?? row.high, low: row.min ?? row.low,
    close: row.close, volume: row.volume,
  })).sort((a, b) => a.time - b.time);
  const bar = bars[bars.length - 1];
  const barC = bars[bars.length - 2];
  const periods = study.periods || [];
  const infos = chart.infos || {};
  return { bar, bar_c: barC, bars, history_outputs: periods.slice(0, 200).map(p => outputFromPeriod(model, p)), price_observed_at: observedAt,
    [model.realtime_key]: outputFromPeriod(model, periods.find(p => p.$time === bar?.time)),
    [model.confirmed_key]: outputFromPeriod(model, periods.find(p => p.$time === barC?.time)),
    session: { requested, resolved: infos.subsession_id || null, timezone: infos.timezone || null,
      adjustment: 'splits', indicator_inputs: 'defaults', symbol: infos.full_name || infos.name || null },
  };
}

async function main() {
  const args = parseArgs(process.argv);
  const TradingView = require('@mathieuc/tradingview');
  const { loadIndicatorWithText } = require('./pine_text');
  const model = loadPineModels().find(m => m.id === args.model);
  if (!model) throw new Error('Unregistered Pine model');
  const creds = JSON.parse(fs.readFileSync(path.join(__dirname, 'creds.json'), 'utf8'));
  const indicator = await loadIndicatorWithText(TradingView, model.indicator_id, creds);
  const client = new TradingView.Client({ token: creds.sessionid, signature: creds.sessionid_sign });
  const chart = new client.Session.Chart();
  let observedAt = 0;
  chart.onUpdate(() => { observedAt = Date.now() / 1000; });
  chart.onError(() => { process.stdout.write(JSON.stringify({ type: 'error', reason: 'TradingView 图表订阅失败，可能不支持所选时段或标的' }) + '\n'); });
  chart.setMarket(args.symbol, { timeframe: '5', range: 200, session: args.session, adjustment: 'splits' });
  const study = new chart.Study(indicator);
  study.onError(() => { process.stdout.write(JSON.stringify({ type: 'error', reason: 'Pine 指标加载或计算失败，请核对权限与字段' }) + '\n'); });
  let timer;
  const close = () => { clearInterval(timer); try { chart.delete(); client.end(); } finally { process.exit(0); } };
  process.on('SIGTERM', close);
  process.on('SIGINT', close);
  timer = setInterval(() => {
    const data = snapshot(chart, study, model, args.session, observedAt);
    data.session.pine_version = indicator.pineVersion || null;
    process.stdout.write(JSON.stringify({ type: 'tick', ts: Date.now() / 1000, symbols: { [args.symbol]: data } }) + '\n');
    if (args.once && data.bar && Object.keys(data[model.realtime_key]).length) close();
  }, 1000);
  if (args.once) setTimeout(() => { process.stderr.write('Read-only probe timeout\n'); process.exit(2); }, 45000).unref();
}
if (require.main === module) main().catch(error => { process.stderr.write(`夜盘数据连接失败，请核对凭据、网络或指标权限 (${error.name || 'Error'})\n`); process.exit(1); });
module.exports = { parseArgs, snapshot };
