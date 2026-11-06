'use strict';

const fs = require('node:fs');
const path = require('node:path');
const { loadPineModels, outputFromPeriod } = require('./pine_models');

const versions = {
  e9: '12.0', e20: '1.0', e7v2: '3.0', trend: '27.0', fvg: '7.0',
  smart: '1.0', attack: '17.0',
};
const supplementalPlots = {
  trend: {
    Trend_State: 'Trend State', Atr_Value: 'Atr Value', ST_Duration_Bars: 'ST Duration Bars',
    ST_Push_Count: 'ST Push Count', EMA_Fast: 'EMA Fast', EMA_Slow: 'EMA Slow',
    Up_Trend: 'Up Trend', Down_Trend: 'Down Trend',
    Uptrend_Channel: 'Uptrend Channel', Downtrend_Channel: 'Downtrend Channel',
  },
  fvg: {
    STFVG: 'ST同向FVG累积计数', FVG: '最后FVG上沿', FVG_2: '最后FVG下沿',
    KFVG110: '当前K线FVG信号(1涨/-1跌/0无)',
  },
  smart: { Smart_Stop_Long: 'Smart Stop Long', Smart_Stop_Short: 'Smart Stop Short' },
  attack: {
    Bar_Color: 'barcolor#0', plot_1: '盘前高点', _2: '盘前低点',
    K: '开盘前三根K线高点', K_2: '开盘前三根K线低点', Opening3_Distance: 'Opening3_Distance',
  },
};

function getDefinition(id) {
  const model = loadPineModels().find(item => item.id === id || item.realtime_key === id);
  const key = model?.realtime_key ?? id;
  const plots = model?.plots ?? supplementalPlots[key];
  if (!plots || !Object.hasOwn(versions, key)) throw new Error(`未登记的本地指标: ${id}`);
  return {
    key, version: versions[key], model, plots,
    source: fs.readFileSync(path.join(__dirname, '..', 'indicators', 'pinets', key + '.pine'), 'utf8'),
  };
}

function loadRuntime(version) {
  let pinets;
  let installedVersion;
  try {
    const dist = path.dirname(require.resolve('pinets'));
    installedVersion = JSON.parse(fs.readFileSync(path.join(dist, '..', 'package.json'), 'utf8')).version;
    pinets = require('pinets');
  } catch {
    throw new Error('PineTS 加载失败，请在项目目录运行 npm ci');
  }
  if (installedVersion !== version) throw new Error('PineTS 版本与锁文件不一致，请运行 npm ci');
  return pinets;
}

function candles(rows, seconds) {
  let previous = 0;
  return rows.map(({ time, close_time, ...ohlcv }) => {
    if (!Number.isSafeInteger(time) || time <= previous) throw new Error('PineTS 输入 K 线时间重复或乱序');
    previous = time;
    return { ...ohlcv, openTime: time * 1000, closeTime: (close_time ?? time + seconds) * 1000 };
  });
}

function createDatasetProvider({ symbol, metadata, bars, daily }) {
  const [prefix, ticker] = symbol.split(':');
  const { timezone, minmove, pricescale } = metadata;
  if (!timezone || !(minmove > 0) || !(pricescale > 0)) throw new Error('PineTS 缺少标的元数据');
  let fiveMinutes = candles(bars, 300);
  const dailyBars = daily && candles(daily, 86400);
  function checkSymbol(id) {
    // PineTS can pass the unqualified ticker for request.security(syminfo.tickerid, ...).
    if (![symbol, ticker].includes(id)) throw new Error(`PineTS 请求了未提供的标的: ${id}`);
  }
  return {
    configure() {},
    replaceLast(bar) {
      const [current] = candles([bar], 300);
      if (current.openTime !== fiveMinutes.at(-1)?.openTime) throw new Error('盘中重算只能替换当前 K 线');
      fiveMinutes = [...fiveMinutes.slice(0, -1), current];
    },
    append(finalBar, currentBar) {
      const [final, current] = candles([finalBar, currentBar], 300);
      if (final.openTime !== fiveMinutes.at(-1)?.openTime) throw new Error('换根时缺少上一根最终 K 线');
      fiveMinutes = [...fiveMinutes.slice(0, -1), final, current];
    },
    async getMarketData(id, timeframe, limit, start, end) {
      checkSymbol(id);
      if (String(timeframe) === '5') return start == null ? fiveMinutes : fiveMinutes.filter(bar => bar.openTime >= start);
      if (['D', '1D'].includes(timeframe) && dailyBars?.length) {
        // Include the preceding daily bar for [1] under lookahead_on.
        return end == null ? dailyBars : dailyBars.filter(bar => bar.openTime <= end);
      }
      throw new Error(`PineTS 请求了未提供的周期: ${timeframe}`);
    },
    async getSymbolInfo(id) {
      checkSymbol(id);
      return { tickerid: symbol, main_tickerid: symbol, prefix, ticker, timezone,
        minmove, pricescale, mintick: minmove / pricescale, type: metadata.type, session: metadata.session };
    },
  };
}

function plotValue(plot, { value, options }) {
  if (plot === 'barcolor#0') {
    if (options?.color === '#4CAF50') return 0;
    if (options?.color === '#F23645') return 1;
    return null;
  }
  return typeof value === 'boolean' ? Number(value) : value;
}

function createCalculator(definition, { version, source = definition.source, inputs = {} }) {
  const { PineTS, Indicator } = loadRuntime(version);
  const indicator = new Indicator(source, inputs);
  function readOutput({ plots }, bars, tail, revised = false) {
    const selected = tail ? bars.slice(-tail) : bars;
    const periods = new Map(selected.map(({ time }) => [time * 1000, { $time: time }]));
    for (const plot of Object.values(definition.plots)) {
      const values = plots[plot]?.data;
      if (!values || (!revised && values.length !== bars.length)) throw new Error(`PineTS 输出不完整: ${plot}`);
      if (revised) {
        let remaining = periods.size;
        for (let i = values.length - 1; i >= 0 && remaining; i--) {
          const point = values[i];
          const row = periods.get(point.time);
          if (!row || Object.hasOwn(row, plot)) continue;
          row[plot] = plotValue(plot, point);
          remaining--;
        }
        if (remaining) throw new Error(`PineTS 盘中输出缺少 K 线: ${plot}`);
        continue;
      }
      const relevant = tail ? values.slice(-tail) : values;
      for (const point of relevant) {
        const row = periods.get(point.time);
        if (!row || Object.hasOwn(row, plot)) throw new Error(`PineTS 输出时间未对齐: ${plot}`);
        row[plot] = plotValue(plot, point);
      }
    }
    return [...periods.values()].map(row => outputFromPeriod(definition, row));
  }
  async function open(dataset) {
    if (definition.key === 'attack' && !dataset.daily?.length) throw new Error('5M 进攻区域需要日线');
    const { symbol, bars } = dataset;
    const provider = createDatasetProvider(dataset);
    const pine = new PineTS(provider, symbol, '5', bars.length);
    const context = await pine.run(indicator);
    let outputBars = bars;
    let appended = 0;
    return {
      read(tail = 0) { return readOutput(context, outputBars, tail); },
      async replaceLast(bar, tail = 2) {
        provider.replaceLast(bar);
        if (!await pine.updateTail(context)) throw new Error('PineTS 未重算当前 K 线');
        return readOutput(context, outputBars, tail, true);
      },
      async advance(nextBars, tail) {
        // Bound how long a mutable PineTS context is kept across bars.
        if (appended >= 24) throw new Error('PineTS 需要重新预热');
        provider.append(nextBars.at(-2), nextBars.at(-1));
        if (!await pine.updateTail(context)) throw new Error('PineTS 未处理新 K 线');
        outputBars = nextBars;
        appended++;
        return readOutput(context, outputBars, tail, true);
      },
    };
  }
  const calculate = async (dataset, { tail = 0 } = {}) => (await open(dataset)).read(tail);
  calculate.open = open;
  return calculate;
}

module.exports = { createCalculator, getDefinition };
