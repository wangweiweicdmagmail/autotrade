'use strict';
// Map local PineTS plots to the existing realtime/confirmed snapshot contract.
const {loadPineModels,outputFromPeriod,periodFromOutput}=require('./pine_models');
const PINE_MODELS=loadPineModels();
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
//   premarketHigh / premarketLow  当天盘前区间（PineTS 按原 Pine 计算，
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
// 键名是中文 plot 标题被剥离非 ASCII 后的兼容输出名称。
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

  const definitions = [
    { name: '趋势状态', key: 'ts', confirmed: 'ts_c',
      read: tsFromPeriod, extra: ['breakoutAtr'],
      history: (s, ps) => { s.breakoutAtr = new Map(ps.slice(1, 161).map(p => [p.$time, finite(p.Atr_Value)])); } },
    { name: 'FVG Finder+', key: 'fvg', confirmed: 'fvg_c', read: fvgFromPeriod },
    { name: '智能止损', key: 'ss', confirmed: 'ss_c', read: ssFromPeriod },
    ...PINE_MODELS.map(model => ({
      name: model.name || model.id, model,
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
    { name: '5M 进攻区域', key: 'attack', confirmed: 'attack_c',
      read: attackFromPeriod, extra: ['breakoutRanges'],
      history: (s, ps) => {
        s.breakoutRanges = new Map(ps.slice(1, 161).map(p =>
          [p.$time, [finite(p.opening3High ?? p.K), finite(p.opening3Low ?? p.K_2)]]));
      } },
  ];

  function clearOutputs(snapshot, definition) {
    snapshot[definition.key] = {};
    snapshot[definition.confirmed] = {};
    for (const key of definition.extra || []) delete snapshot[key];
  }

  function applyOutput(snapshot, definition, periods) {
    snapshot[definition.key] = definition.read(periods[0], periods, 0);
    snapshot[definition.confirmed] = periods[1] ? definition.read(periods[1], periods, 1) : {};
    definition.history?.(snapshot, periods);
  }

  function localSnapshot(local, symbol, raw) {
    const { base, outputs, observedAt } = local.snapshot(symbol, raw);
    const snapshot = { ...base, price_observed_at: observedAt == null ? undefined : observedAt / 1000 };
    for (const definition of definitions) {
      const key = { ts: 'trend', ss: 'smart' }[definition.key] || definition.key;
      const rows = outputs[key];
      if (rows?.length) {
        const periods = rows.map(row => definition.model ? periodFromOutput(definition.model, row) :
          row && { ...row, $time: row.time });
        applyOutput(snapshot, definition, periods);
      }
    }
    return snapshot;
  }


module.exports={PINE_MODELS,definitions,clearOutputs,localSnapshot,tsFromPeriod,ssFromPeriod,fvgFromPeriod,attackFromPeriod};
