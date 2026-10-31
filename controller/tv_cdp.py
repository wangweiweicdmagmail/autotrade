# -*- coding: utf-8 -*-
"""
控制模块 (TradingView CDP Grid Controller)
==========================================

通过 Chrome DevTools Protocol 直连 Mac 端 TradingView 桌面客户端
（以 --remote-debugging-port=9222 启动），将策略输出的 Top N 股票
灌入多图格布局，并完成视图居中与回读校验。

本模块的实现基于 2026-09-24 本机实测结论（六宫格全部验证通过）：

1. 【连接】GET http://localhost:9222/json 枚举 target。
   - 必须用 "localhost"，用 127.0.0.1 会被 DevTools Host 校验拒绝（仅 Python
     urllib 场景，浏览器/curl 不受影响）。
   - 启动 TradingView 前必须确保无存活实例：Electron 单实例机制会把第二次
     启动转发给旧实例，--remote-debugging-port 参数被丢弃（实测踩坑）。

2. 【图表定位】图表页暴露 window._exposed_chartWidgetCollection，
   其 _chartWidgetsDefs[i].chartWidget 即官方 Charting Library Widget API，
   直接提供 setSymbol()/symbol()，无需模拟键盘、无需 React 逆向。

3. 【格子顺序】_chartWidgetsDefs 数组顺序 ≠ 视觉行列顺序（实测为创建顺序/
   列优先）。每个 def 的 width/height 与 DOM 容器 rect 唯一匹配，
   据此把 symbol 按视觉位置（行优先）精确落位。

4. 【回读校验】setSymbol 后 API 层立即生效（6 格 6ms），但画面切换需
   5~20 秒（免费 BATS 数据 + 并发渲染）。校验必须查 API 层：
   chartWidget.symbol().value()。交易所前缀会被归一化
   （NASDAQ:QQQ -> BATS:QQQ，免费账号走 BATS 行情），比对时只比 ticker。

5. 【视图居中】chartWidget.model().m_model.timeScale().setRightOffset(
   width()/barSpacing()/2) 让最新 K 线居中。rightOffset 以 K 线根数计，
   新 K 线推出后画面会漂移，应在每轮刷新后重新居中（毫秒级成本）。
   scrollToRealtime() 可恢复贴右侧视图。

解耦约定：本模块只接受纯 symbol 列表（List[str]），不感知策略细节。
"""

import asyncio
import json
import logging
import urllib.request
from typing import Dict, List, Optional

import websockets

import config

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 注入脚本：八宫格灌入（几何映射 + 逐格异常隔离）
# ---------------------------------------------------------------------------

_SET_SYMBOLS_JS = r"""
(async () => {
  const symbols = %SYMBOLS%;          // Python 端注入，按视觉行优先排序
  const col = window._exposed_chartWidgetCollection;
  if (!col || !col._chartWidgetsDefs) {
    return JSON.stringify({fatal: 'no _exposed_chartWidgetCollection'});
  }
  const defs = col._chartWidgetsDefs;

  // ---- 几何映射：def(w/h) <-> DOM 容器 rect，得视觉行优先顺序 ----
  const prim = v => (typeof v === 'function') ? v() : v;
  const defGeos = defs.map((d, i) => ({i, w: Math.round(prim(d.width)),
                                       h: Math.round(prim(d.height))}));
  const seen = new Set();
  const domRects = [...document.querySelectorAll('[class*="chart-container"]')]
    .map(el => { const r = el.getBoundingClientRect();
                 return {x: Math.round(r.x), y: Math.round(r.y),
                         w: Math.round(r.width), h: Math.round(r.height)}; })
    .filter(r => r.w > 200)
    .filter(r => { const k = r.x+'_'+r.y;      // DOM 容器会重复出现，去重
                   if (seen.has(k)) return false;
                   seen.add(k); return true; });
  // 按 (y, x) 排序 = 视觉行优先
  domRects.sort((a, b) => (a.y - b.y) || (a.x - b.x));

  // w/h 匹配 def -> 视觉位置（实测唯一匹配）；匹配不到的 def 追加到尾部
  const orderedDefs = [];
  const usedDefs = new Set();
  for (const rect of domRects) {
    const hit = defGeos.find(g => !usedDefs.has(g.i) &&
                                  Math.abs(g.w - rect.w) <= 6);
    if (hit) { orderedDefs.push(hit.i); usedDefs.add(hit.i); }
  }
  for (const g of defGeos) if (!usedDefs.has(g.i)) orderedDefs.push(g.i);

  // ---- 逐格灌入（每格独立捕获异常） ----
  const results = [];
  const n = Math.min(symbols.length, orderedDefs.length);
  for (let k = 0; k < n; k++) {
    const w = defs[orderedDefs[k]].chartWidget;
    const before = (s => (s && typeof s.value === 'function') ? s.value() : s)
                   (w.symbol ? w.symbol() : null);
    try {
      w.setSymbol(symbols[k]);
      results.push({cell: k, defIdx: orderedDefs[k], before,
                    target: symbols[k], ok: true});
    } catch (e) {
      results.push({cell: k, defIdx: orderedDefs[k], before,
                    target: symbols[k], ok: false, error: String(e)});
    }
  }
  return JSON.stringify({cellsFound: domRects.length, defsFound: defs.length,
                         results});
})()
"""

# 回读校验：API 层 symbol（ticker 部分），按视觉行优先
_READBACK_JS = r"""
(() => {
  const defs = window._exposed_chartWidgetCollection._chartWidgetsDefs;
  return JSON.stringify(defs.map(d => {
    try {
      const s = d.chartWidget.symbol().value();
      return String(s).split(':').pop();   // BATS:QQQ -> QQQ，容忍前缀归一化
    } catch (e) { return 'ERR'; }
  }));
})()
"""

# 视图居中：最新 K 线移到画面中部（右侧留半屏）
_CENTER_JS = r"""
(() => {
  const defs = window._exposed_chartWidgetCollection._chartWidgetsDefs;
  const out = [];
  for (let i = 0; i < defs.length; i++) {
    try {
      const ts = defs[i].chartWidget.model().m_model.timeScale();
      ts.setRightOffset(ts.width() / ts.barSpacing() / 2);
      out.push({pane: i, ok: true});
    } catch (e) { out.push({pane: i, ok: false, error: String(e)}); }
  }
  return JSON.stringify(out);
})()
"""


class TradingViewCDPController:
    """TradingView 多图格 CDP 控制器。"""

    def __init__(self, endpoint: str = config.CDP_HTTP_ENDPOINT):
        self.endpoint = endpoint
        self._ws: Optional[websockets.WebSocketClientProtocol] = None
        self._msg_id = 0
        self._pending: Dict[int, asyncio.Future] = {}
        self._reader_task: Optional[asyncio.Task] = None

    # ------------------------------------------------------------------
    # 连接生命周期
    # ------------------------------------------------------------------

    async def __aenter__(self) -> "TradingViewCDPController":
        await self.connect()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

    async def connect(self) -> None:
        """发现 TradingView 图表页 target 并建立 CDP WebSocket 会话。"""
        ws_url = self._find_tv_ws_url()
        logger.info("连接 CDP target: %s", ws_url[:80])
        self._ws = await websockets.connect(ws_url, max_size=16 * 1024 * 1024)
        self._reader_task = asyncio.create_task(self._reader_loop())
        await self._send_cdp("Runtime.enable")

    async def close(self) -> None:
        if self._reader_task:
            self._reader_task.cancel()
            self._reader_task = None
        if self._ws:
            await self._ws.close()
            self._ws = None

    def _find_tv_ws_url(self) -> str:
        """
        GET /json 枚举 target，返回 TradingView 图表页的 webSocketDebuggerUrl。
        优先匹配 URL 含 tradingview 且路径含 /chart 的 page target。
        """
        url = f"{self.endpoint}/json"   # 注意：endpoint 必须用 localhost
        with urllib.request.urlopen(url, timeout=config.CDP_TIMEOUT) as resp:
            targets = json.loads(resp.read().decode("utf-8"))
        pages = [t for t in targets if t.get("type") == "page"]
        if not pages:
            raise RuntimeError(
                f"CDP 端点 {url} 无 page target。请确认：1) TradingView 已无"
                "存活实例；2) 以 --remote-debugging-port=9222 重新启动")
        for t in pages:                      # 第一优先：图表页
            u = (t.get("url") or "").lower()
            if "tradingview" in u and "/chart" in u:
                return t["webSocketDebuggerUrl"]
        for t in pages:                      # 第二优先：任意 TV 页面
            if "tradingview" in (t.get("url") or "").lower():
                return t["webSocketDebuggerUrl"]
        logger.warning("未找到 tradingview page，回退到第一个 page: %s",
                       pages[0].get("url"))
        return pages[0]["webSocketDebuggerUrl"]

    # ------------------------------------------------------------------
    # CDP 消息收发
    # ------------------------------------------------------------------

    async def _send_cdp(self, method: str, params: Optional[dict] = None) -> dict:
        assert self._ws is not None, "connect() 未被调用"
        self._msg_id += 1
        msg_id = self._msg_id
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[msg_id] = fut
        await self._ws.send(json.dumps({
            "id": msg_id, "method": method, "params": params or {},
        }))
        reply = await asyncio.wait_for(fut, timeout=config.CDP_TIMEOUT)
        if "error" in reply:
            raise RuntimeError(f"CDP {method} 失败: {reply['error']}")
        return reply.get("result", {})

    async def _reader_loop(self) -> None:
        try:
            async for raw in self._ws:
                msg = json.loads(raw)
                msg_id = msg.get("id")
                if msg_id in self._pending:
                    self._pending.pop(msg_id).set_result(msg)
        except (asyncio.CancelledError, websockets.ConnectionClosed):
            pass

    async def evaluate(self, expression: str) -> object:
        """Runtime.evaluate 包装：awaitPromise + returnByValue。"""
        result = await self._send_cdp("Runtime.evaluate", {
            "expression": expression,
            "awaitPromise": True,
            "returnByValue": True,
        })
        if result.get("exceptionDetails"):
            raise RuntimeError(
                f"JS 执行异常: {result['exceptionDetails'].get('text')}")
        return result.get("result", {}).get("value")

    # ------------------------------------------------------------------
    # 对外能力
    # ------------------------------------------------------------------

    async def apply_grid(self, symbols: List[str],
                         center_view: bool = True) -> dict:
        """
        将 symbol 列表按视觉行优先灌入多图格，可选居中视图，并回读校验。

        :param symbols:     策略模块输出的降序 symbol 列表（行优先）
        :param center_view: 灌入后是否执行最新 K 线居中
        :return: 结构化报告 {灌入结果, 校验结果}
        """
        if not symbols:
            logger.warning("apply_grid 收到空列表，跳过本轮 UI 刷新")
            return {"skipped": True}

        capacity = config.GRID_ROWS * config.GRID_COLS
        if len(symbols) > capacity:
            logger.warning("symbol 数量 %d 超过网格容量 %d，截断",
                           len(symbols), capacity)
            symbols = symbols[:capacity]

        # 1) 灌入
        js = _SET_SYMBOLS_JS.replace("%SYMBOLS%", json.dumps(symbols))
        report = json.loads(await self.evaluate(js))
        if "fatal" in report:
            raise RuntimeError(f"图表页未就绪: {report['fatal']}")
        ok = sum(1 for r in report["results"] if r.get("ok"))
        logger.info("灌入完成: %d/%d (cells=%d, defs=%d)",
                    ok, len(symbols),
                    report.get("cellsFound", 0), report.get("defsFound", 0))
        for r in report["results"]:
            if not r.get("ok"):
                logger.warning("格子 %d (%s) 灌入失败: %s",
                               r.get("cell"), r.get("target"), r.get("error"))

        # 2) 视图居中（每轮重居中，抵消新 K 线推出造成的漂移）
        if center_view:
            center_report = json.loads(await self.evaluate(_CENTER_JS))
            report["centered"] = center_report

        # 3) 回读校验（API 层，ticker 比对，容忍交易所前缀归一化）
        await asyncio.sleep(0.5)
        actual = json.loads(await self.evaluate(_READBACK_JS))
        expected_tickers = [s.split(":")[-1].upper() for s in symbols]
        matched = sum(1 for t in expected_tickers if t in actual)
        report["verify"] = {"expected": expected_tickers,
                            "actual": actual, "matched": matched}
        if matched < len(expected_tickers):
            logger.warning("回读校验不完整: 期望 %s, 实际 %s",
                           expected_tickers, actual)
        return report

    async def center_all(self) -> dict:
        """单独触发一次全格视图居中。"""
        return json.loads(await self.evaluate(_CENTER_JS))

    async def screenshot(self, path: Optional[str] = None, quality: int = 70,
                         clip: Optional[dict] = None) -> bytes:
        """Page.captureScreenshot 截屏。clip={x,y,width,height,scale?}
        （CSS 像素，页面坐标）只截指定区域；path 提供时落盘。
        返回 JPEG bytes。"""
        import base64
        params: dict = {"format": "jpeg", "quality": quality}
        if clip:
            params["clip"] = {"scale": 1, **clip}
        result = await self._send_cdp("Page.captureScreenshot", params)
        data = base64.b64decode(result["data"])
        if path:
            with open(path, "wb") as f:
                f.write(data)
        return data
