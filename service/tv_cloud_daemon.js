/**
 * TradingView 实时订阅 daemon (tv_cloud_daemon.js)
 * ================================================
 *
 * 常驻订阅模式：保持一条 TradingView WS 连接，持续接收 1M K线 +
 * EMA(7)（1M）+ st_channel_www（1M）的实时数据，按 1s 节流把最新
 * 快照以 JSON 行输出到 stdout，供 Python 采集器（subprocess）消费。
 *
 * 用法：
 *   node tv_cloud_daemon.js --symbols BATS:SKHY,BATS:NVDA
 *
 * 输出（stdout 每行一个 JSON）：
 *   {"type":"tick","ts":...,"symbols":{
 *     "BATS:SKHY":{"bar":{time,open,high,low,close,volume},
 *                  "ema7":168.86,
 *                  "st":{up_trend,down_trend,up_channel,down_channel,body_middle}}}}
 *   {"type":"heartbeat","ts":...}
 *
 * 周期设计：全部 1M 周期（K线/EMA7/st_channel_www 一致，交易周期）。
 */

'use strict';

const fs = require('fs');
const path = require('path');
const TradingView = require('@mathieuc/tradingview');

const ST_CHANNEL_ID = 'USER;f1157c7728c047069c21e72dcb8f94a2';
const ST_STRENGTH_ID = 'USER;aa6f71ec42fc48839479878b4a36965d';
const FAST_SLOW_MA_ID = 'USER;5c1a17e8fd934e30adb3aa7aad30a42c';
const LONG_FILTER_ID = 'USER;f16a9397f107472584dadd331304907b';
const VWAP_ID = 'STD;VWAP';
const EMA_ID = 'STD;EMA';
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

async function main() {
  const args = parseArgs(process.argv);
  const creds = JSON.parse(fs.readFileSync(args.creds, 'utf8'));

  // 加载指标（st_channel_www + st_strength + fast_slow_ma + VWAP + EMA7）
  const stIndic = await TradingView.getIndicator(
    ST_CHANNEL_ID, 'last', creds.sessionid, creds.sessionid_sign);
  const strengthIndic = await TradingView.getIndicator(
    ST_STRENGTH_ID, 'last', creds.sessionid, creds.sessionid_sign);
  const fsmIndic = await TradingView.getIndicator(
    FAST_SLOW_MA_ID, 'last', creds.sessionid, creds.sessionid_sign);
  fsmIndic.setOption('1_Resolution', '5');   // MA_1 = 5分钟
  fsmIndic.setOption('1_Length', 20);        // MA_1 = 5分钟 EMA20（快线，≈1分钟100）
  fsmIndic.setOption('2_Length', 250);       // MA_2 = 1分钟 EMA250（慢线）
  const longFilterIndic = await TradingView.getIndicator(
    LONG_FILTER_ID, 'last', creds.sessionid, creds.sessionid_sign);
  longFilterIndic.setOption('SuperTrend__Multiplier', 3.2);   // 和 st_channel_www 因子一致
  const vwapIndic = await TradingView.getIndicator(
    VWAP_ID, 'last', creds.sessionid, creds.sessionid_sign);
  const emaIndic = await TradingView.getIndicator(
    EMA_ID, 'last', creds.sessionid, creds.sessionid_sign);
  emaIndic.setOption('Length', 7);

  const client = new TradingView.Client({
    token: creds.sessionid,
    signature: creds.sessionid_sign,
  });

  // symbol -> { bar, ema7, st }
  const state = {};
  const charts = [];

  for (const symbol of args.symbols) {
    state[symbol] = { bar: null, bar_c: null, ema7: null, st: {}, st_c: {}, strength: {}, filter: {} };

    // 1M session：K线 + EMA7 + st_channel_www + st_strength + fast_slow_ma + VWAP
    const chart1m = new client.Session.Chart();
    charts.push(chart1m);
    chart1m.setMarket(symbol, { timeframe: '1', range: 150, session: 'extended' });
    const emaStudy = new chart1m.Study(emaIndic);
    const stStudy = new chart1m.Study(stIndic);
    const strengthStudy = new chart1m.Study(strengthIndic);
    const fsmStudy = new chart1m.Study(fsmIndic);
    const vwapStudy = new chart1m.Study(vwapIndic);
    const longFilterStudy = new chart1m.Study(longFilterIndic);
    chart1m.onUpdate(() => {
      if (chart1m.periods && chart1m.periods.length) {
        const b = chart1m.periods[0];   // periods 倒序 [0]=最新（实时，未收盘）
        state[symbol].bar = {
          time: b.time, open: b.open, high: b.max,
          low: b.min, close: b.close, volume: b.volume,
        };
        const bc = chart1m.periods[1];  // 上一根已收盘（稳定）
        if (bc) {
          state[symbol].bar_c = {
            time: bc.time, open: bc.open, high: bc.max,
            low: bc.min, close: bc.close, volume: bc.volume,
          };
        }
      }
    });
    emaStudy.onUpdate(() => {
      if (emaStudy.periods && emaStudy.periods.length) {
        state[symbol].ema7 = finite(emaStudy.periods[0].EMA);
      }
    });
    stStudy.onUpdate(() => {
      if (stStudy.periods && stStudy.periods.length) {
        const p = stStudy.periods[0];   // 最新（未确认，盘中实时）
        state[symbol].st = {
          up_trend: finite(p.Up_Trend),
          down_trend: finite(p.Down_Trend),
          up_channel: finite(p.Uptrend_Channel),
          down_channel: finite(p.Downtrend_Channel),
          body_middle: finite(p.Body_Middle),
          zone_up: finite(p.ST_Zone_Up),        // 第二个通道（ST回踩检测区）
          zone_down: finite(p.ST_Zone_Down),
          pierce_signal: finite(p.Pierce_Signal), // 回踩信号 1/-1/0
        };
        // 确认值：上一根已收盘 K 线（策略判断/下单全用这套，稳定不抖）
        const c = stStudy.periods[1];
        if (c) {
          state[symbol].st_c = {
            up_trend: finite(c.Up_Trend),
            down_trend: finite(c.Down_Trend),
            up_channel: finite(c.Uptrend_Channel),
            down_channel: finite(c.Downtrend_Channel),
            body_middle: finite(c.Body_Middle),
            zone_up: finite(c.ST_Zone_Up),
            zone_down: finite(c.ST_Zone_Down),
            pierce_signal: finite(c.Pierce_Signal),
          };
        }
      }
    });
    strengthStudy.onUpdate(() => {
      if (strengthStudy.periods && strengthStudy.periods.length) {
        const p = strengthStudy.periods[0];
        state[symbol].strength = {
          bars: finite(p.bars),          // 持续 K 线数
          avg_atr: finite(p.avgATR),     // 整体平均 ATR
          move_amt: finite(p.moveAmt),   // 推动幅度
          move_atr: finite(p.moveATR),   // 推动 ATR 数
        };
      }
    });
    fsmStudy.onUpdate(() => {
      if (fsmStudy.periods && fsmStudy.periods.length) {
        const p = fsmStudy.periods[0];
        state[symbol].filter.ma_fast = finite(p.MA_1);   // 快线（用户已配置）
        state[symbol].filter.ma_slow = finite(p.MA_2);   // 慢线（用户已配置）
      }
    });
    vwapStudy.onUpdate(() => {
      if (vwapStudy.periods && vwapStudy.periods.length) {
        state[symbol].filter.vwap = finite(vwapStudy.periods[0].VWAP);
      }
    });
    longFilterStudy.onUpdate(() => {
      if (longFilterStudy.periods && longFilterStudy.periods.length) {
        const p = longFilterStudy.periods[0];
        state[symbol].filter.long_candidate = finite(p.Filter_Output_10);  // 多头过滤信号 0/1
      }
    });

    chart1m.onError((...e) => console.error(`[${symbol}/1m] err:`,
      JSON.stringify(e).slice(0, 150)));
  }

  // 每秒输出一次最新快照
  setInterval(() => {
    const symbols = {};
    for (const [sym, s] of Object.entries(state)) {
      if (s.bar) symbols[sym] = { bar: s.bar, bar_c: s.bar_c, ema7: s.ema7, st: s.st, st_c: s.st_c, strength: s.strength, filter: s.filter };
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

  console.error(`daemon 订阅就绪: ${args.symbols.length} 个标的 (1M K线/EMA7 + 5M st_channel)`);
}

main().catch((e) => {
  console.error('FATAL:', e.message || e);
  process.exit(1);
});
