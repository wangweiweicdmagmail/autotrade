/**
 * TradingView 实时订阅 daemon (tv_cloud_daemon.js)
 * ================================================
 *
 * 常驻订阅模式：保持一条 TradingView WS 连接，每标的 1 个 5M chart
 * session，挂 5 个 Study（trend_state + FVG Finder+ + smart_stop +
 * ema9_autotrade + 5M 进攻区域），按 1s 节流输出快照。ema9_autotrade
 * 输出 e9/e9_c，分别供金字塔实时建仓和确认管理。
 * smart_stop 的值（Smart_Stop_Long/Short）归一化后合入 ts/ts_c 的
 * smart_stop_long / smart_stop_short 字段；EMA9 由同一 chart 的 5M 收盘价
 * 计算（Length=9/Source=close/Offset=0），合入 ts/ts_c.ema_9。
 *
 * 用法：
 *   node tv_cloud_daemon.js --symbols BATS:SKHY,BATS:NVDA
 *   node tv_cloud_daemon.js --symbols ... --batch-size 6 --batch-delay-ms 3000
 *
 * 启动与自愈（2026-10-24 修复订阅风暴）：
 *   - 分批发起 chart 订阅（默认每批 6 个、批间 3s，--batch-size /
 *     --batch-delay-ms 可调）：一次性并发建几十个 chart 会被 TV 服务端
 *     限流，实测卡在部分标的（无 K线 / attack study 无值）且不自愈。
 *   - 巡检自愈（每 30s）：chart 创建超 90s 仍无 bar、或 attack study
 *     超 90s 无 pmh 值 → chart.delete() 后销毁重建（逐个间隔 1.5s，
 *     单 chart 上限 10 次防 thrash），重建计数与原因写 stderr。
 *
 * 输出（stdout 每行一个 JSON）：
 *   {"type":"tick","ts":...,"symbols":{
 *     "BATS:SKHY":{
 *       "bar":  {time,open,high,low,close,volume},   // 实时 5M K线（未收盘）
 *       "bar_c": {...},                              // 上一根已收盘（稳定）
 *       "bar_c2": {...},                             // 上上根已收盘（periods[2]，回踩单定价基准）
 *       "ts":  {trend_state,direction,ema_fast,ema_slow,supertrend,
 *               channel_edge,atr,st_duration_bars,st_push_count}, // 实时值（periods[0]）
 *       "ts_c": {...同结构},                         // 确认值（periods[1]，策略/下单用）
 *       "fvg":  {fvg_cum_count,fvg_last_top,fvg_last_bottom,
 *                fvg_event,fvg_last_dir},            // FVG 实时值（periods[0]）
 *       "fvg_c": {...同结构}                         // FVG 确认值（periods[1]，策略/选股用）
 *       "attack":  {time,bar_color,pmh,pml},         // 进攻区域实时值（periods[0]）
 *       "attack_c": {...同结构}                      // 进攻区域确认值（periods[1]，盘前区间注入用）
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
 *
 * FVG Finder+ 指标（USER;5ec7d04179b3429ca9286db452352d85）：
 *   ⚠️ plot 键名 = 原标题剥离非 ASCII 字符 + 重名加后缀（实测）。
 *   在 TV 上改 plot 标题会让键名静默变化，字段将全部变 null！
 *   STFVG    ← 'ST同向FVG累积计数'        → fvg_cum_count（ST 同向 FVG 累积，转向清零）
 *   FVG      ← '最后FVG上沿'              → fvg_last_top
 *   FVG_2    ← '最后FVG下沿'              → fvg_last_bottom（与上沿撞名加后缀）
 *   KFVG110  ← '当前K线FVG信号(1涨/-1跌/0无)' → fvg_event（1 需求/-1 供应/0 无）
 *   fvg_last_dir：最后 FVG 方向，daemon 自 periods 历史向前扫描最后一个
 *   非零 fvg_event 推导（上下沿只在新 FVG 形成时更新，两者天然对应）；
 *   200 根窗口内无 FVG 事件则为 null。保存版另有 plot_4/5（_FVG_/_FVG__2），忽略。
 *
 * 5M 进攻区域指标（USER;8ab4f534e5874b47b24a5ce5c49acace，
 * 「5M 进攻区域- 方向过滤器,双区间」，plot 标题全 ASCII 无剥键问题）：
 *   Bar_Color      0/1（na 时为 1e100，finite() 转 null）
 *   premarketHigh  当天盘前高点（指标 TV 侧自算 04:00–09:25 ET 口径，
 *                  实测与 research.db 盘前区间精确一致，权威来源）
 *   premarketLow   当天盘前低点
 */

'use strict';

const fs = require('fs');
const path = require('path');
const TradingView = require('@mathieuc/tradingview');
const { loadIndicatorWithText } = require('./pine_text.js');
const { chartRetryReason } = require('./chart_health.js');
const { priorBodyEma10, priorRegularEngulfing4, breakoutContext, historicalBreakoutContext } = require('./candle_metrics.js');
const { emaFromPeriods } = require('./ema.js');
const { loadPineModels, outputFromPeriod } = require('./pine_models.js');
const PINE_MODELS = loadPineModels();

const TREND_STATE_ID = 'USER;e577100fe79942b4830783d1ceaad535';
const FVG_ID = 'USER;5ec7d04179b3429ca9286db452352d85';
const SMART_STOP_ID = 'USER;ca26e3ab587841e2adb38cfbfd0bec41';
const ATTACK_ZONE_ID = 'USER;8ab4f534e5874b47b24a5ce5c49acace';
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

const _args = parseArgs(process.argv);
// 分批订阅参数：一次性并发建几十个 chart 会被 TV 服务端限流（实测卡死
// 部分标的且不自愈），每批 6 个、批间 3s，51 标的约 30s 内全部发起
const BATCH_SIZE = Math.max(1, parseInt(_args['batch-size'] || '6', 10) || 6);
const BATCH_DELAY_MS = Math.max(0, parseInt(_args['batch-delay-ms'] || '3000', 10) || 0);
const INSPECT_INTERVAL_MS = 30000;    // 巡检间隔：死 chart 自愈检查
const READY_GRACE_MS = 90000;         // chart 创建超过 90s 仍无 bar → 重建
const ATTACK_GRACE_MS = 90000;        // attack study 超过 90s 无 pmh 值 → 聚合告警
const REBUILD_STAGGER_MS = 1500;      // 重建逐个间隔（别一次性全砸）
const MAX_REBUILDS = 10;              // 单 chart 重建上限（防无限 thrash）

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
    time: p.$time,
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

// smart_stop 指标（USER;ca26e3ab587841e2adb38cfbfd0bec41，源码 smart_stop.pine）：
//   Smart_Stop_Long / Smart_Stop_Short  智能止损价（结构极值+ATR钳制，
//   指标内部按 close 计算距离；控制端用距离套到实际入场价上）
//   归一化后合入 ts/ts_c：smart_stop_long / smart_stop_short
function ssFromPeriod(p) {
  if (!p) return {};
  return {
    smart_stop_long: finite(p.Smart_Stop_Long),
    smart_stop_short: finite(p.Smart_Stop_Short),
  };
}

// 「5M 进攻区域」指标（USER;8ab4f534e5874b47b24a5ce5c49acace）：
//   Bar_Color      0/1（na = 1e100，finite 转 null）
//   premarketHigh / premarketLow  当天盘前区间（TV 侧自算，权威来源，
//   供 console 注入 BreakoutTracker，盘中重启控制台也能拿到正确区间）
function attackFromPeriod(p) {
  if (!p) return {};
  // plot 键名 = 标题剥离非 ASCII。用户改过指标后 premarketHigh/premarketLow
  // 的标题变成中文/空标题，WS 键名退化为 plot_1/_2（实测 HOOD 验证：
  // plot_1=98.1=周五盘前高，_2=96=盘前低）——两套键名都兼容。
  // K/K_2 = 旧版开盘前三根 5M K 线高低点。
  const pmhRaw = p.premarketHigh != null ? p.premarketHigh : p.plot_1;
  const pmlRaw = p.premarketLow != null ? p.premarketLow : p._2;
  return {
    time: p.$time,   // 该值所属 bar 的时间戳：消费方用它校验 attack_c 与 bar_c 对齐
    bar_color: finite(p.Bar_Color),
    pmh: finite(pmhRaw),
    pml: finite(pmlRaw),
    opening3High: finite(p.opening3High ?? p.K),
    opening3Low: finite(p.opening3Low ?? p.K_2),
    opening3Distance: finite(p.Opening3_Distance),
  };
}

// 把一根 bar 的 FVG Finder+ plot 值归一化为标准结构。
// 键名是中文 plot 标题被剥离非 ASCII 后的实测结果（见文件头映射表）。
// fvg_last_dir：上下沿只在新 FVG 形成时更新，方向 = 自 idx 向前扫描
// periods 历史中最后一个非零 fvg_event；扫不到（窗口内无 FVG）为 null。
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
    time: p.$time,   // 该值所属 bar 的时间戳：消费方用它校验 fvg_c 与 bar_c 对齐
    fvg_cum_count: finite(p.STFVG),
    fvg_last_top: finite(p.FVG),
    fvg_last_bottom: finite(p.FVG_2),
    fvg_event: finite(p.KFVG110),
    fvg_last_dir: lastDir,
  };
}

function closedAtrSma10(periods, start = 1) {
  if (periods.length < start + 11) return null;
  let total = 0;
  for (let i = start; i < start + 10; i++) {
    const bar = periods[i];
    const high = finite(bar.max);
    const low = finite(bar.min);
    const previousClose = finite(periods[i + 1].close);
    if (high === null || low === null || previousClose === null
        || low <= 0 || high < low || previousClose <= 0) return null;
    total += Math.max(high - low, Math.abs(high - previousClose), Math.abs(low - previousClose));
  }
  return total / 10;
}

async function main() {
  const args = _args;
  const creds = JSON.parse(fs.readFileSync(args.creds, 'utf8'));

  let loaded = 0;
  const total = PINE_MODELS.length + 4;
  const report = (event) => process.stdout.write(JSON.stringify({
    type: 'indicator_startup', loaded, total, ...event,
  }) + '\n');
  async function loadStartupIndicator(id, name) {
    report({ name });
    try {
      const indicator = await loadIndicatorWithText(TradingView, id, creds);
      loaded++;
      report({ loaded_id: id, name });
      return indicator;
    } catch (error) {
      if (error.cacheIssue) report({ cache_issue: { id, name, ...error.cacheIssue } });
      throw error;
    }
  }

  // 加密正文注入（2026-10-27 起 create_study 必须携带 text，见 pine_text.js）
  const trendStateIndic = await loadStartupIndicator(TREND_STATE_ID, "趋势状态");
  // 输入全部用指标默认值（ATR 10 / Factor 3.0 / Channel 1.0 / 回看 6 /
  // EMA差值>1×ATR），与图表上的指标所见即所得，不做 setOption 覆盖
  const fvgIndic = await loadStartupIndicator(FVG_ID, "FVG Finder+");
  // FVG 同样用指标默认输入（过滤器 Off / ST 3.0×10 等），与图表所见即所得
  const ssIndic = await loadStartupIndicator(SMART_STOP_ID, "智能止损");
  // smart_stop 同样用默认输入（ATR10 / 回看 3 / 钳制 1~2×ATR）
  const modelIndicators = [];
  for (const model of PINE_MODELS) {
    try {
      modelIndicators.push({ model, indicator: await loadStartupIndicator(model.indicator_id, model.name || model.id) });
    } catch (e) {
      console.error(`[pine:${model.id}] 指标加载失败，该模型不可启动: ${(e.message || e).toString().slice(0, 240)}`);
    }
  }
  let attackIndic = null;
  try {
    attackIndic = await loadStartupIndicator(ATTACK_ZONE_ID, "5M 进攻区域");
  } catch (e) {
    console.error(`[attack] 指标加载失败，降级为无进攻区域数据，核心行情继续: ${(e.message || e).toString().slice(0, 240)}`);
  }
  // 进攻区域同样用指标默认输入（双区间/方向过滤器均为图表默认值）

  const client = new TradingView.Client({
    token: creds.sessionid,
    signature: creds.sessionid_sign,
  });

  const state = {};
  const entries = {};   // symbol -> {chart, studies, createdAt, rebuilds, rebuilding, gaveUp}

  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

  // 核心 3 个 Study 必须可用；attack 是可选增强，失败时不影响核心行情。
  // state[symbol] 对象跨重建复用（推送循环只认 state，无感）
  function spawnChart(symbol, rebuilds = 0) {
    const chart = new client.Session.Chart();
    const snapshot = state[symbol];
    snapshot.breakoutBars = [];
    snapshot.breakoutAtr = null;
    snapshot.breakoutRanges = null;
    snapshot.breakoutChartPeriods = [];
    snapshot.breakoutModelPeriods = null;
    chart.setMarket(symbol, { timeframe: '5', range: 200, session: 'extended' });
    const tsStudy = new chart.Study(trendStateIndic);
    const fvgStudy = new chart.Study(fvgIndic);
    const ssStudy = new chart.Study(ssIndic);
    const modelStudies = modelIndicators.map(({ model, indicator }) => ({ model, study: new chart.Study(indicator) }));
    const attackStudy = attackIndic ? new chart.Study(attackIndic) : null;
    entries[symbol] = {
      chart, studies: { attack: attackStudy, trend: tsStudy },
      createdAt: Date.now(), rebuilds, rebuilding: false, gaveUp: false,
    };

    chart.onUpdate(() => {
      if (entries[symbol]?.chart !== chart) return;
      const ent = entries[symbol];
      if (ent) ent.rebuilding = false;   // 首批数据到达 = 重建完成
      if (chart.periods && chart.periods.length) {
        state[symbol].breakoutChartPeriods = chart.periods.slice(0, 175);
        state[symbol].ema_9 = emaFromPeriods(chart.periods, 0, 9);
        state[symbol].ema_9_c = emaFromPeriods(chart.periods, 1, 9);
        const b = chart.periods[0];   // periods 倒序 [0]=最新（实时，未收盘）
        state[symbol].bar = {
          time: b.time, open: b.open, high: b.max,
          low: b.min, close: b.close, volume: b.volume,
          atr_sma10: closedAtrSma10(chart.periods, 0),
          body_ema10: priorBodyEma10(chart.periods, 0),
          prior_engulfing4: priorRegularEngulfing4(chart.periods, 0),
        };
        state[symbol].breakoutBars = chart.periods.slice(1, 161).map((p, index) => [
          p.time, p.open, p.max, p.min, p.close, closedAtrSma10(chart.periods, index + 1),
          priorBodyEma10(chart.periods, index + 1), priorRegularEngulfing4(chart.periods, index + 1),
        ]);
        state[symbol].closedPrices = new Map(chart.periods.slice(1, 21).map(p => [p.time, p.close]));
        const bc = chart.periods[1];  // 上一根已收盘（稳定）
        if (bc) {
          state[symbol].bar_c = {
            time: bc.time, open: bc.open, high: bc.max,
            low: bc.min, close: bc.close, volume: bc.volume,
            atr_sma10: closedAtrSma10(chart.periods),
            body_ema10: priorBodyEma10(chart.periods, 1),
            prior_engulfing4: priorRegularEngulfing4(chart.periods, 1),
          };
        }
        const b2 = chart.periods[2];  // 上上根已收盘（回踩单定价基准，随新 bar 滚动）
        if (b2) {
          state[symbol].bar_c2 = {
            time: b2.time, open: b2.open, high: b2.max,
            low: b2.min, close: b2.close, volume: b2.volume,
          };
        }
      }
    });
    tsStudy.onUpdate(() => {
      if (entries[symbol]?.chart !== chart) return;
      const ps = tsStudy.periods;
      if (ps && ps.length) {
        state[symbol].breakoutAtr = new Map(ps.slice(1, 161).map(p => [p.$time, finite(p.Atr_Value)]));
        state[symbol].ts = tsFromPeriod(ps[0]);    // 实时（盘中会抖，仅显示）
        if (ps[1]) state[symbol].ts_c = tsFromPeriod(ps[1]);  // 确认（策略用）
      }
    });
    ssStudy.onUpdate(() => {
      if (entries[symbol]?.chart !== chart) return;
      // smart_stop 存独立字段（ss/ss_c），推送时才合并进 ts/ts_c——
      // 直接 Object.assign 进 ts 会被 tsStudy 的下一次整对象覆盖（竞态丢值）
      const ps = ssStudy.periods;
      if (ps && ps.length) {
        state[symbol].ss = ssFromPeriod(ps[0]);
        if (ps[1]) state[symbol].ss_c = ssFromPeriod(ps[1]);
      }
    });
    fvgStudy.onUpdate(() => {
      if (entries[symbol]?.chart !== chart) return;
      const ps = fvgStudy.periods;
      if (ps && ps.length) {
        state[symbol].fvg = fvgFromPeriod(ps[0], ps, 0);      // 实时（仅显示）
        if (ps[1]) state[symbol].fvg_c = fvgFromPeriod(ps[1], ps, 1);  // 确认（策略/选股用）
      }
    });
    for (const { model, study } of modelStudies) study.onUpdate(() => {
      if (entries[symbol]?.chart !== chart) return;
      const ps = study.periods;
      if (ps && ps.length) {
        state[symbol][model.realtime_key] = outputFromPeriod(model, ps[0]);
        if (ps[1]) state[symbol][model.confirmed_key] = outputFromPeriod(model, ps[1]);
        if (model.id === 'ema9_pyramid') {
          state[symbol].breakoutModelPeriods = new Map(ps.slice(0, 175).map(p => [
            p.$time, outputFromPeriod(model, p),
          ]));
          state[symbol].ema7_closed = ps.slice(1, 79).map(p => ({
            time: p.$time, ema: finite(p.EMA_9),
          }));
        }
      }
    });
    if (attackStudy) attackStudy.onUpdate(() => {
      if (entries[symbol]?.chart !== chart) return;
      // 进攻区域存独立字段（attack/attack_c），推送时直接下发——
      // 同 ss 的教训：不 Object.assign 进别的通道，防整对象覆盖竞态
      const ps = attackStudy.periods;
      if (ps && ps.length) {
        state[symbol].breakoutRanges = new Map(ps.slice(1, 161).map(p => [
          p.$time, [finite(p.opening3High ?? p.K), finite(p.opening3Low ?? p.K_2)],
        ]));
        state[symbol].attack = attackFromPeriod(ps[0]);       // 实时（仅显示）
        if (ps[1]) state[symbol].attack_c = attackFromPeriod(ps[1]);  // 确认（盘前区间注入用）
      }
    });
    chart.onError((...e) => console.error(`[${symbol}/5m] err:`,
      JSON.stringify(e).slice(0, 150)));
  }

  async function rebuildChart(symbol, why) {
    const e = entries[symbol];
    if (!e || e.gaveUp) return;
    if (e.rebuilding && Date.now() - e.createdAt <= READY_GRACE_MS) return;
    e.rebuilding = true;
    const n = e.rebuilds + 1;
    try { e.chart.delete(); } catch (_) { /* 旧 session 可能已死，忽略 */ }
    if (n > MAX_REBUILDS) {
      e.gaveUp = true;
      e.rebuilding = false;
      console.error(`[${symbol}] 重建已达 ${MAX_REBUILDS} 次仍不就绪（${why}），放弃自动重建`);
      return;
    }
    console.error(`[${symbol}] 重建 chart（${why}，第 ${n}/${MAX_REBUILDS} 次）`);
    spawnChart(symbol, n);
    // spawnChart 会替换 entries[symbol]（rebuilding 被重置），重新标记，
    // 直到新 chart 首批数据到达（chart.onUpdate 里清除）
    const ne = entries[symbol];
    if (ne) ne.rebuilding = true;
  }

  // 分批发起订阅：一次性并发建 51 chart 会被 TV 限流（实测卡在部分标的
  // 且不自愈），每批 BATCH_SIZE 个、批间 sleep BATCH_DELAY_MS
  for (const symbol of args.symbols) {
    state[symbol] = { bar: null, bar_c: null, bar_c2: null, ts: {}, ts_c: {}, fvg: {}, fvg_c: {}, ss: {}, ss_c: {}, attack: {}, attack_c: {}, ema_9: null, ema_9_c: null, e9: {}, e9_c: {} };
  }
  // 每秒输出一次最新快照
  setInterval(() => {
    const symbols = {};
    for (const [sym, s] of Object.entries(state)) {
      // smart_stop（ss/ss_c 独立通道）推送时合并进 ts/ts_c，消费方无感知
      if (s.bar) symbols[sym] = {
        opening_breakout_history: (s.breakoutBars || []).flatMap((row, index) => {
          const time = row[0];
          const range = s.breakoutRanges?.get(time);
          if (!range || !s.breakoutAtr?.has(time)) return [];
          return [[...row.slice(0, 6), s.breakoutAtr.get(time), ...range, null, row[6], row[7],
            historicalBreakoutContext(s.breakoutChartPeriods || [], index + 1, s.breakoutModelPeriods)]];
        }),
        bar: { ...s.bar, ...breakoutContext(s.breakoutChartPeriods || [], 0, s.breakoutModelPeriods) },
        bar_c: s.bar_c ? { ...s.bar_c, ...breakoutContext(s.breakoutChartPeriods || [], 1, s.breakoutModelPeriods) } : null,
        bar_c2: s.bar_c2, ts: { ...s.ts, ...s.ss, ema_9: s.ema_9 }, ts_c: { ...s.ts_c, ...s.ss_c, ema_9: s.ema_9_c }, fvg: s.fvg, fvg_c: s.fvg_c, attack: s.attack, attack_c: s.attack_c, ema7_closed: (s.ema7_closed || []).map(row => ({ ...row, close: finite(s.closedPrices?.get(row.time)) })), ...Object.fromEntries(PINE_MODELS.flatMap(model => [
        [model.realtime_key, s[model.realtime_key] || {}],
        [model.confirmed_key, s[model.confirmed_key] || {}],
      ])) };
    }
    if (Object.keys(symbols).length > 0) {
      process.stdout.write(JSON.stringify({
        type: 'tick', ts: Date.now() / 1000, symbols,
      }) + '\n');
    }
  }, PUSH_INTERVAL_MS);

  for (let i = 0; i < args.symbols.length; i += BATCH_SIZE) {
    const batch = args.symbols.slice(i, i + BATCH_SIZE);
    for (const symbol of batch) spawnChart(symbol);
    const subscribed = Math.min(i + BATCH_SIZE, args.symbols.length);
    process.stdout.write(JSON.stringify({ type: 'startup_progress', subscribed }) + '\n');
    console.error(`[batch] 已发起 ${subscribed}/${args.symbols.length} 个 chart`);
    if (i + BATCH_SIZE < args.symbols.length) await sleep(BATCH_DELAY_MS);
  }

  // 核心 K 线或确认趋势指标缺失时重建 chart。attack 缺值仅聚合告警，不能因可选
  // 区间数据反复销毁同 session 的 K 线、trend、FVG 和 smart_stop。
  let sweeping = false;   // 单飞：一轮重建 54 chart×1.5s 远超巡检间隔，
                          // 不拦会叠扫→同一 chart 连环重建（实测 40ms 内 3 次）
  let lastAttackWarn = 0;
  setInterval(() => {
    if (sweeping) return;
    sweeping = true;
    const now = Date.now();
    const dead = [];
    const attackMissing = [];
    for (const [sym, e] of Object.entries(entries)) {
      if (e.gaveUp) continue;
      const age = now - e.createdAt;
      const hasBars = !!(e.chart.periods && e.chart.periods.length);
      const attack = e.studies.attack;
      const aps = attack ? attack.periods : null;
      const hasAttack = !!(aps && aps.length && finite(aps[0].premarketHigh != null ? aps[0].premarketHigh : aps[0].plot_1) !== null);
      const why = chartRetryReason(e, e.chart.periods,
        finite(e.studies.trend?.periods?.[1]?.Trend_State), now, READY_GRACE_MS);
      if (why) dead.push([sym, why]);
      else if (attackIndic && age > ATTACK_GRACE_MS && hasBars && !hasAttack) attackMissing.push(sym);
    }
    if (attackMissing.length && now - lastAttackWarn >= 300000) {
      console.error(`[attack] ${attackMissing.length}/${args.symbols.length} 个标的无盘前高值；保持核心行情，不重建 chart`);
      lastAttackWarn = now;
    }
    (async () => {
      try {
        for (const [sym, why] of dead) {
          await rebuildChart(sym, why);
          await sleep(REBUILD_STAGGER_MS);
        }
      } finally {
        sweeping = false;
      }
    })();
  }, INSPECT_INTERVAL_MS);

  // 心跳
  setInterval(() => {
    process.stdout.write(JSON.stringify({ type: 'heartbeat', ts: Date.now() / 1000 }) + '\n');
  }, HEARTBEAT_MS);

  console.error(`daemon 订阅就绪: ${args.symbols.length} 个标的 (5M K线 + trend_state + FVG Finder+ + smart_stop；attack=${attackIndic ? '启用' : '降级'}；分批 ${BATCH_SIZE}×${BATCH_DELAY_MS}ms，巡检自愈已开启)`);
}

main().catch((e) => {
  console.error('FATAL:', e.message || e);
  process.exit(1);
});
