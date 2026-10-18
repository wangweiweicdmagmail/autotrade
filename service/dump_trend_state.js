/**
 * trend_state 历史数据一次性导出 (dump_trend_state.js)
 * ====================================================
 *
 * 拉取指定标的的 5M K线 + trend_state 指标全量 plot 值（每根 bar 一套），
 * 供历史回测（backtest_trend.py）逐 bar 喂入。
 *
 * 用法：
 *   node dump_trend_state.js --symbols NASDAQ:NVDA --range 500 --out /tmp/nvda.json
 *
 * 输出（JSON 文件或 stdout）：
 *   {"NASDAQ:NVDA": [
 *     {"time":..., "open":..., "high":..., "low":..., "close":..., "volume":...,
 *      "ts": {"trend_state":1, "direction":1, "ema_fast":..., "ema_slow":...,
 *             "supertrend":..., "channel_edge":..., "atr":...,
 *             "st_duration_bars":..., "st_push_count":...}},
 *     ...   // 按时间正序
 *   ]}
 *
 * 注意：range 要包含目标交易日 + 指标 warmup（EMA100 + 回看 6 根，
 * 建议 ≥400；extended 会话含盘前盘后 bar，一天约 190+ 根）。
 */

'use strict';

const fs = require('fs');
const path = require('path');
const TradingView = require('@mathieuc/tradingview');

const TREND_STATE_ID = 'USER;e577100fe79942b4830783d1ceaad535';

function parseArgs(argv) {
  const args = {
    symbols: [],
    range: 500,
    out: '',
    to: 0,               // 最后一根 bar 的时间戳（epoch 秒），0=最新
    creds: path.join(__dirname, 'creds.json'),
  };
  for (let i = 2; i < argv.length; i += 2) {
    const key = argv[i].replace(/^--/, '');
    const val = argv[i + 1];
    if (key === 'symbols') args.symbols = val.split(',');
    else if (key === 'range' || key === 'to') args[key] = parseInt(val, 10);
    else args[key] = val;
  }
  return args;
}

function finite(v) {
  return (v !== undefined && v !== null && v < 1e99) ? v : null;
}

// 与 tv_cloud_daemon.js 相同的归一化
function tsFromPeriod(p) {
  if (!p) return {};
  const up = finite(p.Up_Trend);
  const down = finite(p.Down_Trend);
  const upCh = finite(p.Uptrend_Channel);
  const downCh = finite(p.Downtrend_Channel);
  return {
    trend_state: finite(p.Trend_State),
    direction: up !== null ? 1 : (down !== null ? -1 : 0),
    ema_fast: finite(p.EMA_Fast),
    ema_slow: finite(p.EMA_Slow),
    supertrend: up !== null ? up : down,
    channel_edge: upCh !== null ? upCh : downCh,
    atr: finite(p.Atr_Value),
    divergence: finite(p.Ema_Divergence),   // 指标算好的发散度（所见即所得）
    st_duration_bars: finite(p.ST_Duration_Bars),
    st_push_count: finite(p.ST_Push_Count),
  };
}

async function main() {
  const args = parseArgs(process.argv);
  if (!args.symbols.length) {
    console.error('用法: node dump_trend_state.js --symbols SYM[,SYM] [--range 500] [--out file]');
    process.exit(1);
  }
  const creds = JSON.parse(fs.readFileSync(args.creds, 'utf8'));
  const indic = await TradingView.getIndicator(
    TREND_STATE_ID, 'last', creds.sessionid, creds.sessionid_sign);
  const client = new TradingView.Client({
    token: creds.sessionid,
    signature: creds.sessionid_sign,
  });

  const result = {};
  let pending = args.symbols.length;

  for (const symbol of args.symbols) {
    const chart = new client.Session.Chart();
    // 会话 = extended：含当天完整盘前数据，指标才与用户图表一致
    const opts = { timeframe: '5', range: args.range, session: 'extended' };
    // 数据锚点：默认最新；回测历史日期时以目标日尾盘为最后一根 bar 往前拉
    if (args.to > 0) opts.to = args.to;
    chart.setMarket(symbol, opts);
    const study = new chart.Study(indic);
    let done = false;
    const finish = () => {
      if (done) return;
      const bars = chart.periods;
      const tss = study.periods;
      if (!bars || !bars.length || !tss || !tss.length) return;
      done = true;
      // periods 均为倒序（[0] 最新），按索引对齐后转正序
      const rows = [];
      const n = Math.min(bars.length, tss.length);
      for (let i = n - 1; i >= 0; i--) {
        const b = bars[i];
        rows.push({
          time: b.time, open: b.open, high: b.max,
          low: b.min, close: b.close, volume: b.volume,
          ts: tsFromPeriod(tss[i]),
        });
      }
      result[symbol] = rows;
      console.error(`[${symbol}] 导出 ${rows.length} 根 5M bar（含 trend_state）`);
      if (--pending === 0) {
        const payload = JSON.stringify(result);
        // >64KB 时 process.exit 会截断未 flush 的 stdout，必须在回调里退出
        if (args.out) {
          fs.writeFileSync(args.out, payload);
          console.error(`已写入 ${args.out}`);
          process.exit(0);
        }
        process.stdout.write(payload + '\n', () => process.exit(0));
      }
    };
    study.onUpdate(() => setTimeout(finish, 500));   // 等 chart/study 都就绪
    chart.onUpdate(() => setTimeout(finish, 500));
    chart.onError((...e) => {
      console.error(`[${symbol}] err:`, JSON.stringify(e).slice(0, 200));
      if (--pending === 0) process.exit(1);
    });
  }
  // 超时兜底
  setTimeout(() => {
    console.error('超时（60s）未收齐数据，退出');
    process.exit(1);
  }, 60000);
}

main().catch((e) => {
  console.error('FATAL:', e.message || e);
  process.exit(1);
});
