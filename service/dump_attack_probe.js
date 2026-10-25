'use strict';
// 一次性探针：订阅「5M 进攻区域」，确认脚本可读 + 打印 plot 键名与最近几根的值
const fs = require('fs');
const path = require('path');
const TradingView = require('@mathieuc/tradingview');

const ATTACK_ZONE_ID = 'USER;8ab4f534e5874b47b24a5ce5c49acace';

async function main() {
  const symbol = process.argv[2] || 'BATS:META';
  const creds = JSON.parse(fs.readFileSync(path.join(__dirname, 'creds.json'), 'utf8'));
  const indic = await TradingView.getIndicator(ATTACK_ZONE_ID, 'last', creds.sessionid, creds.sessionid_sign);
  console.error('=== indicator meta ===');
  console.error('name:', indic.name, '| version:', indic.version);
  console.error('plots:', JSON.stringify(indic.plots, null, 2));

  const client = new TradingView.Client({ token: creds.sessionid, signature: creds.sessionid_sign });
  const chart = new client.Session.Chart();
  chart.setMarket(symbol, { timeframe: '5', range: 200, session: 'extended' });
  const study = new chart.Study(indic);

  let done = false;
  const finish = () => {
    if (done) return;
    const ps = study.periods;
    if (!ps || !ps.length) return;
    done = true;
    console.error('\n=== period keys ===');
    console.error(Object.keys(ps[0]).join(', '));
    console.error('\n=== 最近 6 根（[0]=最新实时）===');
    for (let i = 0; i < Math.min(6, ps.length); i++) {
      console.log(JSON.stringify(ps[i]));
    }
    process.exit(0);
  };
  study.onUpdate(() => setTimeout(finish, 1500));
  chart.onUpdate(() => setTimeout(finish, 1500));
  chart.onError((...e) => { console.error('chart err:', JSON.stringify(e).slice(0, 300)); process.exit(1); });
  setTimeout(() => { console.error('超时 45s'); process.exit(1); }, 45000);
}

main().catch((e) => { console.error('FATAL:', e.message || e); process.exit(1); });
