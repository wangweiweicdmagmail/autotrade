# -*- coding: utf-8 -*-
"""
下单控制台后端 (FastAPI)
=========================

提供给 Web 控制页（console/static）的 REST + WebSocket 接口：

- REST：状态 / 下单（LMT + STP 括号单，双向）/ 订单列表 / 聚合仓位 /
  改止损 / 撤单 / 平仓 / 选股 / 绩效 / 事件 / 刷新。
- WebSocket /ws：每 1s 推送一次快照（状态 + 行情 + 仓位 + 全部 lot），
  前端据此实时刷新，无需轮询。

策略体系（2026-10-17 v3）：5M 周期 + 单指标 trend_state。
过滤 = ts_c.trend_state（1 单边多头 / -1 单边空头 / 0 震荡拦截）；
下单方向 = ST 方向（无反向订单）；入场 = ema_fast / channel_edge 两条
回踩限价线；止损 = 限价 ∓ 1×ATR（STP 子单，唯一止损形态）。

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
from engine.auto_engine import LiveBroker, StrategyEngine
from engine.realtime_collector import RealtimeCollector
from engine.shadow_tracker import ShadowTracker
from engine.strategies import build_strategy
from execution.ibkr import IBKRGateway
from execution.order_lot import OrderManager
from execution.risk import RiskConfig, RiskGate
from execution.writer_lock import acquire_writer_lock

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


class UpdateTrailRequest(BaseModel):
    stop_px: float = Field(..., gt=0)


class LimitStopOrderRequest(BaseModel):
    symbol: str = Field(..., description="如 NVDA / NASDAQ:NVDA")
    side: str = "long"
    qty: int = 0                      # 0 = 按净值自动算股数
    limit_px: float = Field(..., gt=0)
    stop_px: float = Field(..., gt=0)
    limit_type: str = "ema"           # ema / channel / midpoint（5M 高低价中点）
    reason: str = ""
    risk_usd: float = 0               # 设定风险金额（风控偏差 10% 容差检查）
    force: bool = False               # 执意开仓：二次确认后放行，跳过 trend_state 过滤拦截


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
_reconcile_task: Optional[asyncio.Task] = None
_midpoint_reprice_task: Optional[asyncio.Task] = None
_external_cleanup_task: Optional[asyncio.Task] = None
_midpoint_data_warned: set = set()

# TV 实时订阅采集器（5M K线 + trend_state 单指标）
collector: RealtimeCollector = RealtimeCollector(config.WATCHLIST)

# 净值/日盈亏缓存（避免每秒查 IBKR）
_equity_cache = {"equity": None, "daily_pnl": None, "ts": 0.0}

# 持仓扫描状态（持仓方向与 trend_state 方向冲突 → 自动平仓/平仓提醒）
_scan_alerts: dict = {}   # ticker -> 平仓提醒（自动平仓失败或开关关闭时）
_scan_task: Optional[asyncio.Task] = None

# 止损单守卫状态（持仓缺少 STP 保护单 → 每分钟提醒）
_stop_alerts: dict = {}   # ticker -> 裸仓提醒
_stop_guard_task: Optional[asyncio.Task] = None

# 趋势反向自动平仓记录（前端语音/通知用，ticker -> 最近一次自动平仓）
_auto_close_notices: dict = {}

# 影子回测跟踪器（回踩事件 → 逐 bar 回测推演 → 信号有效性统计）
shadow_tracker: ShadowTracker = ShadowTracker()
_shadow_task: Optional[asyncio.Task] = None

# 策略引擎（开仓候选人工确认 + bar 收盘自动管理）
auto_engine: StrategyEngine = StrategyEngine(
    LiveBroker(order_mgr), risk_gate=risk_gate, gateway=gateway,
    emit=lambda t, **d: record_event(f"auto_{t}" if not t.startswith("auto_") else t, **d),
    max_instances=config.AUTO_MAX_INSTANCES,
    require_entry_confirmation=True)
_auto_enabled: bool = False        # /api/auto/start 开启，stop 关闭
_auto_task: Optional[asyncio.Task] = None
_auto_last_bar: dict = {}          # symbol -> 已处理的 bar_c.time
_auto_strategy_name: str = ""


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


def _ts_c(data: dict) -> dict:
    """trend_state 确认值（上一根已收盘 5M bar）；缺失时退化实时值。"""
    return data.get("ts_c") or data.get("ts") or {}


def _midpoint_prices(data: dict, side: str):
    """由上一根已收盘 5M bar 计算中点限价与 1×ATR 止损。"""
    bar = data.get("bar_c") or {}
    ts = _ts_c(data)
    high, low = bar.get("high"), bar.get("low")
    atr = ts.get("atr")
    if high is None or low is None or not bar.get("time"):
        raise ValueError("缺少已收盘 5M K 线，无法计算中点限价")
    if high < low:
        raise ValueError("5M K 线 high/low 数据非法")
    if not atr or atr <= 0:
        raise ValueError("ATR 数据缺失，无法计算中点单止损")
    limit = round((high + low) / 2, 2)
    stop = round(limit + (-1 if side == "long" else 1) * atr, 2)
    if limit <= 0 or stop <= 0:
        raise ValueError("中点限价/止损必须为正")
    if side == "long" and stop >= limit:
        raise ValueError("多仓止损必须低于中点限价")
    if side == "short" and stop <= limit:
        raise ValueError("空仓止损必须高于中点限价")
    return limit, stop, bar, float(atr)


def _check_trend_filter(symbol: str, side: str,
                        force: bool = False, reason: str = "") -> None:
    """trend_state 过滤闸门（下单拦截）：
    做多需 ts_c.trend_state == 1，做空需 == -1，震荡(0)/无数据拦截；
    side 必须与 trend_state 同向（下单方向 = ST 方向，无反向订单）。
    force=执意开仓放行（需填原因，记 WARN 日志）。"""
    if force:
        logger.warning("执意开仓放行（force）: %s %s 原因: %s",
                       symbol, side, reason)
        return
    snap = (collector.latest() or {}).get(symbol, {})
    ts = _ts_c(snap)
    state = ts.get("trend_state")
    if ts.get("atr") is None:
        raise HTTPException(400, f"{symbol} ATR 数据缺失，"
                                 "请检查图表上的 trend_state 指标（Atr Value plot）")
    if side == "long" and state != 1:
        raise HTTPException(400, {"code": "not_long_candidate",
                                  "msg": f"{symbol} 不在多头列表"
                                         f"（trend_state={state}）"})
    if side == "short" and state != -1:
        raise HTTPException(400, {"code": "not_long_candidate",
                                  "msg": f"{symbol} 不在空头列表"
                                         f"（trend_state={state}）"})


async def _close_on_flip(ticker: str, sym: str, pos: float,
                         close_px: Optional[float]) -> bool:
    """趋势反向时只处理本工具 lot；外部持仓由独立清理循环处理。"""
    ok = True
    for lot in order_mgr.list_lots(active_only=True, symbol=sym):
        if lot["state"] == "PENDING":
            lok, err = await order_mgr.cancel_lot(lot["lot_id"])
        elif lot["state"] == "ACTIVE":
            lok, err = await order_mgr.close_lot(
                lot["lot_id"], ref_px=close_px, exit_reason="st_flip")
        else:
            continue   # CLOSING 等由各自 close watcher 完成，不重复发单
        if not lok:
            ok = False
            logger.warning("趋势反向平仓 %s lot %s 失败: %s",
                           ticker, lot["lot_id"], err)
    return ok


_external_cleanup_inflight: dict = {}


def _legacy_tool_order_ids() -> set:
    ids = set()
    for lot in order_mgr.list_lots():
        values = list(lot.get("owned_order_ids") or [])
        values.extend(lot.get(key) for key in (
            "entry_order_id", "trail_order_id", "close_order_id", "close_permId"))
        for value in values:
            if value is not None:
                try:
                    ids.add(int(value))
                except (TypeError, ValueError):
                    continue
    return ids


def _external_symbol(ticker: str) -> Optional[str]:
    for symbol in (collector.latest() or {}):
        if symbol.split(":")[-1].upper() == ticker.upper():
            return symbol
    return None


def _external_account_ok(order, accounts: set) -> bool:
    account = getattr(order, "account", "") or ""
    fa_group = getattr(order, "faGroup", "") or ""
    if fa_group == gateway.fa_group:
        return True
    return bool(account and account in accounts)


async def _wait_trade_terminal(trade, timeout: float = 8.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if trade.isDone():
            return True
        await asyncio.sleep(0.25)
    return trade.isDone()


async def _external_cleanup_once() -> dict:
    """清理所有号段外订单/持仓；快照不完整时禁止任何写操作。"""
    stats = {"cancelled": 0, "closed": 0, "pending": 0,
             "failed": 0, "skipped": 0}
    if not (config.EXTERNAL_ORDER_CLEANUP_ENABLED
            and gateway.enabled and gateway._connected):
        return stats
    positions, trades = await gateway.positions(), await gateway.trade_map()
    if positions is None or trades is None:
        logger.warning("外部清理跳过：IBKR 订单或持仓快照不完整")
        stats["failed"] += 1
        return stats
    accounts = set(await gateway.fa_accounts())
    legacy_ids = _legacy_tool_order_ids()

    for trade in list(trades.values()):
        if trade.isDone():
            continue
        order = trade.order
        order_id = int(order.orderId)
        if gateway.is_tool_order_id(order_id) or order_id in legacy_ids:
            continue
        if not _external_account_ok(order, accounts):
            stats["skipped"] += 1
            logger.warning("外部挂单账户无法确认，跳过 orderId=%s", order_id)
            continue
        ok = gateway.cancel_order(trade)
        if not ok:
            stats["failed"] += 1
            record_event("external_order_cancel_failed", order_id=order_id,
                         error="cancel_order 调用失败")
            continue
        await _wait_trade_terminal(trade, timeout=5.0)
        status = trade.orderStatus.status
        if trade.isDone() and status != "Filled":
            stats["cancelled"] += 1
            record_event("external_order_cancelled", order_id=order_id,
                         symbol=trade.contract.symbol, status=status)
        else:
            stats["failed"] += 1
            record_event("external_order_cancel_failed", order_id=order_id,
                         symbol=trade.contract.symbol, status=status)

    positions = await gateway.positions()
    if positions is None:
        logger.warning("外部持仓清理跳过：撤单后持仓快照不可用")
        stats["failed"] += 1
        return stats
    tool_qty = {g["symbol"]: g["net_qty"] for g in order_mgr.aggregate()}
    for ticker in sorted(set(positions) | set(tool_qty)):
        residual = positions.get(ticker, 0) - tool_qty.get(ticker, 0)
        if abs(residual) < 1:
            continue
        existing = _external_cleanup_inflight.get(ticker)
        if existing is not None and not existing.isDone():
            stats["pending"] += 1
            continue
        _external_cleanup_inflight.pop(ticker, None)
        registered = next((t for t in trades.values()
                           if getattr(t.order, "orderRef", "") == "external_cleanup"
                           and t.contract.symbol == ticker and not t.isDone()), None)
        if registered is not None:
            _external_cleanup_inflight[ticker] = registered
            stats["pending"] += 1
            continue
        symbol = _external_symbol(ticker)
        if symbol is None:
            stats["failed"] += 1
            record_event("external_position_close_failed", ticker=ticker,
                         qty=abs(int(residual)), error="标的不在订阅列表")
            continue
        side = "long" if residual > 0 else "short"
        qty = abs(int(residual))
        ref_px = await gateway.last_price(symbol)
        if ref_px is None:
            stats["failed"] += 1
            record_event("external_position_close_failed", symbol=symbol,
                         qty=qty, error="无法获取现价")
            continue
        result = await gateway.place_close(symbol, side, qty, ref_px,
                                           order_ref="external_cleanup")
        if not result.get("ok") or result.get("trade") is None:
            stats["failed"] += 1
            record_event("external_position_close_failed", symbol=symbol,
                         qty=qty, error=result.get("error") or "平仓提交失败")
            continue
        trade = result["trade"]
        _external_cleanup_inflight[ticker] = trade
        done = await _wait_trade_terminal(trade)
        status = trade.orderStatus.status
        if done and status == "Filled":
            stats["closed"] += 1
            _external_cleanup_inflight.pop(ticker, None)
            record_event("external_position_closed", symbol=symbol, side=side,
                         qty=qty, order_id=result.get("order_id"),
                         avg_fill=trade.orderStatus.avgFillPrice)
        elif done:
            stats["failed"] += 1
            _external_cleanup_inflight.pop(ticker, None)
            record_event("external_position_close_failed", symbol=symbol,
                         qty=qty, status=status)
        else:
            stats["pending"] += 1
            record_event("external_position_close_pending", symbol=symbol,
                         qty=qty, order_id=result.get("order_id"), status=status)
    return stats


async def _external_cleanup_loop() -> None:
    while True:
        await asyncio.sleep(30.0)
        try:
            await _external_cleanup_once()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.exception("外部订单清理异常: %s", e)


async def _position_scan_loop() -> None:
    """持仓扫描（每分钟，双向）：持仓方向与 trend_state 确认值（ts_c，上一根
    已收盘 5M bar，不看盘中实时值）冲突时——多仓见 direction==-1、空仓见
    direction==1——实盘自动平仓；平仓失败或开关关闭时每分钟弹窗提醒人工处理。"""
    while True:
        await asyncio.sleep(60.0)
        try:
            if not _is_trading_hours():
                continue
            positions = await gateway.positions()   # {ticker: qty}；None=快照不可用
            if positions is None:
                logger.warning("持仓扫描跳过：IBKR 持仓快照不可用")
                continue
            market = collector.latest()
            for ticker, pos in positions.items():
                if pos == 0:
                    continue
                side = "long" if pos > 0 else "short"
                # ticker（无交易所前缀）→ 完整 symbol
                sym = None
                for s, data in market.items():
                    if s.split(":")[-1] == ticker:
                        sym = s
                        break
                if sym is None:
                    continue
                data = market[sym]
                # 只用上一根已收盘 bar 的确认值（ts_c），不看盘中实时值（ts）
                ts_c = _ts_c(data)
                direction = ts_c.get("direction")
                flipped = ((pos > 0 and direction == -1)
                           or (pos < 0 and direction == 1))
                if not flipped:
                    continue   # 趋势未反向
                st_line = ts_c.get("supertrend")
                bar = data.get("bar") or {}
                close = bar.get("close")
                live = bool(gateway.enabled and gateway._connected)
                if config.AUTO_CLOSE_ON_ST_FLIP and live:
                    logger.warning("持仓扫描: %s %s %s 股趋势已反向(ST %.2f)，自动平仓",
                                   ticker, side, abs(pos), st_line or 0)
                    ok = await _close_on_flip(ticker, sym, pos, close)
                    record_event("auto_close_st_flip", symbol=sym, ticker=ticker,
                                 side=side, qty=abs(pos), st_line=st_line,
                                 close=close, ok=ok, status="requested")
                    if ok:
                        _scan_alerts.pop(ticker, None)
                        _auto_close_notices[ticker] = {
                            "symbol": sym, "ticker": ticker, "side": side,
                            "qty": abs(pos), "st_line": st_line, "close": close,
                            "closed_at": time.time(), "pending": True,
                        }
                        continue
                    # 自动平仓失败 → 落入提醒逻辑，下分钟重试
                _scan_alerts[ticker] = {
                    "symbol": sym,
                    "ticker": ticker,
                    "side": side,
                    "qty": abs(pos),
                    "st_line": st_line,
                    "close": close,
                    "auto_close_failed": bool(config.AUTO_CLOSE_ON_ST_FLIP and live),
                    "scanned_at": time.time(),
                }
                logger.info("持仓扫描: %s %s %s 股趋势已反向(ST %.2f)，提醒平仓",
                            ticker, side, abs(pos), st_line or 0)
                record_event("position_scan_alert", symbol=sym, ticker=ticker,
                             side=side, qty=abs(pos), st_line=st_line, close=close)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.exception("持仓扫描异常: %s", e)


async def _stop_guard_loop() -> None:
    """止损单守卫（每分钟）：FA 组每个持仓必须有足够的 STP/TRAIL 保护单覆盖，
    裸仓（保护单股数 < 持仓股数）每分钟提醒一次，直到补齐。只告警不自动补单。"""
    while True:
        await asyncio.sleep(60.0)
        try:
            if not config.STOP_GUARD_ENABLED or not gateway._connected:
                continue
            positions = await gateway.positions()
            if positions is None:
                logger.warning("止损守卫跳过：IBKR 持仓快照不可用")
                continue
            covers = await gateway.open_protective_qty()
            if covers is None:
                logger.warning("止损守卫跳过：IBKR 开放订单快照不可用")
                continue
            now = time.time()
            missing: Set[str] = set()
            for ticker, pos in positions.items():
                side = "long" if pos > 0 else "short"
                need = "sell" if pos > 0 else "buy"   # 平多要 SELL 保护单
                covered = (covers.get(ticker) or {}).get(need, 0.0)
                if covered >= abs(pos):
                    continue
                missing.add(ticker)
                prev = _stop_alerts.get(ticker)
                _stop_alerts[ticker] = {
                    "ticker": ticker,
                    "side": side,
                    "qty": abs(pos),
                    "covered_qty": covered,
                    "since": prev["since"] if prev else now,
                    "checked_at": now,
                }
                logger.warning("⚠️ 止损单缺失: %s %s %s 股，%s 保护单仅 %s 股"
                               "——裸仓，请立即补挂止损",
                               ticker, side, abs(pos), need.upper(), covered)
                record_event("missing_stop_alert", ticker=ticker, side=side,
                             qty=abs(pos), covered_qty=covered)
            # 已补齐/已平仓的标的移出提醒
            for t in list(_stop_alerts):
                if t not in missing:
                    _stop_alerts.pop(t, None)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.exception("止损单守卫异常: %s", e)


# ---------------------------------------------------------------------------
# 事件记录与快照辅助
# ---------------------------------------------------------------------------

async def _shadow_loop() -> None:
    """影子回测循环（每 1s）：新收盘 5M bar 喂入回测引擎——检测 ema_fast /
    channel_edge 回踩事件并逐 bar 推演，事件与结算写入当日事件流。"""
    while True:
        await asyncio.sleep(1.0)
        try:
            market = collector.latest()
            if not market:
                continue
            events, settled = shadow_tracker.process(market)
            for tr in events:
                record_event("pullback_event", shadow_id=tr.shadow_id,
                             symbol=tr.symbol, ticker=tr.ticker, side=tr.side,
                             line_type=tr.line_type, bar_time=tr.bar_time,
                             entry=tr.entry, stop=tr.stop, atr=tr.atr)
            for tr in settled:
                record_event("shadow_settled", shadow_id=tr.shadow_id,
                             symbol=tr.symbol, ticker=tr.ticker, side=tr.side,
                             line_type=tr.line_type, stopped=tr.stopped,
                             mfe_rr=tr.mfe_rr, mfe_rr_eod=tr.mfe_rr_eod,
                             outcome_rr=tr.outcome_rr)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.exception("影子回测循环异常: %s", e)


async def _auto_loop() -> None:
    """策略分发循环（每 1s）：
    - bar 收盘推进 → engine.on_bar（候选过期 + 跟踪/反向等持仓管理）
    - tick 价 → engine.on_price（WATCHING 产生候选；ACTIVE 达到 2R 立即保本）
    - 每 5s 收养人工 ACTIVE 仓（同一套保本/跟踪逻辑）
    """
    from zoneinfo import ZoneInfo
    import datetime as _dt
    et = ZoneInfo("America/New_York")
    adopt_tick = 0
    while True:
        await asyncio.sleep(1.0)
        try:
            if not _auto_enabled:
                continue
            market = collector.latest()
            if not market:
                continue
            now = _dt.datetime.now(et)
            now_hm = now.hour * 60 + now.minute
            trading = _is_trading_hours()
            for sym, data in market.items():
                bar_c = data.get("bar_c") or {}
                ts_c = data.get("ts_c") or data.get("ts") or {}
                bt = bar_c.get("time") or 0
                if bt and _auto_last_bar.get(sym) != bt:
                    _auto_last_bar[sym] = bt
                    try:
                        await auto_engine.on_bar(sym, bar_c, ts_c)
                    except Exception:
                        logger.exception("auto on_bar 异常 %s", sym)
                if trading:
                    price = (data.get("bar") or {}).get("close")
                    if price:
                        try:
                            await auto_engine.on_price(sym, price, ts_c,
                                                       bar_c, now_hm)
                        except Exception:
                            logger.exception("auto on_price 异常 %s", sym)
            adopt_tick += 1
            if adopt_tick >= 5:
                adopt_tick = 0
                try:
                    await auto_engine.adopt_manual_lots(
                        order_mgr.list_lots(active_only=True))
                except Exception:
                    logger.exception("auto 收养扫描异常")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.exception("全自动策略循环异常: %s", e)


async def _midpoint_limit_reprice_loop() -> None:
    """手动中点限价单：每根新确认 5M bar 同步修改 LMT 父单与 STP 子单。"""
    while True:
        await asyncio.sleep(1.0)
        try:
            pending = [l for l in order_mgr.list_lots(active_only=True)
                       if l["state"] == "PENDING"
                       and l.get("limit_type") == "midpoint"]
            if not pending:
                continue
            market = collector.latest() or {}
            for lot in pending:
                data = market.get(lot["symbol"]) or {}
                bar = data.get("bar_c") or {}
                bt = bar.get("time") or 0
                if not bt or bt <= (lot.get("reprice_bar_time") or 0):
                    continue
                attempt_key = (lot["lot_id"], bt)
                if attempt_key in _midpoint_data_warned:
                    continue
                try:
                    limit, stop, bar, atr = _midpoint_prices(data, lot["side"])
                except ValueError as e:
                    _midpoint_data_warned.add(attempt_key)
                    logger.warning("%s 中点改价数据缺失: %s", lot["symbol"], e)
                    record_event("midpoint_limit_reprice_failed",
                                 lot_id=lot["lot_id"], symbol=lot["symbol"],
                                 bar_time=bt, error=str(e))
                    continue
                old_limit, old_stop = lot["entry_px"], lot["stop_px"]
                ok, error = await order_mgr.reprice_midpoint_lot(
                    lot["lot_id"], limit, stop, bt)
                event_type = ("midpoint_limit_repriced" if ok
                              else "midpoint_limit_reprice_failed")
                record_event(event_type, lot_id=lot["lot_id"],
                             symbol=lot["symbol"], side=lot["side"],
                             old_limit_px=old_limit, limit_px=limit,
                             old_stop_px=old_stop, stop_px=stop,
                             atr=atr, bar_time=bt, error=error)
                if not ok:
                    _midpoint_data_warned.add(attempt_key)
                    logger.warning("%s 中点改价失败: %s", lot["symbol"], error)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.exception("中点限价单改价循环异常: %s", e)


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
    """下单时抓取该标的的 trend_state 快照（复盘用）。"""
    snap = (collector.latest() or {}).get(symbol, {})
    return {
        "ts": snap.get("ts") or {},                    # 实时值（显示）
        "ts_c": snap.get("ts_c") or snap.get("ts") or {},   # 确认值（策略）
        "close": (snap.get("bar") or {}).get("close"),
    }


def compute_screen() -> dict:
    """横截面选股（双向）：trend_state==1 进多头列、==-1 进空头列（0 震荡不列）。
    趋势判断只有 trend_state（指标值，所见即所得）；发散度仅作列内
    强→弱排序展示，由指标 plot 值（ema_fast/ema_slow/atr）换算，不做判断。"""
    market = collector.latest()
    long_rows, short_rows = [], []
    for symbol, data in market.items():
        # 策略用「确认值」（上一根已收盘），稳定不抖
        ts = _ts_c(data)
        trend = ts.get("trend_state")
        close = (data.get("bar") or {}).get("close")
        if trend not in (1, -1) or close is None:
            continue
        divergence = ts.get("divergence")
        if divergence is None:
            ema_f, ema_s, atr = ts.get("ema_fast"), ts.get("ema_slow"), ts.get("atr")
            if ema_f is not None and ema_s is not None and atr and atr > 0:
                divergence = abs(ema_f - ema_s) / atr
        row = {
            "symbol": symbol,
            "ticker": symbol.split(":")[-1],
            "side": "long" if trend == 1 else "short",
            "trend_state": trend,
            "divergence": (round(abs(divergence), 2)
                           if divergence is not None else None),
            "st_duration_bars": ts.get("st_duration_bars"),
            "st_push_count": ts.get("st_push_count"),
            "close": round(close, 2),
            "qualified": True,     # 已过 trend_state 过滤（前端兼容字段）
        }
        (long_rows if trend == 1 else short_rows).append(row)
    long_rows.sort(key=lambda x: -(x["divergence"] or 0))
    short_rows.sort(key=lambda x: -(x["divergence"] or 0))
    return {"long": long_rows, "short": short_rows}


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
        "screen": compute_screen(),
        "scan_alerts": sorted(_scan_alerts.values(),
                              key=lambda x: x.get("scanned_at", 0), reverse=True),
        "stop_alerts": sorted(_stop_alerts.values(),
                              key=lambda x: x.get("checked_at", 0), reverse=True),
        "auto_close_notices": sorted(_auto_close_notices.values(),
                                     key=lambda x: x.get("closed_at", 0), reverse=True),
        "shadow": {"stats": shadow_tracker.stats(),
                   "events": shadow_tracker.events(100)},
        "auto": {"enabled": _auto_enabled, "strategy": _auto_strategy_name,
                 "instances": auto_engine.instances(),
                 "proposals": auto_engine.proposals()},
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
                active = order_mgr.list_lots(active_only=True)
                logger.warning("EOD 强平触发：%d 个活跃仓位", len(active))
                all_ok = True
                for lot in active:
                    if lot["state"] == "PENDING":
                        ok, err = await order_mgr.cancel_lot(lot["lot_id"])
                    elif lot["state"] == "ACTIVE":
                        ok, err = await order_mgr.close_lot(lot["lot_id"])
                    else:
                        continue   # CLOSING 由 close watcher 完成，不重复发单
                    if not ok:
                        all_ok = False
                    logger.info("EOD 处理 %s: %s", lot["lot_id"],
                                "成功" if ok else f"失败({err})")
                if all_ok:
                    done_days.add(day_key)   # 失败时保留下轮重试，已 CLOSING 的不会重复发单
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.exception("EOD 强平循环异常: %s", e)


async def _reconcile_loop() -> None:
    """每 60s 订单对账：重挂丢失的 watcher、补登系统外平仓记录
    （重启 / 断线重连 / TWS 手动操作后的自愈）。"""
    while True:
        await asyncio.sleep(60.0)
        try:
            await order_mgr.reconcile()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.exception("订单对账循环异常: %s", e)


def _on_lot_closed(d: dict) -> None:
    """保护单触发 / 对账补登的平仓事件：完整字段落事件流，供复盘。"""
    record_event("lot_closed", **{k: d.get(k) for k in (
        "lot_id", "symbol", "side", "qty", "filled_qty", "avg_fill",
        "exit_px", "realized_pnl", "actual_rr", "target_rr", "mae", "mfe",
        "exit_reason", "limit_type", "entry_time", "closed_at", "reason")})


@app.on_event("startup")
async def on_startup() -> None:
    global _broadcast_task, _eod_task, _scan_task, _stop_guard_task, _reconcile_task
    global _shadow_task, _auto_task, _midpoint_reprice_task, _external_cleanup_task
    config.validate_config()
    if not acquire_writer_lock("logs/autotrade_writer.lock"):
        raise RuntimeError("另一个 autotrade 执行写进程正在运行，禁止重复管理订单")
    connected = await gateway.connect()
    if config.EXECUTION_ENABLED and not connected:
        logger.error("EXECUTION_ENABLED=1 但 IBKR 未连接，下单将失败")
    elif not connected:
        logger.info("IBKR 未连接，运行于纯 dry-run（虚拟成交）")
    order_mgr.on_lot_closed = _on_lot_closed
    if connected:
        await order_mgr.reconcile()
    await collector.start()
    _broadcast_task = asyncio.create_task(_broadcast_loop())
    _eod_task = asyncio.create_task(_eod_force_close_loop())
    _scan_task = asyncio.create_task(_position_scan_loop())
    _stop_guard_task = asyncio.create_task(_stop_guard_loop())
    _reconcile_task = asyncio.create_task(_reconcile_loop())
    _shadow_task = asyncio.create_task(_shadow_loop())
    _auto_task = asyncio.create_task(_auto_loop())
    _midpoint_reprice_task = asyncio.create_task(_midpoint_limit_reprice_loop())
    _external_cleanup_task = asyncio.create_task(_external_cleanup_loop())


@app.on_event("shutdown")
async def on_shutdown() -> None:
    for t in (_broadcast_task, _eod_task, _scan_task,
              _stop_guard_task, _reconcile_task, _shadow_task, _auto_task,
              _midpoint_reprice_task, _external_cleanup_task):
        if t:
            t.cancel()
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
    """TV 实时数据快照（5M K线 + trend_state）。"""
    return {"symbols": collector.latest(),
            "age": None if collector._last_update == 0 else round(collector.age(), 1),
            "running": collector.running}


@app.get("/api/screen")
async def api_screen() -> dict:
    """横截面选股（双向）：trend_state 多空两列，按 EMA 发散度排序。"""
    return compute_screen()


@app.get("/api/shadow")
async def api_shadow() -> dict:
    """当日影子回测：统计 + 全部回踩事件（实时逐 bar 推演）。"""
    return {"stats": shadow_tracker.stats(),
            "events": shadow_tracker.events(200)}


class AutoStartRequest(BaseModel):
    strategy: str = "trend_pullback"
    symbols: List[str] = Field(default_factory=list,
                               description="空 = 全 WATCHLIST（受实例上限约束）")
    params: dict = Field(default_factory=dict, description="覆盖策略参数")


@app.post("/api/auto/start")
async def api_auto_start(req: AutoStartRequest) -> dict:
    """启动策略实例（≤ AUTO_MAX_INSTANCES）：tick 产生候选，人工确认开仓，
    bar 收盘自动管理（保本/ST 跟踪/反向）。"""
    global _auto_enabled, _auto_strategy_name, _auto_last_bar
    symbols = [s.strip().upper() for s in req.symbols if s.strip()]
    if not symbols:
        symbols = list(config.WATCHLIST)
    # 补交易所前缀（允许裸 ticker）
    fixed = []
    for s in symbols:
        if ":" not in s:
            match = [w for w in config.WATCHLIST if w.split(":")[-1] == s]
            fixed.append(match[0] if match else s)
        else:
            fixed.append(s)
    try:
        strategy = build_strategy(req.strategy, **req.params)
    except ValueError as e:
        raise HTTPException(400, str(e))
    started = auto_engine.start(strategy, fixed)
    _auto_enabled = True
    _auto_strategy_name = req.strategy
    record_event("auto_start", strategy=req.strategy, symbols=started,
                 params=strategy.params)
    logger.info("全自动策略启动 %s: %s（实例上限 %d）",
                req.strategy, started, config.AUTO_MAX_INSTANCES)
    return {"ok": True, "started": started, "params": strategy.params,
            "skipped": [s for s in fixed if s not in started]}


@app.post("/api/auto/stop")
async def api_auto_stop(close_positions: bool = False) -> dict:
    """停止全自动策略：不再开新仓；存量实例默认继续管理（可选全部平仓）。"""
    global _auto_enabled
    _auto_enabled = False
    stopped = []
    for inst in auto_engine.instances():
        if inst["state"] == "DONE":
            continue
        auto_engine.stop_symbol(inst["symbol"], close_position=close_positions)
        if close_positions and inst["lot_id"]:
            await order_mgr.close_lot(inst["lot_id"], exit_reason="auto_stop")
        stopped.append(inst["symbol"])
    record_event("auto_stop", symbols=stopped, close_positions=close_positions)
    return {"ok": True, "stopped": stopped, "close_positions": close_positions}


@app.get("/api/auto/status")
async def api_auto_status() -> dict:
    """策略状态：开关/策略名/待确认候选/全部实例（含收养的人工仓）。"""
    return {"enabled": _auto_enabled, "strategy": _auto_strategy_name,
            "max_instances": config.AUTO_MAX_INSTANCES,
            "instances": auto_engine.instances(),
            "proposals": auto_engine.proposals()}


@app.post("/api/auto/proposals/{proposal_id}/approve")
async def api_auto_proposal_approve(proposal_id: str) -> dict:
    """批准服务端保存的开仓候选，价格与方向不接受浏览器覆盖。"""
    if not _auto_enabled:
        raise HTTPException(409, "策略已停止，候选不可批准")
    proposal = next((p for p in auto_engine.proposals()
                     if p["proposal_id"] == proposal_id), None)
    if proposal is None:
        raise HTTPException(404, "候选不存在或已处理")
    symbol = proposal["symbol"]
    bar_c = ((collector.latest() or {}).get(symbol, {})
             .get("bar_c") or {})
    result, error = await auto_engine.approve_entry(proposal_id, bar_c=bar_c)
    if error:
        raise HTTPException(409, error)
    return {"ok": True, **result}


class AutoRejectRequest(BaseModel):
    reason: str = ""


@app.post("/api/auto/proposals/{proposal_id}/reject")
async def api_auto_proposal_reject(proposal_id: str,
                                   req: Optional[AutoRejectRequest] = None) -> dict:
    """忽略开仓候选，同一 bar 不重复提醒。"""
    result, error = await auto_engine.reject_entry(
        proposal_id, reason=(req.reason if req else ""))
    if error:
        raise HTTPException(409, error)
    return {"ok": True, **result}


# ---------------------------------------------------------------------------
# 回测工作台（会话式：加载 → 单步 / 全跑 → 订单全生命周期）
# ---------------------------------------------------------------------------

from engine.backtest_session import BacktestSession

_bt_session: Optional[BacktestSession] = None


class BtLoadRequest(BaseModel):
    symbol: str = Field(..., description="如 NVDA / NASDAQ:NVDA")
    day: str = ""                     # YYYY-MM-DD，默认上个交易日
    strategy: str = "trend_pullback"
    params: dict = Field(default_factory=dict)
    bars: int = 500


@app.post("/api/bt/load")
async def api_bt_load(req: BtLoadRequest) -> dict:
    """加载回测数据并初始化会话（拉取 5M bar + trend_state，约 5-10s）。"""
    global _bt_session
    from datetime import datetime as _dt
    from backtest_trend import ET, fetch_history, prev_trading_day
    symbol = req.symbol.strip().upper()
    if ":" not in symbol:
        match = [s for s in config.WATCHLIST if s.split(":")[-1] == symbol]
        if match:
            symbol = match[0]
    day = req.day or prev_trading_day(_dt.now(ET)).date().isoformat()
    try:
        rows = await fetch_history(symbol, req.bars, day)
    except Exception as e:
        raise HTTPException(400, f"历史数据拉取失败: {e}")
    try:
        session = BacktestSession(rows, symbol, day, req.strategy, req.params)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if session.total_bars == 0:
        raise HTTPException(400, f"{symbol} 在 {day} 无常规时段数据")
    _bt_session = session
    logger.info("回测会话加载 %s %s [%s]：%d 根 bar", symbol, day,
                req.strategy, session.total_bars)
    return _bt_session.state()


@app.post("/api/bt/step")
async def api_bt_step(n: int = 1) -> dict:
    """单步：前进 n 根 bar（默认 1 根）。"""
    if _bt_session is None:
        raise HTTPException(400, "请先加载回测数据（/api/bt/load）")
    return await _bt_session.step(n)


@app.post("/api/bt/run_all")
async def api_bt_run_all() -> dict:
    """一次性跑完当日剩余全部 bar。"""
    if _bt_session is None:
        raise HTTPException(400, "请先加载回测数据（/api/bt/load）")
    return await _bt_session.run_all()


@app.get("/api/bt/state")
async def api_bt_state() -> dict:
    """当前回测会话状态（进度/当前 bar/实例/订单生命周期/汇总）。"""
    if _bt_session is None:
        raise HTTPException(400, "无活跃回测会话")
    return _bt_session.state()


@app.get("/api/backtest")
async def api_backtest(symbol: str, day: str = "", bars: int = 500) -> dict:
    """历史回测：把指定交易日的 5M bar 逐根喂给回测引擎，
    返回每一次入场信号的统计与 N 倍盈亏比分布。"""
    from datetime import datetime as _dt
    from backtest_trend import ET, fetch_history, prev_trading_day, run_backtest
    symbol = symbol.strip().upper()
    if ":" not in symbol:   # 补交易所前缀
        match = [s for s in config.WATCHLIST if s.split(":")[-1] == symbol]
        if match:
            symbol = match[0]
    if not day:
        day = prev_trading_day(_dt.now(ET)).date().isoformat()
    try:
        rows = await fetch_history(symbol, bars)
    except Exception as e:
        raise HTTPException(400, f"历史数据拉取失败: {e}")
    return {"symbol": symbol, **run_backtest(rows, day)}


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


@app.post("/api/lots/limit-stop")
async def api_place_limit_stop(req: LimitStopOrderRequest) -> dict:
    """LMT 父单 + STP 子单下单；midpoint 由服务端按已收盘 bar 中点定价。
    方向必须与 ts_c.trend_state 同向（1 多 / -1 空），震荡拦截；
    force=执意开仓放行（需原因）。"""
    if req.side not in ("long", "short"):
        raise HTTPException(400, f"方向非法: {req.side}")
    if req.limit_type not in ("ema", "channel", "midpoint"):
        raise HTTPException(400, f"不支持的限价类型: {req.limit_type}")
    if not config.ALLOW_SHORT and req.side == "short":
        raise HTTPException(400, "当前仅允许做多（空头已屏蔽）")
    # trend_state 过滤拦截（含 ATR 缺失检查；force=二次确认放行）
    _check_trend_filter(req.symbol, req.side, req.force, req.reason)
    limit_px, stop_px = req.limit_px, req.stop_px
    reprice_bar_time = 0.0
    if req.limit_type == "midpoint":
        data = (collector.latest() or {}).get(req.symbol, {})
        try:
            limit_px, stop_px, bar, _ = _midpoint_prices(data, req.side)
        except ValueError as e:
            raise HTTPException(400, str(e))
        reprice_bar_time = bar["time"]
    qty = req.qty
    if qty == 0:
        equity = await gateway.net_liquidation()
        qty = risk_gate.size_position(limit_px, stop_px, equity)
    if qty <= 0:
        raise HTTPException(400, "无法确定有效股数（请显式填写 qty）")

    lot, err = await order_mgr.place_limit_stop_lot(
        symbol=req.symbol, side=req.side, qty=qty,
        limit_px=limit_px, stop_px=stop_px,
        limit_type=req.limit_type, reason=req.reason,
        risk_usd=req.risk_usd,
        signal_snapshot=_snapshot_for(symbol=req.symbol),
        reprice_bar_time=reprice_bar_time)
    if lot is None:
        raise HTTPException(400, err)
    record_event("order_placed", lot_id=lot.lot_id, symbol=req.symbol,
                 side=req.side, qty=qty, limit_px=limit_px,
                 stop_px=stop_px, limit_type=req.limit_type,
                 risk_usd=req.risk_usd, force=req.force, reason=req.reason)
    return {"ok": True, "lot": lot.to_dict()}


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
    """提交平仓并进入 CLOSING；实际成交由 close watcher 记录。"""
    ok, err = await order_mgr.close_lot(lot_id)
    record_event("order_close_requested" if ok else "order_close_failed",
                 lot_id=lot_id, error=(err if not ok else ""))
    if not ok:
        raise HTTPException(400, err)
    return {"ok": True}


@app.post("/api/positions/{symbol}/close")
async def api_close_position(symbol: str) -> dict:
    """按 symbol 处理本工具 lot；外部持仓仅由外部清理循环处理。"""
    ticker = symbol.split(":")[-1]
    lots = order_mgr.list_lots(active_only=True, symbol=symbol)
    if not lots:
        raise HTTPException(400, f"{ticker} 没有本工具管理的订单或持仓")
    results = []
    errors = []
    for lot in lots:
        if lot["state"] == "PENDING":
            ok, err = await order_mgr.cancel_lot(lot["lot_id"])
            action = "cancel"
        elif lot["state"] == "ACTIVE":
            snap = (collector.latest() or {}).get(symbol, {})
            ref_px = (snap.get("bar") or {}).get("close")
            ok, err = await order_mgr.close_lot(
                lot["lot_id"], ref_px=ref_px, exit_reason="manual")
            action = "close"
        else:
            continue
        results.append({"lot_id": lot["lot_id"], "action": action, "ok": ok})
        if not ok:
            errors.append(f"{lot['lot_id']}: {err}")
    if errors:
        raise HTTPException(400, "; ".join(errors))
    _scan_alerts.pop(ticker, None)
    qty = sum(l.get("filled_qty", 0) for l in lots if l["state"] == "ACTIVE")
    side = lots[0]["side"] if lots else ""
    record_event("position_close_requested", symbol=symbol, results=results)
    return {"ok": True, "symbol": symbol, "side": side, "qty": qty,
            "results": results}


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
