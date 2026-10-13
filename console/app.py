# -*- coding: utf-8 -*-
"""
下单控制台后端 (FastAPI)
=========================

提供给 Web 控制页（console/static）的 REST + WebSocket 接口：

- REST：状态 / 下单（分批建仓）/ 订单列表 / 聚合仓位 / 改 TRAIL / 撤单 / 刷新。
- WebSocket /ws：每 2s 推送一次快照（状态 + 聚合仓位 + 全部 lot），
  前端据此实时刷新，无需轮询。

运行（默认 dry-run，不发真实订单）：:

    EXECUTION_ENABLED=0 uvicorn console.app:app --host 127.0.0.1 --port 8000

实盘（先确认 config.RISK_* 与 TWS 已就绪）：:

    EXECUTION_ENABLED=1 uvicorn console.app:app --host 127.0.0.1 --port 8000
"""

import asyncio
import collections
import logging
import sys
import time
from pathlib import Path
from typing import List, Optional, Set

# 允许从项目根目录 import config / execution
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import config
from engine.realtime_collector import RealtimeCollector
from execution.ibkr import IBKRGateway
from execution.order_lot import OrderManager
from execution.risk import RiskConfig, RiskGate

logging.basicConfig(level=getattr(logging, config.LOG_LEVEL, "INFO"),
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("console")

# ib_async 日志降噪：持仓/成交回报极多，压到 WARNING
logging.getLogger("ib_async").setLevel(logging.WARNING)
logging.getLogger("ib_async.wrapper").setLevel(logging.WARNING)
logging.getLogger("ib_async.client").setLevel(logging.WARNING)

# 内存日志环形缓冲区（供前端日志窗口展示）
_log_buffer: "collections.deque" = collections.deque(maxlen=300)


class _BufferHandler(logging.Handler):
    """把日志写入内存环形缓冲区，供 /api/logs 与 WebSocket 推送。"""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            _log_buffer.append({
                "ts": record.created,
                "level": record.levelname,
                "name": record.name.split(".")[-1],
                "msg": record.getMessage(),
            })
        except Exception:
            pass


_buffer_handler = _BufferHandler()
_buffer_handler.setLevel(logging.INFO)
logging.getLogger().addHandler(_buffer_handler)

STATIC_DIR = Path(__file__).resolve().parent / "static"

# ---------------------------------------------------------------------------
# 请求模型
# ---------------------------------------------------------------------------


class PlaceLotRequest(BaseModel):
    symbol: str = Field(..., description="如 NVDA / NASDAQ:NVDA")
    side: str = "long"
    qty: int = 0                      # 0 = 按净值自动算股数
    entry_px: float = Field(..., gt=0)
    stop_px: float = Field(..., gt=0)
    trail_amount: float = Field(..., gt=0, description="TRAIL 跟踪幅度（美元/股）")
    reason: str = ""


class UpdateTrailRequest(BaseModel):
    stop_px: float = Field(..., gt=0)


class TvCalcRequest(BaseModel):
    symbols: List[str] = Field(..., description="要拉取 TV 数据的标的列表")


class LimitStopOrderRequest(BaseModel):
    symbol: str = Field(..., description="如 NVDA / NASDAQ:NVDA")
    side: str = "long"
    qty: int = 0                      # 0 = 按净值自动算股数
    limit_px: float = Field(..., gt=0)
    stop_px: float = Field(..., gt=0)
    limit_type: str = "ema"           # ema / channel（仅用于记录来源）
    reason: str = ""
    risk_usd: float = 0               # 设定风险金额（风控偏差 10% 容差检查）


# ---------------------------------------------------------------------------
# 依赖组装
# ---------------------------------------------------------------------------

gateway: IBKRGateway = IBKRGateway.from_config()
risk_gate: RiskGate = RiskGate(RiskConfig(
    per_trade_pct=config.RISK_PER_TRADE_PCT,
    per_trade_usd=config.RISK_PER_TRADE_USD,
    max_order_qty=config.RISK_MAX_ORDER_QTY,
    max_open_positions=config.RISK_MAX_OPEN_POSITIONS,
    daily_loss_limit_usd=config.RISK_DAILY_LOSS_LIMIT_USD,
))
order_mgr: OrderManager = OrderManager(gateway, risk_gate)

app = FastAPI(title="autotrade 下单控制台", version="0.1.0")

# WebSocket 连接集合
_ws_clients: Set[WebSocket] = set()
_broadcast_task: Optional[asyncio.Task] = None
_eod_task: Optional[asyncio.Task] = None

# TV 实时订阅采集器（1M K线/EMA7 + 5M st_channel）
collector: RealtimeCollector = RealtimeCollector(config.WATCHLIST)

# 净值/日盈亏缓存（避免每秒查 IBKR）
_equity_cache = {"equity": None, "daily_pnl": None, "ts": 0.0}

# ST 回踩信号状态（代码实现两级信号：预警 + 入场）
_entry_state: dict = {}   # 入场信号（稳定 K线 + 确认指标）→ 下单
_alert_state: dict = {}   # 预警信号（实时 K线 + 确认指标）→ 提醒
_signal_task: Optional[asyncio.Task] = None
ST_WICK_RATIO = 0.4       # 长下影线阈值（影线/整K线幅度 ≥ 40%）


# ---------------------------------------------------------------------------
# 快照
# ---------------------------------------------------------------------------

async def get_status_cached() -> dict:
    """净值/日盈亏缓存（每 10s 刷新一次）。"""
    global _equity_cache
    if time.time() - _equity_cache["ts"] > 10.0:
        _equity_cache["equity"] = await gateway.net_liquidation()
        _equity_cache["daily_pnl"] = await gateway.daily_pnl()
        _equity_cache["ts"] = time.time()
    return _equity_cache


def _is_trading_hours() -> bool:
    """是否在美股常规交易时段（9:30-16:00 ET，周一~周五）。"""
    from zoneinfo import ZoneInfo
    import datetime as _dt
    now = _dt.datetime.now(ZoneInfo("America/New_York"))
    if now.weekday() >= 5:            # 周六/周日
        return False
    minutes = now.hour * 60 + now.minute
    return 9 * 60 + 30 <= minutes < 16 * 60


def _is_long_candidate(data: dict) -> bool:
    """是否多头候选：直接用 langfilter 指标的 signalOutput（Filter_Output_10）。"""
    flt = data.get("filter") or {}
    return flt.get("long_candidate") == 1


def _check_signal(bar: dict, st_c: dict):
    """回踩信号判断：bar 的 K 线形态 + 确认指标（st_c）。返回 'long'/'short'/None。"""
    low, high, close = bar.get("low"), bar.get("high"), bar.get("close")
    if low is None or high is None or close is None:
        return None
    rng = high - low
    if rng <= 0:
        return None
    if st_c.get("up_trend") is not None:          # 确认多头
        zone = st_c.get("zone_up")
        if zone is not None and low <= zone and (close - low) / rng >= ST_WICK_RATIO:
            return "long"
    elif st_c.get("down_trend") is not None:      # 确认空头
        zone = st_c.get("zone_down")
        if zone is not None and high >= zone and (high - close) / rng >= ST_WICK_RATIO:
            return "short"
    return None


async def _signal_detector_loop() -> None:
    """信号检测：入场信号（Pine 指标 Pierce_Signal，所见即所得）+ 预警（代码实时 K 线提醒）。"""
    while True:
        await asyncio.sleep(1.0)
        try:
            if not _is_trading_hours():
                continue   # 非正式交易时段：不检测信号、不提醒
            market = collector.latest()
            for symbol, data in market.items():
                st_c = data.get("st_c") or data.get("st") or {}   # 确认指标
                bar = data.get("bar") or {}       # 实时 K 线（未收盘）
                bar_c = data.get("bar_c") or {}   # 上一根已收盘 K 线

                # 回踩信号只对「多头候选」（ST多头 + EMA快慢 + VWAP 三条件）触发
                if not _is_long_candidate(data):
                    continue

                # ---- 入场信号：读 Pine 指标的确认信号（st_c.pierce_signal，所见即所得）----
                sig = st_c.get("pierce_signal")
                if sig == 1:
                    side = "long"
                    btime = bar_c.get("time") or bar.get("time") or 0
                    cur = _entry_state.get(symbol)
                    if btime > 0 and (cur is None or cur.get("bar_time") != btime):
                        close = bar_c.get("close") or bar.get("close")
                        low = bar_c.get("low")
                        high = bar_c.get("high")
                        stop_px = low if side == "long" else high
                        _entry_state[symbol] = {
                            "symbol": symbol,
                            "ticker": symbol.split(":")[-1],
                            "side": side,
                            "bar_time": btime,
                            "stage": "confirmed",
                            "close": round(close, 4) if close else 0,
                            "limit_px": round(close, 4) if close else 0,
                            "stop_px": round(stop_px, 4) if stop_px else 0,
                            "triggered_at": time.time(),
                        }
                        logger.info("ST 回踩入场信号: %s %s close=%s stop=%s",
                                    symbol, side, close, stop_px)
                        record_event("entry_signal", symbol=symbol,
                                     ticker=symbol.split(":")[-1],
                                     side=side, close=close, stop_px=stop_px)

                # ---- 预警信号（代码：实时 K 线 + 确认指标，仅提醒）----
                alert_side = _check_signal(bar, st_c)
                if alert_side and (config.ALLOW_SHORT or alert_side == "long"):
                    btime = bar.get("time") or 0
                    cur = _alert_state.get(symbol)
                    if btime > 0 and (cur is None or cur.get("bar_time") != btime):
                        _alert_state[symbol] = {
                            "symbol": symbol,
                            "ticker": symbol.split(":")[-1],
                            "side": alert_side,
                            "bar_time": btime,
                            "triggered_at": time.time(),
                        }
                        logger.info("ST 回踩预警: %s %s（注意观察）",
                                    symbol, alert_side)
                        record_event("alert_signal", symbol=symbol,
                                     ticker=symbol.split(":")[-1],
                                     side=alert_side)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.exception("回踩信号检测异常: %s", e)


def record_event(event_type: str, **data) -> None:
    """追加一条事件到当日 JSONL 日志（持久化，可回溯分析）。"""
    import json as _json
    try:
        day = time.strftime("%Y%m%d")
        path = Path(__file__).resolve().parent.parent / "logs" / f"events-{day}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        rec = {"ts": time.time(), "type": event_type, **data}
        with open(path, "a", encoding="utf-8") as f:
            f.write(_json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception as e:
        logger.warning("事件记录失败: %s", e)


def _snapshot_for(symbol: str) -> dict:
    """下单时抓取该标的的指标快照（复盘用）。"""
    snap = (collector.latest() or {}).get(symbol, {})
    return {
        "strength": snap.get("strength") or {},
        "filter": snap.get("filter") or {},
        "close": (snap.get("bar") or {}).get("close"),
        "st_c": snap.get("st_c") or snap.get("st") or {},   # 确认 ST（策略用）
    }


def compute_screen(min_bars: int = 5) -> dict:
    """横截面选股：按 VWAP + 快慢线过滤分类，再按趋势强度（推动 ATR 数）排序。"""
    market = collector.latest()
    rows = []
    for symbol, data in market.items():
        sg = data.get("strength") or {}
        # 策略用「确认值」（上一根已收盘），稳定不抖
        st = data.get("st_c") or data.get("st") or {}
        flt = data.get("filter") or {}
        bar = data.get("bar") or {}
        bars = sg.get("bars")
        move_atr = sg.get("move_atr")
        close = bar.get("close")
        if bars is None or move_atr is None or bars < min_bars or close is None:
            continue
        side = ("long" if st.get("up_trend") is not None
                else ("short" if st.get("down_trend") is not None else None))
        if side is None:
            continue
        # 归一化趋势强度符号：多头恒为正、空头恒为负（st_strength 与 st_channel_www 的 ST 参数可能不一致，按方向统一符号）
        move_atr = abs(move_atr) if side == "long" else -abs(move_atr)
        # 过滤：直接用 langfilter 指标的 signalOutput（Filter_Output_10）
        qualified = (flt.get("long_candidate") == 1)
        # ST 回踩判断：价格触及通道边沿（多头跌破上边 / 空头涨破下边）
        channel = (st.get("up_channel") if side == "long"
                   else st.get("down_channel"))
        if channel is not None:
            pullback = (close <= channel) if side == "long" else (close >= channel)
        else:
            pullback = False
        rows.append({
            "symbol": symbol,
            "ticker": symbol.split(":")[-1],
            "side": side,
            "bars": round(bars),
            "move_atr": round(move_atr, 2),
            "close": round(close, 2),
            "qualified": qualified,    # 是否在过滤范围内（绿/灰背景）
            "pullback": pullback,      # 是否触发 ST 回踩（闪烁）
        })
    # 多头列表：只按 langfilter 指标信号（qualified = long_candidate==1），组内按趋势强度排序
    long_rows = sorted([r for r in rows if r["qualified"]],
                       key=lambda x: -abs(x["move_atr"]))
    short_rows = (sorted([r for r in rows if r["side"] == "short" and r["qualified"]],
                         key=lambda x: x["move_atr"]) if config.ALLOW_SHORT else [])
    # 回踩事件队列：必须符合多头过滤（qualified = long_candidate==1），按趋势强度排序
    pullbacks = sorted([r for r in rows if r["pullback"] and r["qualified"]],
                       key=lambda x: abs(x["move_atr"]), reverse=True)
    return {"long": long_rows, "short": short_rows, "pullbacks": pullbacks}


async def build_snapshot() -> dict:
    status = await get_status_cached()
    open_symbols = len({l["symbol"] for l in order_mgr.list_lots(active_only=True)})
    return {
        "type": "snapshot",
        "ts": time.time(),
        "status": {
            "execution_enabled": bool(gateway.enabled),
            "connected": bool(gateway._connected),
            "dry_run": not bool(gateway.enabled),
            "equity": status["equity"],
            "daily_pnl": status["daily_pnl"],
            "halted": risk_gate.is_halted(),
            "open_symbols": open_symbols,
            "max_order_qty": config.RISK_MAX_ORDER_QTY,  # 单笔股数硬顶（前端封顶用）
            "allow_short": config.ALLOW_SHORT,  # 是否允许做空
            "market_age": (None if collector._last_update == 0
                           else round(collector.age(), 1)),
            "market_running": collector.running,
            "data_time": collector.latest_bar_time,   # TV 数据时钟（最新 K 线时间）
            "server_time": time.time(),               # 本地时钟
            "ibkr_time": gateway.ibkr_time,           # IBKR 服务器时钟
            "bar_update_delay": collector.bar_update_delay,  # K线收盘→收到 的延时（秒）
        },
        "market": collector.latest(),
        "positions": order_mgr.aggregate(),
        "lots": order_mgr.list_lots(),
        "logs": list(_log_buffer),
        "screen": compute_screen(5),
        "stab_events": sorted(_entry_state.values(),
                              key=lambda x: x.get("triggered_at", 0), reverse=True),
        "alert_events": sorted(_alert_state.values(),
                               key=lambda x: x.get("triggered_at", 0), reverse=True),
    }


async def _broadcast_loop() -> None:
    """每 1s 广播一次快照（含 TV 实时数据）。"""
    while True:
        try:
            snapshot = await build_snapshot()
            dead = []
            for ws in list(_ws_clients):
                try:
                    await ws.send_json(snapshot)
                except Exception:
                    dead.append(ws)
            for ws in dead:
                _ws_clients.discard(ws)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.exception("广播循环异常: %s", e)
        await asyncio.sleep(1.0)


# ---------------------------------------------------------------------------
# 生命周期
# ---------------------------------------------------------------------------

async def _eod_force_close_loop() -> None:
    """15:45 美东时间强制平掉所有活跃仓位（日内不过夜）。"""
    from zoneinfo import ZoneInfo
    import datetime as _dt
    et = ZoneInfo("America/New_York")
    done_days: set = set()
    while True:
        await asyncio.sleep(30.0)
        try:
            now_et = _dt.datetime.now(et)
            if now_et.hour == 15 and now_et.minute >= 45 and now_et.minute < 59:
                day_key = now_et.strftime("%Y%m%d")
                if day_key in done_days:
                    continue
                done_days.add(day_key)
                active = order_mgr.list_lots(active_only=True)
                logger.warning("EOD 强平触发：%d 个活跃仓位", len(active))
                for lot in active:
                    ok, err = await order_mgr.close_lot(lot["lot_id"])
                    logger.info("EOD 强平 %s: %s", lot["lot_id"],
                                "成功" if ok else f"失败({err})")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.exception("EOD 强平循环异常: %s", e)


@app.on_event("startup")
async def on_startup() -> None:
    global _broadcast_task, _eod_task, _signal_task
    connected = await gateway.connect()
    if config.EXECUTION_ENABLED and not connected:
        logger.error("EXECUTION_ENABLED=1 但 IBKR 未连接，下单将失败")
    elif not connected:
        logger.info("IBKR 未连接，运行于纯 dry-run（虚拟成交）")
    await collector.start()
    _broadcast_task = asyncio.create_task(_broadcast_loop())
    _eod_task = asyncio.create_task(_eod_force_close_loop())
    _signal_task = asyncio.create_task(_signal_detector_loop())


@app.on_event("shutdown")
async def on_shutdown() -> None:
    if _broadcast_task:
        _broadcast_task.cancel()
    if _eod_task:
        _eod_task.cancel()
    if _signal_task:
        _signal_task.cancel()
    await collector.stop()
    await gateway.disconnect()


# ---------------------------------------------------------------------------
# REST 接口
# ---------------------------------------------------------------------------

@app.get("/api/status")
async def api_status() -> dict:
    return (await build_snapshot())["status"]


@app.get("/api/logs")
async def api_logs(limit: int = 200) -> list:
    """返回最近 N 条日志（内存环形缓冲区）。"""
    logs = list(_log_buffer)
    return logs[-limit:]


@app.get("/api/market")
async def api_market() -> dict:
    """TV 实时数据快照（1M K线/EMA7 + 5M st_channel）。"""
    return {"symbols": collector.latest(),
            "age": None if collector._last_update == 0 else round(collector.age(), 1),
            "running": collector.running}


@app.get("/api/screen")
async def api_screen(min_bars: int = 5) -> dict:
    """横截面选股：按趋势强度（推动 ATR 数）排序，返回多头/空头候选。"""
    return compute_screen(min_bars)


@app.get("/api/stab/events")
async def api_stab_events() -> list:
    """当前入场信号（稳定 K线触发，按时间倒序）。"""
    evs = sorted(_entry_state.values(),
                 key=lambda x: x.get("triggered_at", 0), reverse=True)
    return evs


@app.post("/api/signal/clear")
async def api_signal_clear() -> dict:
    """一键清除所有交易信号（入场 + 预警）。"""
    n_entry = len(_entry_state)
    n_alert = len(_alert_state)
    _entry_state.clear()
    _alert_state.clear()
    record_event("signals_cleared", entry=n_entry, alert=n_alert)
    return {"ok": True, "cleared": n_entry + n_alert}


@app.post("/api/stab/confirm")
async def api_stab_confirm(symbol: str, risk_usd: float = 300) -> dict:
    """用户确认下单：限价=收盘价，止损=低点(多)/高点(空)。"""
    st = _entry_state.get(symbol)
    if st is None or st.get("stage") != "confirmed":
        raise HTTPException(400, "该回踩信号不可下单")
    if not config.ALLOW_SHORT and st.get("side") == "short":
        raise HTTPException(400, "当前仅允许做多（空头已屏蔽）")
    # 非多头列表拦截：long_candidate != 1 不允许下单
    _snap = (collector.latest() or {}).get(symbol, {})
    if (_snap.get("filter") or {}).get("long_candidate") != 1:
        raise HTTPException(400, f"{symbol} 不在多头列表中，拦截下单")
    equity = await gateway.net_liquidation()
    qty = risk_gate.size_position(st["limit_px"], st["stop_px"], equity)
    if qty <= 0:
        raise HTTPException(400, "无法确定有效股数")
    lot, err = await order_mgr.place_limit_stop_lot(
        symbol=symbol, side=st["side"], qty=qty,
        limit_px=st["limit_px"], stop_px=st["stop_px"],
        limit_type="st_pierce", reason="ST回踩长下影线",
        risk_usd=risk_usd, signal_snapshot=_snapshot_for(symbol))
    if lot is None:
        raise HTTPException(400, err)
    st["stage"] = "ordered"
    st["lot_id"] = lot.lot_id
    record_event("order_placed", lot_id=lot.lot_id, symbol=symbol,
                 side=st["side"], qty=qty, limit_px=st["limit_px"],
                 stop_px=st["stop_px"], limit_type="st_pierce",
                 risk_usd=risk_usd)
    return {"ok": True, "lot": lot.to_dict()}


@app.get("/api/performance")
async def api_performance() -> dict:
    """复盘统计：从历史归档（logs/trades-*.json）读取平仓记录。"""
    import glob
    import json as _json
    logs_dir = Path(__file__).resolve().parent.parent / "logs"
    records = []
    for f in sorted(glob.glob(str(logs_dir / "trades-*.json"))):
        try:
            records.extend(_json.loads(open(f, encoding="utf-8").read()))
        except Exception:
            continue
    closed = [r for r in records if r.get("state") == "CLOSED"]

    def _stats(rows) -> dict:
        if not rows:
            return {"count": 0, "win_rate": 0, "avg_win": 0, "avg_loss": 0,
                    "profit_factor": None, "total_pnl": 0,
                    "avg_mae": 0, "avg_mfe": 0, "avg_target_rr": 0,
                    "avg_actual_rr": 0, "expectancy": 0}
        wins = [r for r in rows if r.get("realized_pnl", 0) > 0]
        losses = [r for r in rows if r.get("realized_pnl", 0) <= 0]
        total = sum(r.get("realized_pnl", 0) for r in rows)
        avg_win = sum(r["realized_pnl"] for r in wins) / len(wins) if wins else 0.0
        avg_loss = sum(r["realized_pnl"] for r in losses) / len(losses) if losses else 0.0
        pf = round(abs(avg_win / avg_loss), 2) if avg_loss != 0 else None
        avg_mae = sum(r.get("mae", 0) for r in rows) / len(rows)
        avg_mfe = sum(r.get("mfe", 0) for r in rows) / len(rows)
        avg_target_rr = sum(r.get("target_rr", 0) for r in rows) / len(rows)
        avg_actual_rr = sum(r.get("actual_rr", 0) for r in rows) / len(rows)
        return {
            "count": len(rows),
            "win_rate": round(len(wins) / len(rows) * 100, 1),
            "avg_win": round(avg_win, 2),
            "avg_loss": round(avg_loss, 2),
            "profit_factor": pf,
            "total_pnl": round(total, 2),
            "expectancy": round(total / len(rows), 2),       # 每笔期望盈亏
            "avg_mae": round(avg_mae, 2),                    # 平均最大浮亏
            "avg_mfe": round(avg_mfe, 2),                    # 平均最大浮盈
            "avg_target_rr": round(avg_target_rr, 2),        # 平均目标 RR
            "avg_actual_rr": round(avg_actual_rr, 2),        # 平均真实 RR
        }

    def _group(key) -> dict:
        out = {}
        for r in closed:
            k = r.get(key) or "未知"
            out.setdefault(k, []).append(r)
        return {k: _stats(v) for k, v in out.items()}

    return {
        "count": len(closed),
        "overall": _stats(closed),
        "by_type": _group("limit_type"),
        "by_side": _group("side"),
        "by_symbol": _group("symbol"),
        "records": closed,
    }


@app.get("/api/events")
async def api_events(event_type: Optional[str] = None, limit: int = 500) -> list:
    """查询历史事件（JSONL 持久日志，可回溯分析）。"""
    import glob
    import json as _json
    logs_dir = Path(__file__).resolve().parent.parent / "logs"
    records = []
    for f in sorted(glob.glob(str(logs_dir / "events-*.jsonl")), reverse=True):
        try:
            with open(f, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    r = _json.loads(line)
                    if event_type is None or r.get("type") == event_type:
                        records.append(r)
        except Exception:
            continue
    records.sort(key=lambda x: x.get("ts", 0), reverse=True)
    return records[:limit]


@app.get("/api/lots")
async def api_lots(active_only: bool = False, symbol: Optional[str] = None) -> List[dict]:
    return order_mgr.list_lots(active_only=active_only, symbol=symbol)


@app.get("/api/positions")
async def api_positions() -> List[dict]:
    return order_mgr.aggregate()


@app.post("/api/lots")
async def api_place_lot(req: PlaceLotRequest) -> dict:
    """分批建仓：每次调用生成一个独立的 LMT + TRAIL lot。"""
    qty = req.qty
    if qty == 0:
        # 按净值自动算股数（以损定量）
        equity = await gateway.net_liquidation()
        qty = risk_gate.size_position(req.entry_px, req.stop_px, equity)
    if qty <= 0:
        raise HTTPException(400, "无法确定有效股数（请显式填写 qty）")

    lot, err = await order_mgr.place_lot(
        symbol=req.symbol, side=req.side, qty=qty,
        entry_px=req.entry_px, stop_px=req.stop_px,
        trail_amount=req.trail_amount, reason=req.reason)
    if lot is None:
        raise HTTPException(400, err)
    return {"ok": True, "lot": lot.to_dict()}


@app.post("/api/lots/limit-stop")
async def api_place_limit_stop(req: LimitStopOrderRequest) -> dict:
    """限价单（父）+ 固定 STOP 止损（子）下单。"""
    if not config.ALLOW_SHORT and req.side == "short":
        raise HTTPException(400, "当前仅允许做多（空头已屏蔽）")
    # 非多头列表拦截：long_candidate != 1 不允许下单
    _snap = (collector.latest() or {}).get(req.symbol, {})
    if (_snap.get("filter") or {}).get("long_candidate") != 1:
        raise HTTPException(400, f"{req.symbol} 不在多头列表中，拦截下单")
    qty = req.qty
    if qty == 0:
        equity = await gateway.net_liquidation()
        qty = risk_gate.size_position(req.limit_px, req.stop_px, equity)
    if qty <= 0:
        raise HTTPException(400, "无法确定有效股数（请显式填写 qty）")

    lot, err = await order_mgr.place_limit_stop_lot(
        symbol=req.symbol, side=req.side, qty=qty,
        limit_px=req.limit_px, stop_px=req.stop_px,
        limit_type=req.limit_type, reason=req.reason,
        risk_usd=req.risk_usd,
        signal_snapshot=_snapshot_for(symbol=req.symbol))
    if lot is None:
        raise HTTPException(400, err)
    record_event("order_placed", lot_id=lot.lot_id, symbol=req.symbol,
                 side=req.side, qty=qty, limit_px=req.limit_px,
                 stop_px=req.stop_px, limit_type=req.limit_type,
                 risk_usd=req.risk_usd)
    return {"ok": True, "lot": lot.to_dict()}


@app.post("/api/tv/calc")
async def api_tv_calc(req: TvCalcRequest) -> dict:
    """从 TV 拉数据，计算各标的的下单参数（方向/止损/EMA限价/channel限价）。"""
    from engine.cloud_collector import CloudDataCollector
    from engine.order_calc import compute_order_params

    async with CloudDataCollector(req.symbols, timeframe="5", bars=20) as c:
        snapshot = await c.collect()
    results = []
    for sym, data in snapshot.items():
        p = compute_order_params(data.extra, sym)
        if p:
            results.append({**p, "close": round(data.close, 2)})
    return {"ok": True, "orders": results}


@app.post("/api/lots/{lot_id}/trail")
async def api_update_trail(lot_id: str, req: UpdateTrailRequest) -> dict:
    ok, err = await order_mgr.update_trail(lot_id, req.stop_px)
    if not ok:
        raise HTTPException(400, err)
    return {"ok": True}


@app.post("/api/lots/{lot_id}/cancel")
async def api_cancel_lot(lot_id: str) -> dict:
    ok, err = await order_mgr.cancel_lot(lot_id)
    record_event("order_cancelled" if ok else "order_cancel_failed",
                 lot_id=lot_id, error=(err if not ok else ""))
    if not ok:
        raise HTTPException(400, err)
    return {"ok": True}


@app.post("/api/lots/{lot_id}/close")
async def api_close_lot(lot_id: str) -> dict:
    """平仓：撤 STOP 保护单 + 反向 marketable 平仓单。"""
    ok, err = await order_mgr.close_lot(lot_id)
    record_event("order_closed" if ok else "order_close_failed",
                 lot_id=lot_id, error=(err if not ok else ""))
    if not ok:
        raise HTTPException(400, err)
    return {"ok": True}


@app.post("/api/refresh")
async def api_refresh() -> dict:
    await order_mgr.refresh_marks()
    return await build_snapshot()


# ---------------------------------------------------------------------------
# WebSocket
# ---------------------------------------------------------------------------

@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket) -> None:
    await ws.accept()
    _ws_clients.add(ws)
    try:
        await ws.send_json(await build_snapshot())
        while True:
            # 只收不发（客户端心跳/未来指令可在此处理）
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        _ws_clients.discard(ws)


# ---------------------------------------------------------------------------
# 静态前端
# ---------------------------------------------------------------------------

@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.middleware("http")
async def _no_cache_static(request, call_next):
    """静态文件禁止缓存，避免前端改动后浏览器仍用旧版本。"""
    response = await call_next(request)
    if request.url.path.startswith("/static"):
        response.headers["Cache-Control"] = "no-cache"
    return response
