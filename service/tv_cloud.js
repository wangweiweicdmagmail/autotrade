/**
 * TradingView 云端数据服务 (tv_cloud.js)
 * ======================================
 *
 * 基于开源项目 TradingView-API (@mathieuc/tradingview) 二次开发：
 *   https://github.com/Mathieu2301/Tradingview-API
 *
 * 能力（全部经由 TradingView 云端计算，无需 TV 界面显示）：
 *   - K线 OHLCV（chart session）
 *   - 指标数据（STD 内置指标 + 用户私有 Pine 脚本，study periods）
 *   - 信号数据（图形型指标的 boxes/lines，study.graphic）
 *
 * 用法：
 *   node tv_cloud.js --symbols NASDAQ:QQQ,NASDAQ:AAPL [--timeframe 5]
 *                    [--range 300] [--session 24x7] [--out result.json]
 *
 * 输出：JSON -> stdout（或 --out 文件），结构：
 *   { "generated_at": ..., "symbols": {
 *       "NASDAQ:QQQ": {
 *         "bars":  [{time, open, high, low, close, volume}, ...],
 *         "studies": { "supertrend": {"periods": [...], "graphic": {...} } }
 *       } } }
 *
 * 实测要点（2026-09-24 本机验证）：
 *   - 登录态来自 sessionid/sessionid_sign cookie（creds.json）；
 *   - 图形型指标（5M 进攻区域）必须用 24x7 会话，否则服务端返回空；
 *   - 受邀保护类脚本（如"超级信号"）pine-facade 返回 404，无法云端化。
 */

'use strict';

const fs = require('fs');
const path = require('path');
const TradingView = require('@mathieuc/tradingview');

// ---------------------------------------------------------------------------
// 参数解析
// ---------------------------------------------------------------------------

function parseArgs(argv) {
  const args = {
    symbols: ['NASDAQ:QQQ'],
    timeframe: '5',
    range: 300,
    session: 'extended',
    creds: path.join(__dirname, 'creds.json'),
    indicators: path.join(__dirname, 'indicators.json'),
    out: null,
    timeout: 30000,
  };
  for (let i = 2; i < argv.length; i += 2) {
    const key = argv[i].replace(/^--/, '');
    const val = argv[i + 1];
    if (key === 'symbols') args.symbols = val.split(',');
    else if (key === 'range' || key === 'timeout') args[key] = Number(val);
    else args[key] = val;
  }
  return args;
}

// ---------------------------------------------------------------------------
// 单标的采集
// ---------------------------------------------------------------------------

/**
 * 为一个标的采集快照。支持多周期：indicators.json 里每个指标可带
 * "timeframe" 字段（缺省用 --timeframe），按周期分组，每个周期建一个
 * chart session（共享一条 WS 连接）；bars 取自主周期会话。
 */
function collectSymbol(client, indicMap, symbol, args) {
  return new Promise((resolve) => {
    // 按周期分组：tf -> [{ key, indic }]
    const groups = {};
    for (const [key, indic] of Object.entries(indicMap)) {
      const tf = indic.__timeframe || args.timeframe;
      (groups[tf] = groups[tf] || []).push({ key, indic });
    }

    const studies = {};
    const pending = new Set(Object.keys(indicMap));
    const charts = [];
    let bars = [];
    let settled = false;

    const finish = (reason) => {
      if (settled) return;
      settled = true;
      for (const c of charts) { try { c.delete(); } catch (e) { /* ignore */ } }
      const outStudies = {};
      for (const [key, st] of Object.entries(studies)) {
        let graphic = null;
        let hasGfx = false;
        try {
          graphic = st.graphic || null;
          hasGfx = graphic && Object.values(graphic).some(
            (arr) => Array.isArray(arr) && arr.length > 0);
        } catch (e) { /* graphic 未就绪时 getter 可能抛异常 */ }
        // 注意：TradingView-API 的 periods 为倒序（[0]=最新），
        // 取最近 50 点后翻转为正序输出
        const periods = (st.periods || []).slice(0, 50).reverse();
        outStudies[key] = {
          periods,
          graphic,
          ready: Boolean((st.periods && st.periods.length > 0) || hasGfx),
        };
      }
      resolve({
        reason,
        // chart.periods 同样倒序：取最近 range 根翻转为正序
        bars: bars.slice(0, args.range).reverse(),
        studies: outStudies,
      });
    };

    const timer = setTimeout(() => finish('timeout'), args.timeout
      * Object.keys(groups).length);

    const checkDone = () => {
      if (pending.size === 0 && bars.length > 0) {
        clearTimeout(timer);
        finish('ok');
      }
    };

    for (const [tf, items] of Object.entries(groups)) {
      const chart = new client.Session.Chart();
      charts.push(chart);
      const isPrimary = tf === args.timeframe;

      chart.onError((...err) => {
        clearTimeout(timer);
        finish(`chart_error:${tf}:${JSON.stringify(err).slice(0, 200)}`);
      });

      chart.onUpdate(() => {
        if (isPrimary && chart.periods && chart.periods.length) {
          bars = chart.periods;
        }
        checkDone();
      });

      chart.setMarket(symbol, {
        timeframe: tf,
        range: args.range,
        session: args.session,   // 实测：图形型指标必须 extended
      });

      for (const { key, indic } of items) {
        const study = new chart.Study(indic);
        studies[key] = study;
        study.onUpdate(() => {
          const hasPeriods = study.periods && study.periods.length > 0;
          let hasGfx = false;
          try {
            const g = study.graphic;
            hasGfx = g && Object.values(g).some(
              (arr) => Array.isArray(arr) && arr.length > 0);
          } catch (e) { /* graphic getter 未就绪时可能抛异常 */ }
          if (hasPeriods || hasGfx) {
            pending.delete(key);
            checkDone();
          }
        });
        study.onError((...err) => {
          console.error(`[${symbol}/${key}] study_error:`,
                        JSON.stringify(err).slice(0, 200));
          pending.delete(key);   // 单指标失败不阻塞整体
          checkDone();
        });
      }
    }
  });
}

// ---------------------------------------------------------------------------
// 主流程
// ---------------------------------------------------------------------------

async function main() {
  const args = parseArgs(process.argv);
  const creds = JSON.parse(fs.readFileSync(args.creds, 'utf8'));
  const indicSpecs = JSON.parse(fs.readFileSync(args.indicators, 'utf8'));

  // 1) 加载指标描述符（私有脚本需 sessionid），应用 inputs 覆盖与周期
  const indicMap = {};
  await Promise.all(indicSpecs.map(async (spec) => {
    try {
      const indic = await TradingView.getIndicator(
        spec.id, 'last', creds.sessionid, creds.sessionid_sign);
      // inputs 覆盖：key 支持 in_N / inline 名 / internalID
      // （如 STD;Supertrend 的 Factor / ATR Length）
      for (const [k, v] of Object.entries(spec.inputs || {})) {
        try { indic.setOption(k, v); } catch (e) {
          console.error(`指标 ${spec.key} 参数 ${k} 设置失败:`,
                        (e.message || e).toString().slice(0, 120));
        }
      }
      indic.__timeframe = spec.timeframe || null;   // 缺省用主周期
      indicMap[spec.key] = indic;
    } catch (e) {
      console.error(`指标加载失败 ${spec.key} (${spec.id}):`,
                    (e.message || e).toString().slice(0, 120));
    }
  }));
  console.error(`指标就绪: ${Object.keys(indicMap).join(', ')}`);

  // 2) 建立登录态 WS 客户端
  const client = new TradingView.Client({
    token: creds.sessionid,
    signature: creds.sessionid_sign,
  });

  // 3) 逐标的采集（串行，避免并发触发服务端限流；实测可后续改批量）
  const result = { generated_at: Date.now() / 1000, symbols: {} };
  for (const symbol of args.symbols) {
    console.error(`采集 ${symbol} ...`);
    result.symbols[symbol] = await collectSymbol(client, indicMap, symbol, args);
    console.error(`  -> ${result.symbols[symbol].reason}, `
                  + `bars=${result.symbols[symbol].bars.length}`);
  }

  client.end();
  const json = JSON.stringify(result);
  if (args.out) {
    fs.writeFileSync(args.out, json);
    process.exit(0);
  } else {
    // 注意：大输出时 process.exit 会截断未 flush 的 stdout，
    // 必须在 write 回调里退出
    process.stdout.write(json, () => process.exit(0));
  }
}

main().catch((e) => {
  console.error('FATAL:', e.message || e);
  process.exit(1);
});
