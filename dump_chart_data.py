# -*- coding: utf-8 -*-
"""dump_chart_data.py — 从 TV 图表精确导出 K线+指标线值（供回测/迁移用）

用法:
  python3 dump_chart_data.py NASDAQ:TSLA NASDAQ:ARM [--outdir /tmp]
  # 生成 {outdir}/{ticker}_full.json, 每行字段:
  # t,o,h,l,c,vol, ma1(EMA20),ma2(EMA100), stAup/stAdn,stBup/stBdn(双ST), vwap

前置: TV 图表页以 --remote-debugging-port=9222 运行, 且图表上已挂
      fast/slow MA、双 SuperTrend、VWAP 指标（单格任一标的即可，
      本脚本会自动切换 symbol 并等待数据加载）。
注意: VWAP 等线值必须读 TV 计算值, 无法从 bar 精确复算（见 PLAYBOOK §9）。
"""
import asyncio
import json
import sys

from controller.tv_cdp import TradingViewCDPController

DUMP_JS = r'''
(() => {
  const w = window._exposed_chartWidgetCollection._chartWidgetsDefs[0].chartWidget;
  const model = w.model().m_model;
  const bars = model.mainSeries().bars();
  const studies = {ma: {}, sts: [], vw: {}};
  for (const s of model.dataSources()) {
    try {
      if (typeof s.metaInfo !== 'function') continue;
      const t = String(s.metaInfo().description || '');
      const d = s.data && s.data();
      if (!d || !d._items || !d._items.length) continue;
      const m = {};
      for (const it of d._items) {
        let v; try { v = it.value || it; } catch(e) { continue; }
        if (v && typeof v[0] === 'number') m[v[0]] = v;
      }
      if (t.startsWith('Multi Timeframe')) studies.ma = m;
      else if (t.startsWith('Supertrend')) studies.sts.push(m);
      else if (t.startsWith('Volume Weighted')) studies.vw = m;
    } catch(e) {}
  }
  const rows = [];
  const n = bars.size();
  for (let i = 0; i < n; i++) {
    let v; try { v = bars.valueAt(i); } catch(e) { continue; }
    if (!v) continue;
    const t = v[0];
    const row = {t, o: v[1], h: v[2], l: v[3], c: v[4], vol: v[5]};
    const ma = studies.ma[t]; if (ma) { row.ma1 = ma[1]; row.ma2 = ma[3]; }
    if (studies.sts[0] && studies.sts[0][t]) { row.stAup = studies.sts[0][t][1]; row.stAdn = studies.sts[0][t][2]; }
    if (studies.sts[1] && studies.sts[1][t]) { row.stBup = studies.sts[1][t][1]; row.stBdn = studies.sts[1][t][2]; }
    const vw = studies.vw[t]; if (vw) { row.vwap = vw[1]; }
    rows.push(row);
  }
  return JSON.stringify({count: rows.length, rows});
})()
'''

SET_SYMBOL_JS = ('window._exposed_chartWidgetCollection._chartWidgetsDefs[0]'
                 '.chartWidget.setSymbol(%s); "ok"')


async def main(symbols, outdir, restore='NYSE:TSM'):
    async with TradingViewCDPController() as c:
        for sym in symbols:
            await c.evaluate(SET_SYMBOL_JS % json.dumps(sym))
            await asyncio.sleep(15)  # 等指标数据加载
            r = json.loads(await c.evaluate(DUMP_JS))
            name = sym.split(':')[-1].lower()
            path = f'{outdir}/{name}_full.json'
            with open(path, 'w') as f:
                json.dump(r['rows'], f)
            print(f'{sym} -> {path} ({r["count"]} bars)')
        if restore:
            await c.evaluate(SET_SYMBOL_JS % json.dumps(restore))


if __name__ == '__main__':
    args = [a for a in sys.argv[1:] if not a.startswith('--')]
    outdir = next((a.split('=', 1)[1] for a in sys.argv[1:]
                   if a.startswith('--outdir=')), '/tmp')
    if not args:
        print(__doc__)
        sys.exit(1)
    asyncio.run(main(args, outdir))
