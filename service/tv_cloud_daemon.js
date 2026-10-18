/**
 * TradingView 实时订阅 daemon (tv_cloud_daemon.js)
 * ================================================
 *
 * 常驻订阅模式：保持一条 TradingView WS 连接，每标的 1 个 5M chart
 * session，只挂 1 个 Study（trend_state），按 1s 节流把最新快照以
 * JSON 行输出到 stdout，供 Python 采集器（subprocess）消费。
 *
 * 用法：
 *   node tv_cloud_daemon.js --symbols BATS:SKHY,BATS:NVDA
 *
 * 输出（stdout 每行一个 JSON）：
 *   {"type":"tick","ts":...,"symbols":{
 *     "BATS:SKHY":{
 *       "bar":  {time,open,high,low,close,volume},   // 实时 5M K线（未收盘）
 *       "bar_c": {...},                              // 上一根已收盘（稳定）
 *       "ts":  {trend_state,direction,ema_fast,ema_slow,supertrend,
 *               channel_edge,atr,st_duration_bars,st_push_count}, // 实时值（periods[0]）
 *       "ts_c": {...同结构}                          // 确认值（periods[1]，策略/下单用）
 *     }}}
 *   {"type":"heartbeat","ts":...}
 *
 * trend_state 指标（USER;e577100fe79942b4830783d1ceaad535，源码 trend_state.pine）：
 *   Trend_State    1 单边多头 / -1 单边空头 / 0 震荡（过滤器）
 *   EMA_Fast       EMA20，回踩入场线 1
 *   EMA_Slow       EMA100
 *   Up_Trend / Down_Trend        SuperTrend(10,3) 值，谁有效即方向
 *   Uptrend_Channel / Downtrend_Channel  channelEdge = ST ± 1×ATR，回踩入场线 2
 *   Atr_Value      ATR(10)，止损 = 限价 ∓ 1×ATR
 *   ST_Duration_Bars  当前 ST 方向延续的 5M bar 数（翻向当根为 0）
 *   ST_Push_Count     当前方向内 ST 线有利推进次数（翻向清零）
 */

'use strict';

const fs = require('fs');
const path = require('path');
const TradingView = require('@mathieuc/tradingview');

const TREND_STATE_ID = 'USER;e577100fe79942b4830783d1ceaad535';
const PUSH_INTERVAL_MS = 1000;      // 输出节流：每秒一次
const HEARTBEAT_MS = 30000;         // 心跳：每 30s

function parseArgs(argv) {
  const args = {
    symbols: [],
    creds: path.join(__dirname, 'creds.json'),
  };
  for (let i = 2; i < argv.length; i += 2) {
    const key = argv[i].replace(/^--/, '');
    const val = argv[i + 1];
    if (key === 'symbols') args.symbols = val.split(',');
    else args[key] = val;
  }
  return args;
}

function finite(v) {
  return (v !== undefined && v !== null && v < 1e99) ? v : null;
}

// 把一根 bar 的 trend_state plot 值归一化为标准结构。
// Up/Down 两个方向 plot 永远互斥（一个有效另一个 na），谁有效谁定方向。
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
  const creds = JSON.parse(fs.readFileSync(args.creds, 'utf8'));

  const trendStateIndic = await TradingView.getIndicator(
    TREND_STATE_ID, 'last', creds.sessionid, creds.sessionid_sign);
  // 输入全部用指标默认值（ATR 10 / Factor 3.0 / Channel 1.0 / 回看 6 /
  // EMA差值>1×ATR），与图表上的指标所见即所得，不做 setOption 覆盖

  const client = new TradingView.Client({
    token: creds.sessionid,
    signature: creds.sessionid_sign,
  });

  const state = {};
  const charts = [];

  for (const symbol of args.symbols) {
    state[symbol] = { bar: null, bar_c: null, ts: {}, ts_c: {} };

    // 5M session：K线 + trend_state（200 根保证 EMA100 + 回看 warmup）
    // 会话 = extended：含当天完整盘前数据的指标才与用户图表一致
    // （regular 缺盘前历史，EMA/ST/ATR 开盘时段严重失真，实测 10-13 翻车）
    const chart = new client.Session.Chart();
    charts.push(chart);
    chart.setMarket(symbol, { timeframe: '5', range: 200, session: 'extended' });
    const tsStudy = new chart.Study(trendStateIndic);

    chart.onUpdate(() => {
      if (chart.periods && chart.periods.length) {
        const b = chart.periods[0];   // periods 倒序 [0]=最新（实时，未收盘）
        state[symbol].bar = {
          time: b.time, open: b.open, high: b.max,
          low: b.min, close: b.close, volume: b.volume,
        };
        const bc = chart.periods[1];  // 上一根已收盘（稳定）
        if (bc) {
          state[symbol].bar_c = {
            time: bc.time, open: bc.open, high: bc.max,
            low: bc.min, close: bc.close, volume: bc.volume,
          };
        }
      }
    });
    tsStudy.onUpdate(() => {
      const ps = tsStudy.periods;
      if (ps && ps.length) {
        state[symbol].ts = tsFromPeriod(ps[0]);    // 实时（盘中会抖，仅显示）
        if (ps[1]) state[symbol].ts_c = tsFromPeriod(ps[1]);  // 确认（策略用）
      }
    });
    chart.onError((...e) => console.error(`[${symbol}/5m] err:`,
      JSON.stringify(e).slice(0, 150)));
  }

  // 每秒输出一次最新快照
  setInterval(() => {
    const symbols = {};
    for (const [sym, s] of Object.entries(state)) {
      if (s.bar) symbols[sym] = { bar: s.bar, bar_c: s.bar_c, ts: s.ts, ts_c: s.ts_c };
    }
    if (Object.keys(symbols).length > 0) {
      process.stdout.write(JSON.stringify({
        type: 'tick', ts: Date.now() / 1000, symbols,
      }) + '\n');
    }
  }, PUSH_INTERVAL_MS);

  // 心跳
  setInterval(() => {
    process.stdout.write(JSON.stringify({ type: 'heartbeat', ts: Date.now() / 1000 }) + '\n');
  }, HEARTBEAT_MS);

  console.error(`daemon 订阅就绪: ${args.symbols.length} 个标的 (5M K线 + trend_state)`);
}

main().catch((e) => {
  console.error('FATAL:', e.message || e);
  process.exit(1);
});
