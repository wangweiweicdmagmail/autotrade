"""trading application functions and owned state. No console.app dependency."""

from engine.opening_breakout import RULES_VERSION, entry_window
from fastapi import HTTPException
from typing import List
from typing import Optional
import asyncio
import math
from datetime import datetime
from zoneinfo import ZoneInfo

import config
from domain.contracts import finite_number
from console.shared import logger
from console.schemas import LimitStopOrderRequest, OpeningBreakoutOrderRequest, UpdateTrailRequest


ET = ZoneInfo("America/New_York")


class TradingService:
    def __init__(self, context):
        self.context = context

    def _attack_zone(self, data: dict):
        """快照里的进攻区域盘前区间（attack_c 确认值 pmh/pml）。
        无数据（空 dict / null / 区间非法）返回 (None, None)——过滤时一律保留。"""
        atk = data.get("attack_c") or {}
        pmh, pml = (atk.get("pmh"), atk.get("pml"))
        try:
            if pmh is not None and pml is not None and (float(pmh) > float(pml) > 0):
                return (float(pmh), float(pml))
        except (TypeError, ValueError):
            pass
        return (None, None)

    def _premarket_pass(self, side: str, close, pmh, pml) -> bool:
        """盘前区间过滤判定：多头要求现价 > pmh，空头要求现价 < pml；
        区间或现价缺失一律保留（无法判断时不误杀）。"""
        if pmh is None or pml is None or close is None:
            return True
        if side == "long":
            return close > pmh
        return close < pml

    def _ts_c(self, data: dict) -> dict:
        """trend_state 确认值（上一根已收盘 5M bar）；缺失时退化实时值。"""
        return data.get("ts_c") or data.get("ts") or {}

    def _smart_stop_dist(
        self, data: dict, side: str, ref_px: float, atr: float
    ) -> float:
        """智能止损距离（优先读 smart_stop 指标 plot，所见即所得；缺失回退本地计算）。
        指标口径（smart_stop.pine）：距离 = close − Smart_Stop_Long（多）/
        Smart_Stop_Short − close（空），内部已是「3 根极值 + ATR 钳制」。
        本地回退：同样的逻辑止损点（最近三根 K 线极值，含当前 bar）+
        ATR 钳制 [1×ATR, 上限×ATR]（上限 = 全局 stop_atr_mult）。"""
        dist = self.context.trading._smart_stop_dist_from_indicator(data, side)
        if dist is not None:
            return dist
        lows, highs = ([], [])
        for key in ("bar", "bar_c", "bar_c2"):
            b = data.get(key) or {}
            lo, hi = (b.get("low"), b.get("high"))
            if lo is not None and hi is not None:
                lows.append(lo)
                highs.append(hi)
        if side == "long":
            logical = ref_px - min(lows) if lows else 0.0
        else:
            logical = max(highs) - ref_px if highs else 0.0
        lo, hi = (atr, max(self.context.settings._stop_atr_mult(), 1.0) * atr)
        return min(max(logical, lo), hi)

    def _smart_stop_dist_from_indicator(self, data: dict, side: str):
        """读 smart_stop 指标的止损距离（ts_c 确认值，对应 bar_c 的 close）。
        缺失/非法返回 None（调用方回退本地计算）。"""
        ts = self.context.trading._ts_c(data)
        close = (data.get("bar_c") or {}).get("close")
        ssl = (
            ts.get("smart_stop_long") if side == "long" else ts.get("smart_stop_short")
        )
        if ssl is None or close is None:
            return None
        dist = close - ssl if side == "long" else ssl - close
        return dist if dist > 0 else None

    def _midpoint_prices(self, data: dict, side: str, *, opening_stop=None):
        """由上一根已收盘 5M bar 计算中点限价、ATR 止损与突破触发价。

        突破触发价：多单 = bar 高点 + 0.01，空单 = bar 低点 - 0.01，
        供 OCA 突破腿（STP stop-market）使用；止损 = 中点限价 ∓ 智能距离
        （最近三根 K 线极值逻辑止损，钳制在 1×ATR ~ 上限×ATR，上限=全局倍数）。"""
        bar = data.get("bar_c") or {}
        ts = self.context.trading._ts_c(data)
        high, low = (bar.get("high"), bar.get("low"))
        atr = ts.get("atr")
        if high is None or low is None or (not bar.get("time")):
            raise ValueError("缺少已收盘 5M K 线，无法计算中点限价")
        if high < low:
            raise ValueError("5M K 线 high/low 数据非法")
        if opening_stop is None and (not atr or atr <= 0):
            raise ValueError("ATR 数据缺失，无法计算中点单止损")
        limit = round((high + low) / 2, 2)
        stop = opening_stop if opening_stop is not None else round(
            limit
            + (-1 if side == "long" else 1)
            * self.context.trading._smart_stop_dist(data, side, limit, atr),
            2,
        )
        breakout = round(high + 0.01 if side == "long" else low - 0.01, 2)
        if limit <= 0 or stop <= 0 or breakout <= 0:
            raise ValueError("中点限价/止损/突破价必须为正")
        if side == "long" and stop >= limit:
            raise ValueError("多仓止损必须低于中点限价")
        if side == "short" and stop <= limit:
            raise ValueError("空仓止损必须高于中点限价")
        return (limit, stop, breakout, bar, float(atr or 0))

    def _retest_prices(self, data: dict, side: str):
        """由上上根已收盘 5M bar（bar_c2，periods[2]）计算回踩限价与逻辑止损。

        多头：限价 = bar_c2.high（等回踩成交，不回踩形成 gap 不成交），
        止损 = bar_c2.low；空头镜像（限价 = bar_c2.low，止损 = bar_c2.high）。
        止损取 K 线极值，不依赖 ATR（与中点单的 ∓N×ATR 不同，N = 全局止损倍数）。"""
        bar = data.get("bar_c2") or {}
        high, low = (bar.get("high"), bar.get("low"))
        if high is None or low is None or (not bar.get("time")):
            raise ValueError("缺少上上根已收盘 5M K 线（bar_c2），无法计算回踩限价")
        if high < low:
            raise ValueError("5M K 线 high/low 数据非法")
        limit = round(high if side == "long" else low, 2)
        stop = round(low if side == "long" else high, 2)
        if limit <= 0 or stop <= 0:
            raise ValueError("回踩限价/止损必须为正")
        if side == "long" and stop >= limit:
            raise ValueError("多仓止损必须低于回踩限价")
        if side == "short" and stop <= limit:
            raise ValueError("空仓止损必须高于回踩限价")
        return (limit, stop, bar)

    def _fvg_chase_prices(self, data: dict, side: str, symbol: str, *, opening_stop=None):
        """FVG 追价定价（服务端，不信浏览器价）：
        以该 symbol + 方向在 4h 缓冲内的最新 FVG 事件边沿为锚（多=top / 空=bottom），
        现价距边沿 ≤ FVG_CHASE_MAX_ATR_RATIO × ATR 才允许追价；
        限价 = 实时 close × (1 ± ENTRY_LIMIT_PREMIUM_PCT/100)（marketable 限价，
        与系统不用裸 MARKET 的惯例一致），止损 = 现价 ∓ N×ATR（N = 全局止损倍数）。"""
        bar = data.get("bar") or {}
        ts = self.context.trading._ts_c(data)
        close = bar.get("close")
        atr = ts.get("atr")
        if close is None or close <= 0:
            raise ValueError("缺少实时价格，无法追价")
        if not atr or atr <= 0:
            raise ValueError("ATR 数据缺失，无法计算追价止损")
        latest = None
        for ev in reversed(self.context.signals._fvg_events):
            if ev["symbol"] == symbol and ev["side"] == side:
                latest = ev
                break
        if latest is None:
            raise ValueError("无可用 FVG 事件，不可追价")
        edge = latest["top"] if side == "long" else latest["bottom"]
        dist_ratio = abs(close - edge) / atr
        max_ratio = config.FVG_CHASE_MAX_ATR_RATIO
        if dist_ratio > max_ratio:
            raise ValueError(
                f"价格已偏离 FVG 边沿过远（{dist_ratio:.2f}×ATR > {max_ratio}×ATR），不可追价"
            )
        premium = config.ENTRY_LIMIT_PREMIUM_PCT / 100
        limit = round(close * (1 + premium if side == "long" else 1 - premium), 2)
        stop = opening_stop if opening_stop is not None else round(
            close
            + (-1 if side == "long" else 1)
            * self.context.trading._smart_stop_dist(data, side, close, atr),
            2,
        )
        if limit <= 0 or stop <= 0:
            raise ValueError("追价限价/止损必须为正")
        return (limit, stop)

    def _market_prices(self, data: dict, side: str, *, opening_stop=None):
        """市价单定价（服务端，不信浏览器价）：marketable 限价 =
        实时 close × (1 ± ENTRY_LIMIT_PREMIUM_PCT/100)（不用裸 MARKET：延迟行情
        会被券商拒单 error 10349），止损 = 现价 ∓ N×ATR（ts_c 确认值，
        N = 全局止损倍数 _stop_atr_mult()，默认 1.0）。"""
        bar = data.get("bar") or {}
        ts = self.context.trading._ts_c(data)
        close = bar.get("close")
        atr = ts.get("atr")
        if close is None or close <= 0:
            raise ValueError("缺少实时价格，无法下市价单")
        if opening_stop is None and (not atr or atr <= 0):
            raise ValueError("ATR 数据缺失，无法计算市价单止损")
        premium = config.ENTRY_LIMIT_PREMIUM_PCT / 100
        limit = round(close * (1 + premium if side == "long" else 1 - premium), 2)
        stop = opening_stop if opening_stop is not None else round(
            close
            + (-1 if side == "long" else 1)
            * self.context.trading._smart_stop_dist(data, side, close, atr),
            2,
        )
        if limit <= 0 or stop <= 0:
            raise ValueError("市价单限价/止损必须为正")
        return (limit, stop)

    def _check_trend_filter(
        self, symbol: str, side: str, force: bool = False, reason: str = "", *, require_atr=True
    ) -> None:
        """SuperTrend 方向闸门（下单拦截）：
        做多需 ts_c.direction == 1（SuperTrend 多头），做空需 == -1；
        震荡（trend_state=0）但 ST 方向正确也放行。side 必须与 ST 方向同向。
        force=执意开仓放行（需填原因，记 WARN 日志）。"""
        if force:
            logger.warning(
                "执意开仓放行（force）: %s %s 原因: %s", symbol, side, reason
            )
            return
        snap = (self.context.collector.latest() or {}).get(symbol, {})
        ts = self.context.trading._ts_c(snap)
        direction = ts.get("direction")
        if require_atr and ts.get("atr") is None:
            raise HTTPException(
                400,
                f"{symbol} ATR 数据缺失，请检查图表上的 trend_state 指标（Atr Value plot）",
            )
        if side == "long" and direction != 1:
            raise HTTPException(
                400,
                {
                    "code": "not_long_candidate",
                    "msg": f"{symbol} 不在多头列表（ST方向={direction}）",
                },
            )
        if side == "short" and direction != -1:
            raise HTTPException(
                400,
                {
                    "code": "not_long_candidate",
                    "msg": f"{symbol} 不在空头列表（ST方向={direction}）",
                },
            )

    async def _midpoint_limit_reprice_loop(self) -> None:
        """中点限价单监控循环（每秒）。

        2026-10-20 起中点单一次定价不再逐 bar 改价：OCA 双腿（LMT 中点 +
        STP 突破）挂出后价格固定，直到成交/撤单/收盘。本循环仅为旧式
        单腿中点单（无突破腿）保留"突破升级市价"的兜底。"""
        while True:
            await asyncio.sleep(1.0)
            try:
                pending = [
                    l
                    for l in self.context.order_mgr.list_lots(active_only=True)
                    if l["state"] == "PENDING"
                    and l.get("limit_type") == "midpoint"
                    and (not l.get("breakout_escalated"))
                    and (not l.get("breakout_order_id"))
                ]
                if not pending:
                    continue
                market = self.context.collector.latest() or {}
                for lot in pending:
                    data = market.get(lot["symbol"]) or {}
                    bar = data.get("bar_c") or {}
                    cur = data.get("bar") or {}
                    high, low = (bar.get("high"), bar.get("low"))
                    cur_high, cur_low = (cur.get("high"), cur.get("low"))
                    triggered = False
                    if high is not None and low is not None:
                        if (
                            lot["side"] == "long"
                            and cur_high is not None
                            and (cur_high > high)
                        ):
                            triggered = True
                        elif (
                            lot["side"] == "short"
                            and cur_low is not None
                            and (cur_low < low)
                        ):
                            triggered = True
                    if triggered:
                        bt0 = bar.get("time") or 0
                        esc_key = (lot["lot_id"], bt0)
                        if (
                            esc_key
                            not in self.context.trading._midpoint_escalate_attempted
                        ):
                            self.context.trading._midpoint_escalate_attempted.add(
                                esc_key
                            )
                            (
                                ok,
                                error,
                            ) = await self.context.order_mgr.escalate_midpoint_to_market(
                                lot["lot_id"]
                            )
                            self.context.events.record_event(
                                "midpoint_breakout_escalated"
                                if ok
                                else "midpoint_breakout_escalate_failed",
                                lot_id=lot["lot_id"],
                                symbol=lot["symbol"],
                                side=lot["side"],
                                trigger_high=high,
                                trigger_low=low,
                                cur_high=cur_high,
                                cur_low=cur_low,
                                bar_time=bt0,
                                error=error,
                            )
                            if ok:
                                logger.warning(
                                    "%s 中点单未等到回调，价格突破上根K线%s，已升级为市价单",
                                    lot["symbol"],
                                    "高点" if lot["side"] == "long" else "低点",
                                )
                            else:
                                logger.warning(
                                    "%s 中点单突破升级失败: %s", lot["symbol"], error
                                )
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.exception("中点限价单改价循环异常: %s", e)

    async def _retest_limit_reprice_loop(self) -> None:
        """回踩限价单逐 bar 改价循环（每秒检查）。

        每根新确认 5M bar 收盘后 bar_c2 随之前移，对 PENDING 的回踩 lot
        用新 bar_c2 重算限价/止损（多 limit=bar_c2.high / stop=bar_c2.low，
        空头镜像），并按固定风险预算（lot.init_risk_usd，下单时的计划风险
        金额，含仓位%封顶效果，作为锚点不动）以新止损距离重新以损定量：
        qty_new = floor(init_risk_usd / |new_limit − new_stop|)，封顶
        RISK_MAX_ORDER_QTY；仅 filled_qty == 0 时改股数（部分成交后只改价）。
        仅 PENDING、每 bar 每 lot 最多一次（reprice_bar_time 幂等）、
        失败保留原单下根重试，ACTIVE 后自动停止。"""
        while True:
            await asyncio.sleep(1.0)
            try:
                pending = [
                    l
                    for l in self.context.order_mgr.list_lots(active_only=True)
                    if l["state"] == "PENDING" and l.get("limit_type") == "retest"
                ]
                if not pending:
                    continue
                market = self.context.collector.latest() or {}
                for lot in pending:
                    data = market.get(lot["symbol"]) or {}
                    bar2 = data.get("bar_c2") or {}
                    bt = bar2.get("time") or 0
                    if not bt or bt <= (lot.get("reprice_bar_time") or 0):
                        continue
                    try:
                        new_limit, new_stop, _ = self.context.trading._retest_prices(
                            data, lot["side"]
                        )
                    except ValueError as e:
                        logger.warning("%s 回踩单改价跳过: %s", lot["symbol"], e)
                        continue
                    if (lot.get("signal_snapshot") or {}).get("stop_source") == "opening_bar_extreme":
                        new_stop = lot["stop_px"]
                        if not (new_stop < new_limit if lot["side"] == "long" else new_stop > new_limit):
                            continue
                    qty_new = 0
                    if not lot.get("filled_qty"):
                        init_risk = lot.get("init_risk_usd") or 0
                        dist = abs(new_limit - new_stop)
                        if init_risk > 0 and dist > 0:
                            qty_new = int(init_risk / dist)
                            if config.RISK_MAX_ORDER_QTY > 0:
                                qty_new = min(qty_new, config.RISK_MAX_ORDER_QTY)
                            max_pct = lot.get("max_position_pct") or 0
                            equity = (
                                self.context.accounts._equity_cache.get("equity") or 0
                            )
                            if max_pct > 0 and equity > 0 and (new_limit > 0):
                                qty_new = min(
                                    qty_new, int(equity * max_pct / 100 / new_limit)
                                )
                        else:
                            qty_new = lot["qty"]
                    ok, error = await self.context.order_mgr.reprice_retest_lot(
                        lot["lot_id"], new_limit, new_stop, bt, new_qty=qty_new
                    )
                    if ok and error == "unchanged":
                        continue
                    cur = self.context.order_mgr.get(lot["lot_id"])
                    self.context.events.record_event(
                        "retest_limit_repriced"
                        if ok
                        else "retest_limit_reprice_failed",
                        lot_id=lot["lot_id"],
                        symbol=lot["symbol"],
                        side=lot["side"],
                        limit_px=new_limit,
                        stop_px=new_stop,
                        qty=cur.qty if cur is not None else lot["qty"],
                        bar_time=bt,
                        error=error,
                    )
                    if not ok:
                        logger.warning(
                            "%s 回踩单改价失败（保留原挂单，下根 bar 重试）: %s",
                            lot["symbol"],
                            error,
                        )
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.exception("回踩限价单改价循环异常: %s", e)

    def _snapshot_for(self, symbol: str) -> dict:
        """下单时抓取该标的的 trend_state 快照（复盘用）。"""
        snap = (self.context.collector.latest() or {}).get(symbol, {})
        return {
            "ts": snap.get("ts") or {},
            "ts_c": snap.get("ts_c") or snap.get("ts") or {},
            "close": (snap.get("bar") or {}).get("close"),
        }

    async def api_lots(
        self, active_only: bool = False, symbol: Optional[str] = None
    ) -> List[dict]:
        return self.context.order_mgr.list_lots(active_only=active_only, symbol=symbol)

    async def api_positions(self) -> List[dict]:
        return self.context.order_mgr.aggregate()

    async def api_trade_history(self, *, symbol=None, before=None, limit=100) -> list:
        records = await asyncio.to_thread(self.context.order_mgr._store.archive_records)
        if symbol:
            requested = symbol.upper()
            records = [
                row for row in records
                if (row.get("symbol") or "").upper() == requested
                or (":" not in requested and (row.get("symbol") or "").split(":")[-1].upper() == requested)
            ]
        if before is not None:
            records = [row for row in records if (row.get("closed_at") or 0) < before]
        return sorted(records, key=lambda row: row.get("closed_at") or 0, reverse=True)[:limit]

    async def api_place_opening_breakout(
        self, req: OpeningBreakoutOrderRequest, trader: str, *, deadline: float | None = None,
    ) -> dict:
        now = datetime.now(ET)
        signal = next((row for row in self.context.signals.opening_breakouts(now.timestamp())
                       if row["id"] == req.signal_id), None)
        if signal is None:
            raise HTTPException(404, "找不到本交易日的小区间突破信号")
        if deadline is None and req.signal_id in self.context.signals._opening_orders:
            raise HTTPException(409, "该信号已有收盘开仓预约，请查看预约状态")
        signal_open = finite_number(signal.get("open"))
        if signal_open is None or signal_open <= 0:
            raise HTTPException(409, "突破信号缺少开盘价，请等待信号数据更新")
        if signal.get("rules_version") != RULES_VERSION:
            raise HTTPException(409, "信号规则版本已更新，请等待新信号")
        if not entry_window(now.timestamp()) or not signal["time"] <= now.timestamp() < signal["time"] + 20:
            raise HTTPException(409, "已超过突破收盘开仓窗口，未下单")
        deadline = min(deadline or signal["time"] + 20, signal["time"] + 20)
        stop = round((signal_open + signal["close"]) / 2, 2)
        return await self.api_place_limit_stop(
            LimitStopOrderRequest(
                symbol=signal["symbol"], side=signal["side"], limit_type="market",
                limit_px=signal["close"], stop_px=stop, reason="小区间突破收盘确认开仓" if deadline is not None else "小区间突破手动开仓",
                max_position_pct=req.max_position_pct,
            ),
            trader=trader, opening_breakout=signal, deadline=deadline,
        )

    async def api_place_limit_stop(
        self, req: LimitStopOrderRequest, trader: str = None, *, opening_breakout: dict | None = None, deadline: float | None = None
    ) -> dict:
        """LMT 父单 + STP 子单下单；midpoint/retest 由服务端按已收盘 bar 定价。
        方向必须与 ts_c.direction（SuperTrend 方向）同向（1 多 / -1 空），震荡可开；
        force=执意开仓放行（需原因）。"""
        if req.side not in ("long", "short"):
            raise HTTPException(400, f"方向非法: {req.side}")
        if req.qty < 0 or not math.isfinite(req.max_position_pct):
            raise HTTPException(400, "股数或最大仓位比例无效")
        if req.limit_type not in (
            "ema",
            "channel",
            "midpoint",
            "retest",
            "chase",
            "market",
        ):
            raise HTTPException(400, f"不支持的限价类型: {req.limit_type}")
        if not config.ALLOW_SHORT and req.side == "short":
            raise HTTPException(400, "当前仅允许做多（空头已屏蔽）")
        now = datetime.now(ET)
        opening = now.weekday() < 5 and 570 <= now.hour * 60 + now.minute < 580
        data = (self.context.collector.latest() or {}).get(req.symbol, {})
        bar = data.get("bar") or {}
        if opening_breakout:
            if not entry_window(now.timestamp()):
                raise HTTPException(400, "突破开仓仅限09:45至10:30之前")
            bar_time = finite_number(bar.get("time"))
            close = finite_number(bar.get("close"))
            if (bar_time is None or not bar_time <= now.timestamp() < bar_time + 300
                    or close is None or close <= 0):
                raise HTTPException(400, "当前标的行情未就绪，暂不能开仓")
        opening_stop = None
        if opening:
            bar_time = finite_number(bar.get("time"))
            low, high = finite_number(bar.get("low")), finite_number(bar.get("high"))
            if (
                bar_time is None or not bar_time <= now.timestamp() < bar_time + 300
                or low is None or high is None or not 0 < low <= high
            ):
                raise HTTPException(400, "当前 5 分钟 K 线高低点未就绪，无法设置开盘止损")
            opening_stop = round(low if req.side == "long" else high, 2)
        self.context.trading._check_trend_filter(
            req.symbol, req.side, req.force, req.reason, require_atr=not opening and not opening_breakout,
        )
        limit_px, stop_px = (req.limit_px, req.stop_px)
        reprice_bar_time = 0.0
        breakout_px = 0.0
        if req.limit_type == "midpoint":
            try:
                limit_px, stop_px, breakout_px, bar, _ = (
                    self.context.trading._midpoint_prices(data, req.side, opening_stop=opening_stop)
                )
            except ValueError as e:
                raise HTTPException(400, str(e))
            reprice_bar_time = bar["time"]
        elif req.limit_type == "retest":
            try:
                limit_px, stop_px, bar = self.context.trading._retest_prices(
                    data, req.side
                )
            except ValueError as e:
                raise HTTPException(400, str(e))
            reprice_bar_time = bar["time"]
        elif req.limit_type == "chase":
            try:
                limit_px, stop_px = self.context.trading._fvg_chase_prices(
                    data, req.side, req.symbol, opening_stop=opening_stop
                )
            except ValueError as e:
                raise HTTPException(400, str(e))
        elif opening_breakout:
            limit_px = close
            if not (0 < stop_px < close if req.side == "long" else 0 < close < stop_px):
                raise HTTPException(400, "现价已触及或越过突破 K 线止损，无法开仓")
        elif req.limit_type == "market":
            try:
                limit_px, stop_px = self.context.trading._market_prices(data, req.side, opening_stop=opening_stop)
            except ValueError as e:
                raise HTTPException(400, str(e))
        risk_budget = config.RISK_PER_TRADE_USD
        reference_px = limit_px
        risk_ref_px = 0.0
        reprice_qty = req.limit_type in ("market", "chase")
        if reprice_qty:
            risk_ref_px = (data.get("bar") or {}).get("close") or 0
            reference_px = risk_ref_px
        if opening:
            stop_px = opening_stop
            if not (0 < stop_px < reference_px if req.side == "long" else 0 < reference_px < stop_px):
                raise HTTPException(400, "当前 K 线极值未形成有效止损距离，暂不能开仓")
        dist = abs(reference_px - stop_px)
        if not math.isfinite(dist) or reference_px <= 0 or stop_px <= 0 or dist <= 0:
            raise HTTPException(400, "风险距离无效（参考价/止损缺失），拒绝下单")
        qty = min(int(risk_budget / dist), config.RISK_MAX_ORDER_QTY)
        equity = self.context.accounts._equity_cache.get("equity") or 0
        if equity > 0 and req.max_position_pct > 0 and limit_px > 0:
            qty = min(qty, int(equity * req.max_position_pct / 100 / limit_px))
        if not reprice_qty and req.qty > 0:
            qty = min(qty, req.qty)
        if qty <= 0:
            raise HTTPException(400, "无法确定有效股数（请显式填写 qty）")
        signal_snapshot = self.context.trading._snapshot_for(symbol=req.symbol)
        market_frame = getattr(self.context.collector, "market_frame_id", None)
        if market_frame is not None:
            signal_snapshot["decision_market_frame"] = market_frame
        self.context.events.record_event(
            "manual_entry_decision", symbol=req.symbol, side=req.side, trader=trader,
            qty=qty, limit_px=limit_px, stop_px=stop_px, limit_type=req.limit_type,
            reason=req.reason, force=req.force, decision_market_frame=market_frame,
        )
        if opening:
            signal_snapshot.update(stop_source="opening_bar_extreme", stop_bar_time=data["bar"]["time"])
        if opening_breakout:
            signal_snapshot.update(
                stop_source="opening_breakout_body_midpoint",
                stop_bar_time=opening_breakout["time"] - 300,
                opening_breakout_id=opening_breakout["id"],
                breakout_protection_rule="signal_midpoint_then_close_5m",
                breakout_rules_version=RULES_VERSION,
                breakout_rule_evidence=dict(opening_breakout),
                breakout_signal_open=opening_breakout["open"],
                breakout_signal_close=opening_breakout["close"],
                breakout_signal_bar_time=opening_breakout.get("bar_time", opening_breakout["time"] - 300),
            )
        lot, err = await self.context.order_mgr.place_limit_stop_lot(
            symbol=req.symbol,
            side=req.side,
            qty=qty,
            limit_px=limit_px,
            stop_px=stop_px,
            limit_type=req.limit_type,
            reason=req.reason,
            risk_usd=risk_budget,
            signal_snapshot=signal_snapshot,
            reprice_bar_time=reprice_bar_time,
            breakout_px=breakout_px,
            max_position_pct=req.max_position_pct,
            atr_mult=self.context.settings._stop_atr_mult(),
            trader=trader,
            risk_ref_px=risk_ref_px,
            manual_entry=True,
            entry_order_type="MKT" if opening_breakout else "",
            **({"submission_deadline": deadline} if deadline is not None else {}),
        )
        if lot is None:
            self.context.events.record_event(
                "manual_entry_rejected", symbol=req.symbol, side=req.side, trader=trader,
                decision_market_frame=market_frame, reason=err,
            )
            raise HTTPException(400, err)
        self.context.events.record_event(
            "order_placed",
            lot_id=lot.lot_id,
            decision_market_frame=market_frame,
            symbol=req.symbol,
            side=req.side,
            qty=qty,
            limit_px=limit_px,
            stop_px=stop_px,
            limit_type=req.limit_type,
            breakout_px=breakout_px,
            trader=trader,
            risk_usd=risk_budget,
            force=req.force,
            reason=req.reason,
        )
        return {"ok": True, "lot": lot.to_dict()}

    async def api_update_trail(
        self, lot_id: str, req: UpdateTrailRequest, trader: str = None
    ) -> dict:
        ok, err = await self.context.order_mgr.update_trail(lot_id, req.stop_px)
        if not ok:
            raise HTTPException(400, err)
        return {"ok": True}

    async def api_cancel_lot(self, lot_id: str, trader: str = None) -> dict:
        ok, err = await self.context.order_mgr.cancel_lot(lot_id)
        self.context.events.record_event(
            "order_cancelled" if ok else "order_cancel_failed",
            lot_id=lot_id,
            error=err if not ok else "",
        )
        if not ok:
            raise HTTPException(400, err)
        return {"ok": True}

    async def api_close_lot(self, lot_id: str, trader: str = None) -> dict:
        """提交平仓并进入 CLOSING；实际成交由 close watcher 记录。"""
        ok, err = await self.context.order_mgr.close_lot(lot_id)
        self.context.events.record_event(
            "order_close_requested" if ok else "order_close_failed",
            lot_id=lot_id,
            error=err if not ok else "",
        )
        if not ok:
            raise HTTPException(400, err)
        return {"ok": True}

    async def api_breakeven_lot(self, lot_id: str, trader: str = None) -> dict:
        """手动保本：止损移到实际成本价（与自动 2R 保本同一逻辑）。"""
        ok, err = await self.context.order_mgr.breakeven(lot_id)
        self.context.events.record_event(
            "manual_breakeven" if ok else "manual_breakeven_failed",
            lot_id=lot_id,
            error=err if not ok else "",
        )
        if not ok:
            raise HTTPException(400, err)
        return {"ok": True}

    async def api_close_half(self, lot_id: str, trader: str = None) -> dict:
        """平仓一半：收缩 STOP 保护单到剩余量 + marketable 平一半；lot 保持 ACTIVE。"""
        ok, err = await self.context.order_mgr.close_half(lot_id)
        self.context.events.record_event(
            "order_close_half" if ok else "order_close_half_failed",
            lot_id=lot_id,
            error=err if not ok else "",
        )
        if not ok:
            raise HTTPException(400, err)
        return {"ok": True}

    async def api_close_position(self, symbol: str, trader: str = None) -> dict:
        """按 symbol 处理本工具 lot；外部持仓仅由外部清理循环处理。"""
        ticker = symbol.split(":")[-1]
        lots = self.context.order_mgr.list_lots(active_only=True, symbol=symbol)
        if not lots:
            raise HTTPException(400, f"{ticker} 没有本工具管理的订单或持仓")
        results = []
        errors = []
        for lot in lots:
            if lot["state"] == "PENDING":
                ok, err = await self.context.order_mgr.cancel_lot(lot["lot_id"])
                action = "cancel"
            elif lot["state"] == "ACTIVE":
                snap = (self.context.collector.latest() or {}).get(symbol, {})
                ref_px = (snap.get("bar") or {}).get("close")
                ok, err = await self.context.order_mgr.close_lot(
                    lot["lot_id"], ref_px=ref_px, exit_reason="manual"
                )
                action = "close"
            else:
                continue
            results.append({"lot_id": lot["lot_id"], "action": action, "ok": ok})
            if not ok:
                errors.append(f"{lot['lot_id']}: {err}")
        if errors:
            raise HTTPException(400, "; ".join(errors))
        self.context.monitoring._scan_alerts.pop(ticker, None)
        qty = sum((l.get("filled_qty", 0) for l in lots if l["state"] == "ACTIVE"))
        side = lots[0]["side"] if lots else ""
        self.context.events.record_event(
            "position_close_requested", symbol=symbol, results=results
        )
        return {
            "ok": True,
            "symbol": symbol,
            "side": side,
            "qty": qty,
            "results": results,
        }
