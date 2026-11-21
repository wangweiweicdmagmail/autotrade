/**
 * trend_state 历史数据一次性导出 (dump_trend_state.js)
 * ====================================================
 *
 * 拉取指定标的的 5M K线 + trend_state + ema9_autotrade 全量 plot 值，
 * 供历史回测逐 bar 喂入；EMA9 金字塔使用每行 e9。
 *
 * 用法：
 *   node dump_trend_state.js --symbols NASDAQ:NVDA --range 500 --out /tmp/nvda.json
 *
 * 输出（JSON 文件或 stdout）：
 *   {"NASDAQ:NVDA": [
 *     {"time":..., "open":..., "high":..., "low":..., "close":..., "volume":...,
 *      "ts": {"trend_state":1, "direction":1, "ema_fast":..., "ema_9":..., "ema_slow":...,
 *             "supertrend":..., "channel_edge":..., "atr":...,
 *             "st_duration_bars":..., "st_push_count":...},
 *      "fvg": {"fvg_cum_count":..., "fvg_last_top":..., "fvg_last_bottom":...,
 *              "fvg_event":..., "fvg_last_dir":...}},
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
const { emaFromPeriods } = require('./ema.js');
const { loadIndicatorWithText } = require('./pine_text.js');

const TREND_STATE_ID = 'USER;e577100fe79942b4830783d1ceaad535';
const FVG_ID = 'USER;5ec7d04179b3429ca9286db452352d85';
const SMART_STOP_ID = 'USER;ca26e3ab587841e2adb38cfbfd0bec41';
const { loadPineModels, outputFromPeriod } = require('./pine_models.js');
const PINE_MODELS = loadPineModels().filter(model => !model.local_only);

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

// smart_stop 归一化（与 tv_cloud_daemon.js 相同）：合入 ts
function ssFromPeriod(p) {
  if (!p) return {};
  return {
    smart_stop_long: finite(p.Smart_Stop_Long),
    smart_stop_short: finite(p.Smart_Stop_Short),
  };
}

// 与 tv_cloud_daemon.js 相同的 FVG 归一化（键名映射见 daemon 文件头）。
// fvg_last_dir：自 idx 向前扫描 periods 历史中最后一个非零 fvg_event。
function fvgFromPeriod(p, periods, idx) {
  if (!p) return {};
  let lastDir = null;
  if (periods) {
    for (let i = idx; i < periods.length; i++) {
      const ev = finite(periods[i] && periods[i].KFVG110);
      if (ev !== null && ev !== 0) { lastDir = ev > 0 ? 1 : -1; break; }
    }
  }
  return {
    time: p.$time,   // 该值所属 bar 的时间戳（与 daemon 输出对齐）
    fvg_cum_count: finite(p.STFVG),
    fvg_last_top: finite(p.FVG),
    fvg_last_bottom: finite(p.FVG_2),
    fvg_event: finite(p.KFVG110),
    fvg_last_dir: lastDir,
  };
}

async function main() {
  const args = parseArgs(process.argv);
  if (!args.symbols.length) {
    console.error('用法: node dump_trend_state.js --symbols SYM[,SYM] [--range 500] [--out file]');
    process.exit(1);
  }
  const creds = JSON.parse(fs.readFileSync(args.creds, 'utf8'));
  const indic = await loadIndicatorWithText(TradingView, TREND_STATE_ID, creds);
  const fvgIndic = await loadIndicatorWithText(TradingView, FVG_ID, creds);
  const ssIndic = await loadIndicatorWithText(TradingView, SMART_STOP_ID, creds);
  const modelIndicators = await Promise.all(PINE_MODELS.map(async model => ({
    model, indicator: await loadIndicatorWithText(TradingView, model.indicator_id, creds),
  })));
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
    const fvgStudy = new chart.Study(fvgIndic);
    const ssStudy = new chart.Study(ssIndic);
    const modelStudies = modelIndicators.map(({ model, indicator }) => ({ model, study: new chart.Study(indicator) }));
    let done = false;
    const finish = () => {
      if (done) return;
      const bars = chart.periods;
      const tss = study.periods;
      const fss = fvgStudy.periods;
      const sss = ssStudy.periods;
      const modelPeriods = modelStudies.map(({ model, study }) => ({ model, periods: study.periods }));
      if (!bars || !bars.length || !tss || !tss.length || !fss || !fss.length
          || !sss || !sss.length || modelPeriods.some(({ periods }) => !periods || !periods.length)) return;
      done = true;
      // periods 均为倒序（[0] 最新），按索引对齐后转正序
      const rows = [];
      const n = Math.min(bars.length, tss.length, fss.length, sss.length, ...modelPeriods.map(({ periods }) => periods.length));
      for (let i = n - 1; i >= 0; i--) {
        const b = bars[i];
        rows.push({
          time: b.time, open: b.open, high: b.max,
          low: b.min, close: b.close, volume: b.volume,
          ts: Object.assign(tsFromPeriod(tss[i]), ssFromPeriod(sss[i]),
                            { ema_9: emaFromPeriods(bars, i, 9) }),
          fvg: fvgFromPeriod(fss[i], fss, i),
          ...Object.fromEntries(modelPeriods.map(({ model, periods }) => [
            model.realtime_key, outputFromPeriod(model, periods[i]),
          ])),
        });
      }
      result[symbol] = rows;
      console.error(`[${symbol}] 导出 ${rows.length} 根 5M bar（含 trend_state + ema9_autotrade）`);
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
    fvgStudy.onUpdate(() => setTimeout(finish, 500));
    ssStudy.onUpdate(() => setTimeout(finish, 500));
    for (const { study } of modelStudies) study.onUpdate(() => setTimeout(finish, 500));
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
