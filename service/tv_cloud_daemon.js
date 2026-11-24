/**
 * 生产路径从 TradingView 分批订阅同一时段的 5 分钟、1 分钟 K 线和日线，在 PineTS worker
 * 中计算指标。stdout 每秒发布原有 Tick JSON；不提供 TV 指标注册或订阅模式。
 */
'use strict';

const fs = require('fs');
const path = require('path');
const TradingView = require('@mathieuc/tradingview');
const { createMarketClient } = require('./tv_market_proxy');
const {createPriceUpdater, createCoalescedUpdater, isPriceUpdate, chartSession, previousRegularRange} = require('./chart_snapshot.js');
const { chartRetryReason, retryDelay } = require('./chart_health.js');
const { breakoutContext, historicalBreakoutContext } = require('./candle_metrics.js');
const {PINE_MODELS,definitions,clearOutputs,localSnapshot}=require('./local_snapshot');
const { BAR_LIMIT, createLocalIndicators, candle } = require('./pinets_live');
const { calculateVwap1m, candidateProtectionSnapshot, createVwapMinuteBatch, openingVwapSnapshot } = require('./vwap_1m');
const { BETA, REFERENCES, createMarketRhythm, minuteSubscriptionOptions } = require('./market_rhythm');
const { startIpoWatch } = require('./ipo_watch');
const {STOCK_SESSION,MINUTE_HISTORY}=require('./market_session');

const PUSH_INTERVAL_MS = 1000;      // 输出节流：每秒一次
const {createResearchJournal,digest:researchDigest}=require('./research_journal');
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
const REBUILD_STAGGER_MS = 1500;      // 重建逐个间隔（别一次性全砸）
const MAX_REBUILDS = 10;              // 单 chart 重建上限（防无限 thrash）

const finite = v => v !== undefined && v !== null && v < 1e99 ? v : null;

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
  if (Object.keys(args).some(key => key.startsWith('ema7-shadow') || key === 'benchmark-study'))
    throw new Error('TV 云指标对照入口已退役；当前仅支持 K 线订阅与 PineTS 本地计算');
  const creds = JSON.parse(fs.readFileSync(args.creds, 'utf8'));

  const marketSession = args['market-session'] || STOCK_SESSION;
  if (!['regular', '24h', 'extended'].includes(marketSession)) throw new Error('不支持的 5 分钟行情时段');
  const defaultSessionSymbols = new Set((args['default-session-symbols'] || '').split(',').filter(Boolean));
  const research=createResearchJournal(args['research-spool'],{onError:message=>console.error('[research-archive]',message)});
  const researchDailyKeys=new Map();
  if(args['research-spool']){
    try{
    const series=args.symbols.flatMap(symbol=>{
      const session=defaultSessionSymbols.has(symbol)?'default':marketSession;
      return [{symbol,interval:'5m',session},{symbol,interval:'1d',session:session==='default'?'default':'regular'},
        ...(!defaultSessionSymbols.has(symbol)||BETA.includes(symbol)?[{symbol,interval:'1m',session}]:[])];
    });
    research.append('configuration',{symbols:args.symbols,series,market_session:marketSession});
    const {getDefinition}=require('./pinets_runtime');
    research.append('definition',{models:[...definitions.map(d=>getDefinition(({ts:'trend',ss:'smart'})[d.key]||d.key)),getDefinition('opening_position')],
      registry:JSON.parse(fs.readFileSync(path.join(__dirname,'pine_strategies.json'),'utf8')),
      dependency_lock:fs.readFileSync(path.join(__dirname,'..','package-lock.json'),'utf8'),
      scoring_source:fs.readFileSync(path.join(__dirname,'rhythm_score.js'),'utf8'),
      perception_source:fs.readFileSync(path.join(__dirname,'market_rhythm.js'),'utf8')});
    }catch(error){console.error('[research-archive] 版本记录失败:',error.message);}
  }
  function archiveBars(symbol,interval,session,rows,infos){
    if(!args['research-spool'])return;
    try{
    research.observeBars({symbol,resolved_symbol:infos.full_name||infos.pro_name||symbol,interval,session,
      provider:'tradingview',adjustment:'splits',timezone:infos.timezone||'unknown',
      minmove:infos.minmov??infos.minmove??null,pricescale:infos.pricescale??null,type:infos.type||'unknown'},rows);
    }catch(error){console.error('[research-archive] K线记录失败:',error.message);}
  }
  const rhythm = args['rhythm-cache'] ? createMarketRhythm({
    symbols: args.symbols.filter(symbol => !defaultSessionSymbols.has(symbol) || BETA.includes(symbol)),
    cachePath: args['rhythm-cache'],marketSession,onFrame:frame=>research.ranking(frame),
    onError:({symbol,message})=>console.error(`[market-rhythm/${symbol}] calculation_error: ${message}`),
  }) : null;
  let rhythmErrorAt = 0;
  const vwapMinuteBatch = createVwapMinuteBatch(args.symbols.filter(symbol => !defaultSessionSymbols.has(symbol)));
  let loaded = 0;
  const total = definitions.filter(d => d.model?.interval_seconds !== 60).length;
  const report = (event) => process.stdout.write(JSON.stringify({
    type: 'indicator_startup', loaded, total, ...event,
  }) + '\n');
  const localErrors = new Map();
  const local = createLocalIndicators({
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
  });
  const minuteLocal = createLocalIndicators({
    intervalSeconds: 60, barLimit: MINUTE_HISTORY, minBars: 10, workers: 2,
    onResult(symbol,{outputs}) { rhythm?.observeOpeningPosition(symbol,outputs.opening_position?.[1]??null,Date.now()/1000); },
    onError(symbol, key, error) {
      const id = symbol + '/' + key;
      const now = Date.now();
      if (now - (localErrors.get(id) || 0) < 30000) return;
      localErrors.set(id, now);
      console.error(`[${id}] local_indicator_error: ${error}`);
    },
  });
  const client = await createMarketClient(TradingView, creds);

  const connectionTimer = setTimeout(() => {
    if (client.isOpen && client.isLogged) return;
    console.error('[connection] TradingView 行情连接或授权超时，退出以重新连接');
    process.exit(2);
  }, 20_000);
  connectionTimer.unref();
  client.onConnected(() => console.error('[connection] TradingView websocket connected'));
  client.onDisconnected(() => {
    console.error('[connection] TradingView websocket disconnected; exit for supervised reconnect');
    setTimeout(() => process.exit(2), 50);
  });
  client.onError((...errors) => console.error('[connection] TradingView error:',
    errors.map(String).join(' ').slice(0, 300)));

  // IPOs may have zero bars until their opening auction. This light subscription
  // shares the socket, but never joins the strategy/indicator readiness counts.
  if(args['ipo-watches']) startIpoWatch(client,{
    watches:JSON.parse(args['ipo-watches']),
    emit:record=>process.stdout.write(JSON.stringify(record)+'\n'),
  });

  const state = {};
  const entries = {};   // symbol -> K线会话、本地计算上下文和重建状态

  const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
  function spawnChart(symbol, rebuilds = 0) {
    const chart = new client.Session.Chart();
    const requestedSession = defaultSessionSymbols.has(symbol) ? 'default' : marketSession;
    const snapshot = state[symbol] = {};
    for (const definition of definitions) clearOutputs(snapshot, definition);
    const entry = entries[symbol] = {
      chart, requestedSession,
      createdAt: Date.now(), rebuilds, rebuilding: false, gaveUp: false,
    };
    const updatePrices = createPriceUpdater(closedAtrSma10);
    if (local) local.reset(symbol);
    rhythm?.reset(symbol);
    const submitLocal = () => local?.observe(symbol, chart.periods, chart.infos, snapshot, entry.daily);
    chart.onUpdate((changes) => {
      if (entries[symbol] !== entry || entry.gaveUp || !isPriceUpdate(changes)) return;
      const periods = chart.periods;
      if (!periods.length) return;
      entry.sessionError = null;
      if (!chartSession(chart.infos, requestedSession).accepted) return;
      entry.rebuilding = false;
      archiveBars(symbol,'5m',requestedSession,[...periods].reverse().map(candle),chart.infos);
      updatePrices(snapshot, periods.slice(0, 200));
      if (local) local.observe(symbol, periods, chart.infos, snapshot, entry.daily);
    });
    chart.onError((...error) => {
      entry.sessionError = JSON.stringify(error).slice(0, 150);
      console.error(`[${symbol}/5m/${requestedSession}] err:`, entry.sessionError);
    });
    chart.setMarket(symbol, {
      timeframe: '5', range: BAR_LIMIT,
      session: requestedSession === 'default' ? undefined : requestedSession,
    });
    if (local && (!defaultSessionSymbols.has(symbol) || (rhythm && BETA.includes(symbol)))) {
      const referenceOnly = defaultSessionSymbols.has(symbol);
      const minuteSession = referenceOnly ? 'default' : marketSession;
      // Both resolutions share the configured stock session; indices retain default.
      const minuteRequest = minuteSubscriptionOptions(referenceOnly, MINUTE_HISTORY,marketSession);
      const minuteHistory = minuteRequest.range;
      const subscribeMinute = () => {
        if (entries[symbol] !== entry || entry.gaveUp) return;
        const minuteChart = entry.minuteChart = new client.Session.Chart();
        entry.minuteRaw = null;
        entry.rhythmPrice1m = null;
        if (!referenceOnly) minuteLocal.reset(symbol);
        const current = () => entries[symbol] === entry && entry.minuteChart === minuteChart && !entry.gaveUp;
        const minuteWork=createCoalescedUpdater(()=>{
          if(!current())return;
          const periods=minuteChart.periods.slice(0,minuteHistory);
          const rows=[...periods].reverse().map(candle),candidateNow=Date.now()/1000;
          if(rows.length<2)return;
          // Persist the newly closed bar even if its rollover arrived before
          // the local boundary and there is no subsequent trade this minute.
          if(entry.minuteReceivedAt<rows.at(-1).time)archiveBars(symbol,'1m',minuteSession,rows,minuteChart.infos);
          rhythm?.observe(symbol,rows,candidateNow,
            previousRegularRange(entry.daily,rows.at(-1).time),entry.daily||[],entry.minuteReceivedAt);
          if(referenceOnly)return;
          entry.vwap1m=calculateVwap1m(rows,{marketSession});
          const closedMinutes=rows.filter(bar=>bar.time+60<=candidateNow);
          const candidateKey=JSON.stringify(closedMinutes);
          if(candidateKey!==entry.candidateStKey){
            entry.candidateSt1m=candidateProtectionSnapshot(closedMinutes,candidateNow,{marketSession});
            entry.candidateStKey=candidateKey;
          }
          vwapMinuteBatch.observe(symbol,entry.vwap1m);
          entry.vwapOpening=openingVwapSnapshot(rows,candidateNow,{marketSession});
          entry.minuteRaw={bar:rows.at(-1),bar_c:rows.at(-2)};
          minuteLocal.observe(symbol,periods,minuteChart.infos,entry.minuteRaw,[],entry.minuteReceivedAt*1000);
        },{onError:error=>{
          entry.minuteRaw=null;entry.vwap1m=null;entry.candidateSt1m=null;entry.candidateStKey=null;
          entry.vwapOpening=null;rhythm?.reset(symbol);
          console.error(`[minute/${symbol}] calculation_error: ${error.message}`);
        }});
        const retry = () => {
          if (!current()) return;
          minuteWork.cancel();
          clearTimeout(entry.minuteTimer);
          entry.minuteChart = null;
          entry.minuteRaw = null;
          entry.rhythmPrice1m = null;
          if (!referenceOnly) minuteLocal.reset(symbol);
          rhythm?.reset(symbol);
          entry.vwap1m = null;
          entry.candidateSt1m = null;
          entry.candidateStKey = null;
          entry.vwapOpening = null;
          try { minuteChart.delete(); } catch (_) { /* socket already closed */ }
          entry.minuteTimer = setTimeout(subscribeMinute, Math.max(15000, retryDelay(++entry.minuteFailures)));
        };
        minuteChart.onError(retry);
        minuteChart.onUpdate(changes => {
          if (!current() || !isPriceUpdate(changes) || (referenceOnly
              ? !chartSession(minuteChart.infos, 'default').accepted
              : minuteChart.infos?.subsession_id !== minuteSession)) return;
          const rows = minuteChart.periods.slice(0, minuteHistory).reverse().map(candle);
          archiveBars(symbol,'1m',minuteSession,rows,minuteChart.infos);
          if (rows.length < 2) return;
          clearTimeout(entry.minuteTimer);
          entry.minuteFailures = 0;
          entry.minuteReceivedAt=Date.now()/1000;
          if (rhythm) entry.rhythmPrice1m = {bar: rows.at(-1), price_observed_at: entry.minuteReceivedAt};
          minuteWork.notify(rows.at(-1).time*1000);
        });
        entry.minuteTimer = setTimeout(retry, 30000);
        minuteChart.setMarket(symbol, minuteRequest);
      };
      entry.minuteFailures = 0;
      entry.minuteTimer = setTimeout(subscribeMinute, Math.ceil(args.symbols.length / BATCH_SIZE) * BATCH_DELAY_MS + 5000 + args.symbols.indexOf(symbol) * 1100);
    }
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
          if(chartSession(dailyChart.infos,requestedSession==='default'?'default':'regular').accepted){
            archiveBars(symbol,'1d',requestedSession==='default'?'default':'regular',rows,dailyChart.infos);
            if(args['research-spool']){
              const reference={symbol,daily:rows.map((row,i)=>i===rows.length-1?{time:row.time}:row)};
              const key=researchDigest(reference);
              if(researchDailyKeys.get(symbol)!==key&&research.append('reference',reference))researchDailyKeys.set(symbol,key);
            }
          }
          if (rows.length < 2) return;
          clearTimeout(entry.dailyTimer);
          entry.dailyFailures = 0;
          entry.daily = rows;
          if (rhythm && BETA.includes(symbol) && entry.minuteChart?.periods?.length) {
            const minutes=entry.minuteChart.periods.slice(0,4000).reverse().map(candle);
            rhythm.observe(symbol,minutes,Date.now()/1000,previousRegularRange(rows,minutes.at(-1).time),rows);
          }
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

  async function rebuildChart(symbol, why) {
    const e = entries[symbol];
    if (!e || e.gaveUp) return;
    if (e.rebuilding && Date.now() - e.createdAt <= READY_GRACE_MS) return;
    e.rebuilding = true;
    const n = e.rebuilds + 1;
    try { e.chart.delete(); } catch (_) { /* 旧 session 可能已死，忽略 */ }
    clearTimeout(e.minuteTimer);
    try { e.minuteChart?.delete(); } catch (_) { /* socket already closed */ }
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
    void research.flush();
    if (rhythm) void rhythm.flush(Date.now() / 1000).catch(error => {
      if (Date.now() - rhythmErrorAt >= 30000) {
        rhythmErrorAt = Date.now();
        console.error('[market-rhythm] 缓存写入失败:', error.message);
      }
    });
    const symbols = {};
    const sessions = {};
    const vwapOpening = {};
    for (const [sym, raw] of Object.entries(state)) {
      const entry = entries[sym];
      if (entry.vwapOpening) vwapOpening[sym] = entry.vwapOpening;
      const session = chartSession(entry.chart.infos, entry.requestedSession, entry.sessionError);
      sessions[sym] = { ...session, has_bars: !!raw.bar && session.accepted };
      if (!raw.bar || !session.accepted) continue;
      const s = localSnapshot(local, sym, raw);
      if(rhythm&&BETA.includes(sym)){
        const history=local?.confirmedHistory(sym,s,'trend');
        if(history&&entry.rhythmFiveKey!==history.historyKey){
          entry.rhythmFiveKey=history.historyKey;
          entry.rhythmFivePeriods=[...history.bars].reverse();
          entry.rhythmFiveAtr=new Map(history.rows.map(p=>[p.time,finite(p.Atr_Value)]));
          entry.rhythmFiveEma5=new Map(history.rows.map(p=>[p.time,finite(p.EMA_5)]));
        }else if(!history){
          entry.rhythmFiveKey=null;entry.rhythmFivePeriods=[];entry.rhythmFiveAtr=null;entry.rhythmFiveEma5=null;
        }
        rhythm.observeFiveMinute(sym,entry.rhythmFivePeriods||[],entry.rhythmFiveAtr,Date.now()/1000,entry.rhythmFiveEma5);
      }
      const minute = entry.minuteRaw ? minuteLocal.snapshot(sym, entry.minuteRaw) : null;
      // smart_stop（ss/ss_c 独立通道）推送时合并进 ts/ts_c，消费方无感知
      if (s.bar) symbols[sym] = {
        session,
        vwap_1m: entry.vwap1m || null,
        candidate_st_1m_c: entry.candidateSt1m || null,
        td_exit_1m_c: rhythm?.confirmedTd(sym, Date.now()/1000) || null,
        rhythm_confirmed_1m: rhythm?.confirmedState(sym, Date.now()/1000) || null,
        market_1m: minute ? { ...minute.base, price_observed_at: minute.observedAt / 1000 } : null,
        rhythm_price_1m: rhythm ? entry.rhythmPrice1m || null : undefined,
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
      if (symbols[sym] && minute) {
        symbols[sym].vwap_pullback = minute.outputs.vwap_pullback?.[0] ?? {};
        symbols[sym].vwap_pullback_c = minute.outputs.vwap_pullback?.[1] ?? {};
      }
    }
    const vwapDistribution = vwapMinuteBatch?.snapshot();
    if (Object.keys(symbols).length > 0 || marketSession === '24h' || vwapDistribution?.progress.time) {
      process.stdout.write(JSON.stringify({
        type: 'tick', ts: Date.now() / 1000, symbols, sessions,
        vwap_distribution: vwapDistribution, vwap_opening: vwapOpening,
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

  // Only missing price data can rebuild a chart. Local calculation failures
  // retry in the PineTS worker pool, without discarding healthy price data.
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
  console.error(`daemon K线订阅已发起: ${args.symbols.length} 个标的（5m ${marketSession}: ${args.symbols.length - defaultCount}，默认时段: ${defaultCount}）；0 个 TV 指标订阅；PineTS 本地计算`);
}

main().catch((e) => {
  console.error('FATAL:', e.message || e);
  process.exit(1);
});
