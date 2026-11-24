'use strict';
// One read-only historical chart. This process never creates TradingView studies.
const fs = require('node:fs');
const path = require('node:path');
const TradingView = require('@mathieuc/tradingview');
const {chartSession}=require('./chart_snapshot');

async function fetchBars(symbol, range, cutoff, creds, session='regular') {
  const client = new TradingView.Client({ token: creds.sessionid, signature: creds.sessionid_sign });
  const chart = new client.Session.Chart();
  try {
    return await new Promise((resolve, reject) => {
      const timer = setTimeout(() => fail('TradingView 历史 K 线请求超时'), 25000);
      function fail(message) {
        clearTimeout(timer);
        reject(new Error(message));
      }
      client.onError(() => fail('TradingView 连接失败，请核对网络或登录凭据'));
      client.onDisconnected(() => fail('TradingView 历史连接已断开'));
      chart.onError(() => fail('TradingView 无法加载该标的的历史 K 线'));
      chart.onUpdate(changes => {
        if (!changes.includes('$prices') || chart.periods.length < range) return;
        const infos = chart.infos || {};
        if (!chartSession(infos,session).accepted) {
          fail('TradingView 未确认所请求的交易时段');
          return;
        }
        clearTimeout(timer);
        resolve({
          metadata: {
            timezone: infos.timezone,
            minmove: infos.minmov ?? infos.minmove,
            pricescale: infos.pricescale,
            session, resolved_session: infos.subsession_id,
          },
          bars: chart.periods.map(({ time, open, max, min, close, volume }) => ({
            time, open, high: max, low: min, close, volume,
          })).sort((a, b) => a.time - b.time),
        });
      });
      chart.setMarket(symbol, { timeframe: '5', range, to: cutoff, session:session==='default'?undefined:session, adjustment: 'splits' });
    });
  } finally {
    chart.delete();
    await client.end();
  }
}

async function main() {
  const [symbol, count, anchor, session='regular'] = process.argv.slice(2);
  const range = Number(count);
  const cutoff = Number(anchor);
  if (!/^[A-Z0-9_]+:[A-Z0-9_.!\-]+$/.test(symbol || '') ||
      !Number.isSafeInteger(range) || range < 1 || range > 5001 ||
      !Number.isSafeInteger(cutoff) || cutoff <= 0 || !['regular','extended','24h','default'].includes(session)) throw new Error('历史 K 线请求参数无效');
  let creds;
  try {
    creds = JSON.parse(fs.readFileSync(path.join(__dirname, 'creds.json'), 'utf8'));
  } catch {
    throw new Error('缺少可用的 TradingView 登录凭据');
  }
  if (!creds.sessionid || !creds.sessionid_sign) throw new Error('TradingView 登录凭据不完整');
  const data = await fetchBars(symbol, range, cutoff, creds,session);
  process.stdout.write(JSON.stringify(data));
}

main().catch(error => {
  process.stderr.write(String(error.message) + '\n');
  process.exitCode = 1;
});
