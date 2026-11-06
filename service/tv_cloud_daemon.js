/**
 * 生产路径从 TradingView 分批订阅 5 分钟 K 线和日线，在 PineTS worker
 * 中计算七个指标。stdout 每秒发布原有 Tick JSON。TV Study 只用于显式的
 * --ema7-shadow-only 和 --benchmark-study 诊断入口。
 */
'use strict';

const fs = require('fs');
const path = require('path');
const TradingView = require('@mathieuc/tradingview');
const { loadIndicatorWithText } = require('./pine_text.js');
const {createPriceUpdater, isPriceUpdate, chartSession, previousRegularRange} = require('./chart_snapshot.js');
const { chartRetryReason, studyRetryReason, retryDelay } = require('./chart_health.js');
const { breakoutContext, historicalBreakoutContext } = require('./candle_metrics.js');
const { loadPineModels, outputFromPeriod, periodFromOutput } = require('./pine_models.js');
const { BAR_LIMIT, createLocalIndicators, candle } = require('./pinets_live');
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
const STUDY_GRACE_MS = 45000;        // 持续无可对齐输出才重试，容忍换根短暂延迟
const STUDY_PUMP_MS = 250;
const STUDY_BATCH_SIZE = 3;
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
    fvg_last_dir: p.Last_Direction ?? lastDir,
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

  const benchmarkStudy = args['benchmark-study'];
  const shadowLog = args['ema7-shadow-log'] || process.env.AUTOTRADE_EMA7_SHADOW_LOG;
  const shadowOnly = args['ema7-shadow-only'] === '1';
  const localMode = !shadowOnly && !benchmarkStudy;
  const marketSession = args['market-session'] || (localMode ? '24h' : 'extended');
  if (!['24h', 'extended'].includes(marketSession)) throw new Error('不支持的 5 分钟行情时段');
  const defaultSessionSymbols = new Set((args['default-session-symbols'] || '').split(',').filter(Boolean));
  if (shadowOnly && (!args['ema7-shadow-log'] || benchmarkStudy)) {
    throw new Error('独立 EMA7 影子模式需要 --ema7-shadow-log，并且不使用七合一指标');
  }
  const requestedShadowSymbols = (args['ema7-shadow-symbols'] ||
    process.env.AUTOTRADE_EMA7_SHADOW_SYMBOLS || 'NASDAQ:MSFT,NASDAQ:NVDA,NASDAQ:QQQ').split(',');
  const shadowSymbols = new Set(shadowOnly ? args.symbols : args.symbols.filter(s => requestedShadowSymbols.includes(s)));
  let shadow;
  if (!localMode && shadowLog && shadowSymbols.size) {
    const { createEma7Shadow } = require('./pinets_shadow');
    const log = fs.createWriteStream(shadowLog, { flags: 'a', mode: 0o600 });
    let blocked = false;
    let dropped = 0;
    log.on('drain', () => { blocked = false; });
    log.on('error', error => {
      console.error('[ema7-shadow] 记录已停止:', error.message);
      void shadow.close();
    });
    shadow = createEma7Shadow({ record(row) {
      if (blocked) { dropped++; return; }
      blocked = !log.write(JSON.stringify({ ...row, log_dropped: dropped }) + '\n');
      dropped = 0;
    } });
  }
  if (benchmarkStudy && !/^USER;[a-f0-9]+$/i.test(benchmarkStudy)) {
    throw new Error('--benchmark-study requires a saved private Pine ID');
  }
  let loaded = 0;
  const total = benchmarkStudy || shadowOnly ? 1 : PINE_MODELS.length + 4;
  const report = (event) => process.stdout.write(JSON.stringify({
    type: 'indicator_startup', loaded, total, ...event,
  }) + '\n');
  const localErrors = new Map();
  const local = localMode ? createLocalIndicators({
    onReady(key, count) {
      loaded = count;
      report({ name: key, source: 'pinets', tv_study_subscriptions: 0 });
    },
    onError(symbol, key, error) {
      const id = symbol + '/' + key;
      const now = Date.now();
      if (now - (localErrors.get(id) || 0) >= 30000) {
        localErrors.set(id, now);
        console.error(`[${id}] local_indicator_error: ${error}`);
      }
    },
  }) : null;
  async function loadStartupIndicator(id, name) {
    report({ name });
    try {
      const indicator = await loadIndicatorWithText(TradingView, id, creds, { preferVersionedCache: true });
      loaded++;
      report({ loaded_id: id, name });
      return indicator;
    } catch (error) {
      if (error.cacheIssue) report({ cache_issue: { id, name, ...error.cacheIssue } });
      throw error;
    }
  }

  const client = new TradingView.Client({
    token: creds.sessionid,
    signature: creds.sessionid_sign,
  });

  client.onConnected(() => console.error('[connection] TradingView websocket connected'));
  client.onDisconnected(() => {
    console.error('[connection] TradingView websocket disconnected; exit for supervised reconnect');
    setTimeout(() => process.exit(2), 50);
  });
  client.onError((...errors) => console.error('[connection] TradingView error:',
    errors.map(String).join(' ').slice(0, 300)));

  const state = {};
  const entries = {};   // symbol -> {chart, studies, createdAt, rebuilds, rebuilding, gaveUp}

  const definitions = [
    { id: TREND_STATE_ID, name: '趋势状态', key: 'ts', confirmed: 'ts_c',
      read: tsFromPeriod, extra: ['breakoutAtr'],
      history: (s, ps) => { s.breakoutAtr = new Map(ps.slice(1, 161).map(p => [p.$time, finite(p.Atr_Value)])); } },
    { id: FVG_ID, name: 'FVG Finder+', key: 'fvg', confirmed: 'fvg_c', read: fvgFromPeriod },
    { id: SMART_STOP_ID, name: '智能止损', key: 'ss', confirmed: 'ss_c', read: ssFromPeriod },
    ...PINE_MODELS.map(model => ({
      id: model.indicator_id, name: model.name || model.id, model,
      key: model.realtime_key, confirmed: model.confirmed_key,
      read: p => outputFromPeriod(model, p),
      ...(model.id !== 'ema9_pyramid' ? {} : {
        extra: ['breakoutModelPeriods', 'ema7_closed'],
        history: (s, ps) => {
          s.breakoutModelPeriods = new Map(ps.slice(0, 175).filter(Boolean)
            .map(p => [p.$time, outputFromPeriod(model, p)]));
          s.ema7_closed = ps.slice(1, 79).map(p => ({ time: p.$time, ema: finite(p.EMA_9) }));
        },
      }),
    })),
    { id: ATTACK_ZONE_ID, name: '5M 进攻区域', key: 'attack', confirmed: 'attack_c',
      read: attackFromPeriod, extra: ['breakoutRanges'],
      history: (s, ps) => {
        s.breakoutRanges = new Map(ps.slice(1, 161).map(p =>
          [p.$time, [finite(p.opening3High ?? p.K), finite(p.opening3Low ?? p.K_2)]]));
      } },
  ];

  const subscriptions = localMode ? [] : shadowOnly ? definitions.filter(item => item.key === 'e7v2') : benchmarkStudy
    ? [{ id: benchmarkStudy, key: 'combined', name: '七合一验证', outputs: definitions }]
    : definitions;

  function clearStudy(snapshot, definition) {
    snapshot[definition.key] = {};
    snapshot[definition.confirmed] = {};
    for (const key of definition.extra || []) delete snapshot[key];
  }

  function updateStudy(snapshot, definition, periods) {
    snapshot[definition.key] = definition.read(periods[0], periods, 0);
    snapshot[definition.confirmed] = periods[1] ? definition.read(periods[1], periods, 1) : {};
    definition.history?.(snapshot, periods);
  }

  function localSnapshot(symbol, raw) {
    const { base, outputs, observedAt } = local.snapshot(symbol, raw);
    const snapshot = { ...base, price_observed_at: observedAt == null ? undefined : observedAt / 1000 };
    for (const definition of definitions) {
      const key = { ts: 'trend', ss: 'smart' }[definition.key] || definition.key;
      const rows = outputs[key];
      if (rows?.length) {
        const periods = rows.map(row => definition.model ? periodFromOutput(definition.model, row) :
          row && { ...row, $time: row.time });
        updateStudy(snapshot, definition, periods);
      }
    }
    return snapshot;
  }

  function attachStudy(symbol, entry, definition, slot) {
    const snapshot = state[symbol];
    const current = () => entries[symbol] === entry && !entry.gaveUp;
    const fail = (reason) => {
      if (!current()) return;
      const study = slot.study;
      slot.study = null;
      slot.lagSince = null;
      slot.outputTime = null;
      slot.nextAttempt = Date.now() + retryDelay(++slot.failures);
      for (const output of definition.outputs || [definition]) clearStudy(snapshot, output);
      if (study) {
        try { study.remove(); } catch (_) { /* socket may already be closed */ }
      }
      console.error(`[${symbol}/${definition.key}] study_error: ${reason}; 单独重试`);
    };
    slot.fail = fail;
    try {
      const study = new entry.chart.Study(definition.indicator);
      slot.study = study;
      slot.lagSince = Date.now();
      study.onError((...error) => {
        if (current() && slot.study === study) fail(error.map(String).join(' ').slice(0, 220));
      });
      study.onUpdate(() => {
        if (!current() || slot.study !== study) return;
        const periods = study.periods;
        if (!periods?.length) return;
        slot.outputTime = periods[0].$time;
        if (definition.outputs) {
          const rows = Object.fromEntries(definition.outputs.map(output => [output.key, []]));
          for (const period of periods) {
            const values = Object.fromEntries(definition.outputs.map(output => [output.key, { $time: period.$time }]));
            for (const [plot, value] of Object.entries(period)) {
              const separator = plot.indexOf('_');
              const output = values[plot.slice(0, separator)];
              if (separator > 0 && output) output[plot.slice(separator + 1)] = value;
            }
            for (const output of definition.outputs) rows[output.key].push(values[output.key]);
          }
          for (const output of definition.outputs) updateStudy(snapshot, output, rows[output.key]);
        } else {
          updateStudy(snapshot, definition, periods);
        }
        if (shadow && shadowSymbols.has(symbol) && (definition.key === 'e7v2' || definition.outputs)) {
          shadow.observeStudy(symbol, snapshot.e7v2, snapshot.e7v2_c);
        }
      });
    } catch (error) {
      fail(error.message);
    }
  }

  // A definition's metadata retry and each symbol's Study retry are independent.
  // Limit Study registrations per tick to stay near the previous batch rate.
  const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
  let definitionsStarted = false;
  function startDefinitions() {
    if (definitionsStarted) return;
    definitionsStarted = true;
    subscriptions.forEach((definition, index) => {
      setTimeout(async () => {
        let failures = 0;
        while (!definition.indicator) {
          try {
            definition.indicator = await loadStartupIndicator(definition.id, definition.name);
          } catch (error) {
            console.error(`[pine:${definition.key}] 指标加载失败，单独重试: ${String(error.message).slice(0, 220)}`);
            await sleep(retryDelay(++failures));
          }
        }
      }, index * STUDY_PUMP_MS);
    });
  }

  function spawnChart(symbol, rebuilds = 0) {
    const chart = new client.Session.Chart();
    const requestedSession = defaultSessionSymbols.has(symbol) ? 'default' : marketSession;
    const snapshot = state[symbol] = {};
    for (const definition of definitions) clearStudy(snapshot, definition);
    const entry = entries[symbol] = {
      chart, requestedSession, studies: new Map(subscriptions.map(d => [d.key, {
        study: null, nextAttempt: 0, failures: 0, lagSince: null, outputTime: null,
      }])),
      createdAt: Date.now(), rebuilds, rebuilding: false, gaveUp: false,
    };
    const updatePrices = createPriceUpdater(closedAtrSma10);
    const shadowEnabled = shadow && shadowSymbols.has(symbol);
    if (shadowEnabled) shadow.reset(symbol);
    if (local) local.reset(symbol);
    const submitLocal = () => local?.observe(symbol, chart.periods, chart.infos, snapshot, entry.daily);
    chart.onUpdate((changes) => {
      if (entries[symbol] !== entry || entry.gaveUp || !isPriceUpdate(changes)) return;
      const periods = chart.periods;
      if (!periods.length) return;
      entry.sessionError = null;
      if (!chartSession(chart.infos, requestedSession).accepted) return;
      entry.rebuilding = false;
      updatePrices(snapshot, periods.slice(0, 200));
      if (local) local.observe(symbol, periods, chart.infos, snapshot, entry.daily);
      if (shadowEnabled) shadow.observePrice(symbol, periods, chart.infos);
    });
    chart.onError((...error) => {
      entry.sessionError = JSON.stringify(error).slice(0, 150);
      console.error(`[${symbol}/5m/${requestedSession}] err:`, entry.sessionError);
    });
    chart.setMarket(symbol, {
      timeframe: '5', range: local || shadowEnabled ? BAR_LIMIT : 200,
      session: requestedSession === 'default' ? undefined : requestedSession,
    });
    if (local) {
      // Daily bars are an input to the attack indicator and have their own
      // lifecycle. A daily subscription failure does not reset the 5m chart.
      const subscribeDaily = () => {
        if (entries[symbol] !== entry || entry.gaveUp) return;
        const dailyChart = entry.dailyChart = new client.Session.Chart();
        const retry = () => {
          if (entry.dailyChart !== dailyChart || entries[symbol] !== entry || entry.gaveUp) return;
          clearTimeout(entry.dailyTimer);
          entry.dailyChart = null;
          try { dailyChart.delete(); } catch (_) { /* socket already closed */ }
          entry.dailyTimer = setTimeout(subscribeDaily, retryDelay(++entry.dailyFailures));
        };
        dailyChart.onError(retry);
        dailyChart.onUpdate(changes => {
          if (entries[symbol] !== entry || entry.dailyChart !== dailyChart || !isPriceUpdate(changes)) return;
          const rows = dailyChart.periods.slice(0, 20).reverse().map(candle);
          if (rows.length < 2) return;
          clearTimeout(entry.dailyTimer);
          entry.dailyFailures = 0;
          entry.daily = rows;
          if (snapshot.bar && chartSession(chart.infos, requestedSession, entry.sessionError).accepted) submitLocal();
        });
        entry.dailyTimer = setTimeout(retry, 25000);
        dailyChart.setMarket(symbol, {
          timeframe: 'D', range: 20, session: requestedSession === 'default' ? undefined : 'regular',
        });
      };
      entry.dailyFailures = 0;
      entry.dailyTimer = setTimeout(subscribeDaily, 300 + args.symbols.indexOf(symbol) % BATCH_SIZE * 200);
    }
  }

  setInterval(() => {
    const now = Date.now();
    let capacity = STUDY_BATCH_SIZE;
    for (const [symbol, entry] of Object.entries(entries)) {
      const snapshot = state[symbol];
      if (entry.gaveUp || !snapshot.bar) continue;
      for (const definition of subscriptions) {
        const slot = entry.studies.get(definition.key);
        if (slot.study) {
          const reason = studyRetryReason(slot, snapshot.bar.time, slot.outputTime,
            now, STUDY_GRACE_MS);
          if (reason) slot.fail(reason);
        }
        if (!slot.study && definition.indicator && now >= slot.nextAttempt && capacity > 0) {
          capacity--;
          attachStudy(symbol, entry, definition, slot);
        }
      }
    }
  }, STUDY_PUMP_MS);

  async function rebuildChart(symbol, why) {
    const e = entries[symbol];
    if (!e || e.gaveUp) return;
    if (e.rebuilding && Date.now() - e.createdAt <= READY_GRACE_MS) return;
    e.rebuilding = true;
    const n = e.rebuilds + 1;
    try { e.chart.delete(); } catch (_) { /* 旧 session 可能已死，忽略 */ }
    clearTimeout(e.dailyTimer);
    try { e.dailyChart?.delete(); } catch (_) { /* socket already closed */ }
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

  // 每秒输出一次最新快照
  setInterval(() => {
    const symbols = {};
    const sessions = {};
    for (const [sym, raw] of Object.entries(state)) {
      const entry = entries[sym];
      const session = chartSession(entry.chart.infos, entry.requestedSession, entry.sessionError);
      sessions[sym] = { ...session, has_bars: !!raw.bar && session.accepted };
      if (!raw.bar || (entry.requestedSession !== 'extended' && !session.accepted)) continue;
      const s = local ? localSnapshot(sym, raw) : raw;
      // smart_stop（ss/ss_c 独立通道）推送时合并进 ts/ts_c，消费方无感知
      if (s.bar) symbols[sym] = {
        session,
        previous_regular_range: previousRegularRange(entry.daily, s.bar.time),
        opening_breakout_history: (s.breakoutBars || []).flatMap((row, index) => {
          const time = row[0];
          const range = s.breakoutRanges?.get(time);
          if (!range || !s.breakoutAtr?.has(time)) return [];
          return [[...row.slice(0, 6), s.breakoutAtr.get(time), ...range, null, row[6], row[7],
            historicalBreakoutContext(s.breakoutChartPeriods || [], index + 1, s.breakoutModelPeriods)]];
        }),
        bar: { ...s.bar, ...breakoutContext(s.breakoutChartPeriods || [], 0, s.breakoutModelPeriods) },
        bar_c: s.bar_c ? { ...s.bar_c, ...breakoutContext(s.breakoutChartPeriods || [], 1, s.breakoutModelPeriods) } : null,
        bar_c2: s.bar_c2, price_observed_at: s.price_observed_at,
        ts: { ...s.ts, ...s.ss, ema_9: s.ema_9 }, ts_c: { ...s.ts_c, ...s.ss_c, ema_9: s.ema_9_c }, fvg: s.fvg, fvg_c: s.fvg_c, attack: s.attack, attack_c: s.attack_c, ema7_closed: (s.ema7_closed || []).map(row => ({ ...row, close: finite(s.closedPrices?.get(row.time)) })), ...Object.fromEntries(PINE_MODELS.flatMap(model => [
        [model.realtime_key, s[model.realtime_key] || {}],
        [model.confirmed_key, s[model.confirmed_key] || {}],
      ])) };
    }
    if (Object.keys(symbols).length > 0 || marketSession === '24h') {
      process.stdout.write(JSON.stringify({
        type: 'tick', ts: Date.now() / 1000, symbols, sessions,
      }) + '\n');
    }
  }, PUSH_INTERVAL_MS);

  for (let i = 0; i < args.symbols.length; i += BATCH_SIZE) {
    const batch = args.symbols.slice(i, i + BATCH_SIZE);
    for (const symbol of batch) spawnChart(symbol);
    startDefinitions();
    const subscribed = Math.min(i + BATCH_SIZE, args.symbols.length);
    process.stdout.write(JSON.stringify({ type: 'startup_progress', subscribed }) + '\n');
    console.error(`[batch] 已发起 ${subscribed}/${args.symbols.length} 个 chart`);
    if (i + BATCH_SIZE < args.symbols.length) await sleep(BATCH_DELAY_MS);
  }

  // Only missing price data can cause a chart rebuild. Study faults stay on
  // the Study lifecycle and never discard an otherwise healthy price series.
  let sweeping = false;
  setInterval(() => {
    if (sweeping) return;
    sweeping = true;
    const now = Date.now();
    const dead = [];
    for (const [sym, entry] of Object.entries(entries)) {
      const session = chartSession(entry.chart.infos, entry.requestedSession, entry.sessionError);
      if (entry.requestedSession === '24h' && session.resolved && session.resolved !== '24h') continue;
      const bar = entry.requestedSession !== 'extended' && !session.accepted ? null : state[sym].bar;
      const why = chartRetryReason(entry, bar, now, READY_GRACE_MS);
      if (why) dead.push([sym, why]);
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

  const defaultCount = args.symbols.filter(symbol => defaultSessionSymbols.has(symbol)).length;
  console.error(`daemon K线订阅已发起: ${args.symbols.length} 个标的（5m ${marketSession}: ${args.symbols.length - defaultCount}，默认时段: ${defaultCount}）；${subscriptions.length} 个 TV 指标订阅；${local ? 'PineTS 本地计算' : 'TV 诊断模式'}`);
}

main().catch((e) => {
  console.error('FATAL:', e.message || e);
  process.exit(1);
});
