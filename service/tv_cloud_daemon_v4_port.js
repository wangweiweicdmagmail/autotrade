/**
 * TradingView 实时订阅 daemon (tv_cloud_daemon.js) — v4 移植版
 * ================================================
 *
 * 常驻订阅模式：保持一条 TradingView WS 连接，每标的 1 个 5M chart
 * session，挂 4 个 Study（trend_state + FVG Finder+ + smart_stop +
 * 5M 进攻区域），按 1s 节流把最新快照以 JSON 行输出到 stdout，供 Python 采集器（subprocess）消费。
 * smart_stop 的值（Smart_Stop_Long/Short）归一化后合入 ts/ts_c 的
 * smart_stop_long / smart_stop_short 字段。
 *
 * 用法：
 *   node tv_cloud_daemon.js --symbols BATS:SKHY,BATS:NVDA
 *   node tv_cloud_daemon.js --symbols ... --batch-size 6 --batch-delay-ms 3000
 *
 * 2026-10-27 移植到 @mathieuc/tradingview v4（ESM，CJS require 可用）：
 *   TradingView 服务端当天起不再返回 result.ilTemplate（编译后脚本移到
 *   metaInfo.inputs 里 id="text" 的 defval，见上游 issue #354），v3 全部
 *   Study 报 "Can't parse pine"。本文件自带 translate 抓取 + 兼容提取，
 *   再调库里的 parseIndicatorDefinition 构造 PineIndicator。
 *   API 映射：Client→TradingViewClient、chart.Study→createStudy、
 *   periods（倒序）→candles/values（正序，最新在末尾）、bar 字段 max/min→high/low。
 *
 * 启动与自愈（2026-10-24 修复订阅风暴）：
 *   - 分批发起 chart 订阅（默认每批 6 个、批间 3s，--batch-size /
 *     --batch-delay-ms 可调）：一次性并发建几十个 chart 会被 TV 服务端
 *     限流，实测卡在部分标的（无 K线 / attack study 无值）且不自愈。
 *   - 巡检自愈（每 30s）：chart 创建超 90s 无 bar、或 attack study
 *     超 90s 无 pmh 值 → chart.delete() 后销毁重建（逐个间隔 1.5s，
 *     单 chart 上限 10 次防 thrash），重建计数与原因写 stderr。
 *   - 任一 Study 报 error（如 Can't parse pine）→ 立即重建该 chart。
 *   - WS 连接关闭/致命错误 → 进程退出，由 Python 采集器负责重启。
 *
 * 输出（stdout 每行一个 JSON）：
 *   {"type":"tick","ts":...,"symbols":{
 *     "BATS:SKHY":{
 *       "bar":  {time,open,high,low,close,volume},   // 实时 5M K线（未收盘）
 *       "bar_c": {...},                              // 上一根已收盘（稳定）
 *       "bar_c2": {...},                             // 上上根已收盘（periods[2]，回踩单定价基准）
 *       "ts":  {trend_state,direction,ema_fast,ema_slow,supertrend,
 *               channel_edge,atr,st_duration_bars,st_push_count}, // 实时值（最新 bar）
 *       "ts_c": {...同结构},                         // 确认值（上一根已收盘，策略/下单用）
 *       "fvg":  {fvg_cum_count,fvg_last_top,fvg_last_bottom,
 *                fvg_event,fvg_last_dir},            // FVG 实时值
 *       "fvg_c": {...同结构}                         // FVG 确认值（策略/选股用）
 *       "attack":  {time,bar_color,pmh,pml},         // 进攻区域实时值
 *       "attack_c": {...同结构}                      // 进攻区域确认值（盘前区间注入用）
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
 *   fvg_last_dir：最后 FVG 方向，daemon 自 values 历史向前扫描最后一个
 *   非零 fvg_event 推导（上下沿只在新 FVG 形成时更新，两者天然对应）；
 *   200 根窗口内无 FVG 事件则为 null。保存版另有 plot_4/5（_FVG_/_FVG__2），忽略。
 *
 * 5M 进攻区域指标（USER;8ab4f534e5874b47b24a5ce5c49acace，
 * 「5M 进攻区域- 方向过滤器,双区间」）：
 *   Bar_Color      0/1（na 时为 1e100，finite() 转 null）
 *   premarketHigh  当天盘前高点（指标 TV 侧自算 04:00–09:25 ET 口径，
 *                  实测与 research.db 盘前区间精确一致，权威来源）
 *   premarketLow   当天盘前低点
 */

'use strict';

const fs = require('fs');
const path = require('path');
const TradingView = require('@mathieuc/tradingview');

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
const ATTACK_GRACE_MS = 90000;        // attack study 超过 90s 无 pmh 值 → 重建
const REBUILD_STAGGER_MS = 1500;      // 重建逐个间隔（别一次性全砸）
const MAX_REBUILDS = 10;              // 单 chart 重建上限（防无限 thrash）

function finite(v) {
  return (v !== undefined && v !== null && v < 1e99) ? v : null;
}

// ---------------------------------------------------------------------------
// pine-facade translate 抓取（2026-10-27 起 ilTemplate 移到 text 输入 defval，
// 见 TradingView-API issue #354；库内 getIndicator 尚未修，这里自带兼容提取）
// ---------------------------------------------------------------------------

function scriptFromTranslate(result) {
  if (result.ilTemplate) return result.ilTemplate;
  const inputs = (result.metaInfo && result.metaInfo.inputs) || [];
  const text = inputs.find((i) => i.id === 'text');
  if (text && typeof text.defval === 'string' && text.defval) return text.defval;
  throw new Error('no compiled script: neither ilTemplate nor text input defval');
}

async function loadIndicator(id, credentials) {
  const url = `https://pine-facade.tradingview.com/pine-facade/translate/${id.replace(/ |%/g, '%25')}/last`;
  let lastErr = null;
  for (let attempt = 0; attempt < 5; attempt++) {
    try {
      const resp = await fetch(url, {
        headers: { cookie: `sessionid=${credentials.session};sessionid_sign=${credentials.signature}` },
      });
      const data = await resp.json();
      if (!data.success || !data.result || !data.result.metaInfo || !data.result.metaInfo.inputs) {
        throw new Error(`translate 失败: ${data.reason || resp.status}`);
      }
      const result = { ...data.result, ilTemplate: scriptFromTranslate(data.result) };
      return TradingView.parseIndicatorDefinition(result, id, 'last');
    } catch (e) {
      lastErr = e;
      // 限流/网络抖动：退避重试（1s, 2s, 4s, 8s）
      await new Promise((r) => setTimeout(r, 1000 * (2 ** attempt)));
    }
  }
  throw lastErr;
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
  // 的标题变成中文/空标题，v4 键名规则：空标题回退 plot_N、剥非 ASCII 后
  // 空串加后缀 _2（实测：plot_1=盘前高，_2=盘前低）——三套键名都兼容。
  // K/K_2 = 第二区间（双区间之开盘区间，暂不使用）。
  const pmhRaw = p.premarketHigh != null ? p.premarketHigh : p.plot_1;
  const pmlRaw = p.premarketLow != null ? p.premarketLow : p._2;
  return {
    time: p.$time,   // 该值所属 bar 的时间戳：消费方用它校验 attack_c 与 bar_c 对齐
    bar_color: finite(p.Bar_Color),
    pmh: finite(pmhRaw),
    pml: finite(pmlRaw),
  };
}

// 把一根 bar 的 FVG Finder+ plot 值归一化为标准结构。
// 键名是中文 plot 标题被剥离非 ASCII 后的实测结果（见文件头映射表）。
// fvg_last_dir：上下沿只在新 FVG 形成时更新，方向 = 自 idx 向历史（更老）
// 扫描最后一个非零 fvg_event；扫不到（窗口内无 FVG）为 null。
// 注意 v4 values 是正序（最老在前），扫描方向与 v3 periods 相反。
function fvgFromPeriod(p, values, idx) {
  if (!p) return {};
  let lastDir = null;
  if (values) {
    for (let i = idx; i >= 0; i--) {
      const ev = finite(values[i] && values[i].KFVG110);
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

async function main() {
  const args = _args;
  const creds = JSON.parse(fs.readFileSync(args.creds, 'utf8'));
  const credentials = { session: creds.sessionid, signature: creds.sessionid_sign };

  // 输入全部用指标默认值，与图表上的指标所见即所得，不做 setInput 覆盖
  const trendStateIndic = await loadIndicator(TREND_STATE_ID, credentials);
  const fvgIndic = await loadIndicator(FVG_ID, credentials);
  const ssIndic = await loadIndicator(SMART_STOP_ID, credentials);
  const attackIndic = await loadIndicator(ATTACK_ZONE_ID, credentials);

  const client = new TradingView.TradingViewClient({ credentials });
  client.on('error', (e) => console.error('[client] err:', e.code || '', e.message || e));
  // 连接关闭（掉线/被踢）→ 退出进程，Python 采集器会自动重启 daemon
  client.on('close', (code, reason) => {
    console.error(`[client] WS 关闭（code=${code} ${reason || ''}），进程退出待重启`);
    process.exit(1);
  });
  await client.ready;

  const state = {};
  const entries = {};   // symbol -> {chart, studies, createdAt, rebuilds, rebuilding, gaveUp}

  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

  // 单 chart 创建（初始分批与巡检重建共用）：4 个 Study 同 chart 挂载，
  // state[symbol] 对象跨重建复用（推送循环只认 state，无感）
  function spawnChart(symbol, rebuilds = 0) {
    const chart = client.createChart();
    chart.setMarket(symbol, { timeframe: '5', count: 200, session: 'extended' });
    const tsStudy = chart.createStudy(trendStateIndic);
    const fvgStudy = chart.createStudy(fvgIndic);
    const ssStudy = chart.createStudy(ssIndic);
    const attackStudy = chart.createStudy(attackIndic);
    entries[symbol] = {
      chart, studies: { attack: attackStudy },
      createdAt: Date.now(), rebuilds, rebuilding: false, gaveUp: false,
    };

    chart.on('update', () => {
      const ent = entries[symbol];
      if (ent) ent.rebuilding = false;   // 首批数据到达 = 重建完成
      const cs = chart.candles;          // v4：正序，[len-1]=最新（实时，未收盘）
      if (cs && cs.length) {
        const b = cs[cs.length - 1];
        state[symbol].bar = {
          time: b.time, open: b.open, high: b.high,
          low: b.low, close: b.close, volume: b.volume,
        };
        const bc = cs[cs.length - 2];    // 上一根已收盘（稳定）
        if (bc) {
          state[symbol].bar_c = {
            time: bc.time, open: bc.open, high: bc.high,
            low: bc.low, close: bc.close, volume: bc.volume,
          };
        }
        const b2 = cs[cs.length - 3];    // 上上根已收盘（回踩单定价基准，随新 bar 滚动）
        if (b2) {
          state[symbol].bar_c2 = {
            time: b2.time, open: b2.open, high: b2.high,
            low: b2.low, close: b2.close, volume: b2.volume,
          };
        }
      }
    });
    tsStudy.on('update', () => {
      const vs = tsStudy.values;         // v4：正序
      if (vs && vs.length) {
        state[symbol].ts = tsFromPeriod(vs[vs.length - 1]);       // 实时（盘中会抖，仅显示）
        if (vs.length > 1) state[symbol].ts_c = tsFromPeriod(vs[vs.length - 2]);  // 确认（策略用）
      }
    });
    ssStudy.on('update', () => {
      // smart_stop 存独立字段（ss/ss_c），推送时才合并进 ts/ts_c——
      // 直接 Object.assign 进 ts 会被 tsStudy 的下一次整对象覆盖（竞态丢值）
      const vs = ssStudy.values;
      if (vs && vs.length) {
        state[symbol].ss = ssFromPeriod(vs[vs.length - 1]);
        if (vs.length > 1) state[symbol].ss_c = ssFromPeriod(vs[vs.length - 2]);
      }
    });
    fvgStudy.on('update', () => {
      const vs = fvgStudy.values;
      if (vs && vs.length) {
        state[symbol].fvg = fvgFromPeriod(vs[vs.length - 1], vs, vs.length - 1);      // 实时（仅显示）
        if (vs.length > 1) state[symbol].fvg_c = fvgFromPeriod(vs[vs.length - 2], vs, vs.length - 2);  // 确认（策略/选股用）
      }
    });
    attackStudy.on('update', () => {
      // 进攻区域存独立字段（attack/attack_c），推送时直接下发——
      // 同 ss 的教训：不 Object.assign 进别的通道，防整对象覆盖竞态
      const vs = attackStudy.values;
      if (vs && vs.length) {
        state[symbol].attack = attackFromPeriod(vs[vs.length - 1]);       // 实时（仅显示）
        if (vs.length > 1) state[symbol].attack_c = attackFromPeriod(vs[vs.length - 2]);  // 确认（盘前区间注入用）
      }
    });
    chart.on('error', (e) => console.error(`[${symbol}/5m] err:`, e.code || '', e.message || e));
    // 任一 Study 报错（如服务端 Can't parse pine）→ 整个 chart 重建
    const onStudyError = (name) => (e) => {
      console.error(`[${symbol}] study ${name} 报错:`, e.code || '', e.message || e);
      rebuildChart(symbol, `study ${name} 报错`);
    };
    tsStudy.on('error', onStudyError('trend_state'));
    fvgStudy.on('error', onStudyError('fvg'));
    ssStudy.on('error', onStudyError('smart_stop'));
    attackStudy.on('error', onStudyError('attack'));
  }

  async function rebuildChart(symbol, why) {
    const e = entries[symbol];
    if (!e || e.rebuilding || e.gaveUp) return;
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
    // 直到新 chart 首批数据到达（chart.on('update') 里清除）
    const ne = entries[symbol];
    if (ne) ne.rebuilding = true;
  }

  // 分批发起订阅：一次性并发建 51 chart 会被 TV 限流（实测卡在部分标的
  // 且不自愈），每批 BATCH_SIZE 个、批间 sleep BATCH_DELAY_MS
  for (const symbol of args.symbols) {
    state[symbol] = { bar: null, bar_c: null, bar_c2: null, ts: {}, ts_c: {}, fvg: {}, fvg_c: {}, ss: {}, ss_c: {}, attack: {}, attack_c: {} };
  }
  for (let i = 0; i < args.symbols.length; i += BATCH_SIZE) {
    const batch = args.symbols.slice(i, i + BATCH_SIZE);
    for (const symbol of batch) spawnChart(symbol);
    console.error(`[batch] 已发起 ${Math.min(i + BATCH_SIZE, args.symbols.length)}/${args.symbols.length} 个 chart`);
    if (i + BATCH_SIZE < args.symbols.length) await sleep(BATCH_DELAY_MS);
  }

  // 巡检自愈：chart 创建超过 READY_GRACE_MS 仍无 bar 数据，或 attack study
  // 超过 ATTACK_GRACE_MS 无 pmh 值 → 销毁重建（逐个间隔发起，别一次性全砸）
  let sweeping = false;   // 单飞：一轮重建 54 chart×1.5s 远超巡检间隔，
                          // 不拦会叠扫→同一 chart 连环重建（实测 40ms 内 3 次）
  setInterval(() => {
    if (sweeping) return;
    sweeping = true;
    const now = Date.now();
    const dead = [];
    for (const [sym, e] of Object.entries(entries)) {
      if (e.rebuilding || e.gaveUp) continue;
      const age = now - e.createdAt;
      const hasBars = !!(e.chart.candles && e.chart.candles.length);
      const avs = e.studies.attack.values;
      const latestAttack = avs && avs.length ? avs[avs.length - 1] : null;
      const pmhRaw = latestAttack && (latestAttack.premarketHigh != null ? latestAttack.premarketHigh : latestAttack.plot_1);
      const hasAttack = !!(latestAttack && finite(pmhRaw) !== null);
      if (age > READY_GRACE_MS && !hasBars) dead.push([sym, '无K线数据']);
      else if (age > ATTACK_GRACE_MS && hasBars && !hasAttack) dead.push([sym, 'attack无值']);
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

  // 每秒输出一次最新快照
  setInterval(() => {
    const symbols = {};
    for (const [sym, s] of Object.entries(state)) {
      // smart_stop（ss/ss_c 独立通道）推送时合并进 ts/ts_c，消费方无感知
      if (s.bar) symbols[sym] = { bar: s.bar, bar_c: s.bar_c, bar_c2: s.bar_c2, ts: { ...s.ts, ...s.ss }, ts_c: { ...s.ts_c, ...s.ss_c }, fvg: s.fvg, fvg_c: s.fvg_c, attack: s.attack, attack_c: s.attack_c };
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

  console.error(`daemon 订阅就绪: ${args.symbols.length} 个标的 (5M K线 + trend_state + FVG Finder+ + smart_stop + 进攻区域；分批 ${BATCH_SIZE}×${BATCH_DELAY_MS}ms，巡检自愈已开启；tradingview lib v4)`);
}

main().catch((e) => {
  console.error('FATAL:', e.message || e);
  process.exit(1);
});
