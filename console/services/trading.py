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
from execution.candidate_policy import POLICY_KEY, vwap_entry_check
from console.shared import logger
from console.schemas import LimitStopOrderRequest, OpeningBreakoutOrderRequest, UpdateTrailRequest


ET = ZoneInfo("America/New_York")


class TradingService:
    def __init__(self, context):
        self.context = context

    def _rhythm_entry_plan(self, symbol, data, now, stop_mode='atr_2'):
        """Manual entry intent, using the same closed-minute perception as the panel."""
        stamp = now.timestamp()
        if now.weekday() >= 5 or not 570 <= now.hour * 60 + now.minute < 960:
            raise HTTPException(400, "轨道市价单仅使用当日常规盘轨道")
        frame = self.context.research._market_rhythm.read(symbol, watch=symbol, now=stamp)
        tracks = frame.get("tracks", []) + frame.get("watch_tracks", [])
        row = next((t for t in tracks if t.get("symbol") == symbol and not t.get("is_beta")), None)
        if frame.get("status") != "ready" or not row or row.get("issue") or not row.get("aligned"):
            raise HTTPException(400, "四宫格轨道数据未就绪")
        point = next(iter(reversed(row.get("points", []))), None)
        if not point or not 0 <= stamp - (point[0] + 60) <= 120 or point[10] not in (-1, 1):
            raise HTTPException(400, "四宫格推荐方向或确认数据已失效")
        sigma = finite_number(point[3])
        bar = data.get("bar") or {}
        close, observed = finite_number(bar.get("close")), finite_number(data.get("price_observed_at"))
        bar_time = finite_number(bar.get("time"))
        if (close is None or close <= 0 or observed is None or not 0 <= stamp - observed <= 45
                or bar_time is None or not bar_time <= stamp < bar_time + 300):
            raise HTTPException(400, "实时价格或当前 5 分钟 K 线未就绪")
        side = "long" if point[10] == 1 else "short"
        try:
            stop = self._manual_stop_price(data,side,close,stop_mode,now,sigma)
        except ValueError as error:
            raise HTTPException(400, str(error))
        return side, close, stop, {"sigma": sigma, "vwap": point[2], "ema3": point[1],
            "bar_time": point[0], "confirmed_at": point[0] + 60, "direction": point[10],
            "stop_source": "manual_vwap_sigma" if stop_mode=='vwap_sigma' else "market_2atr_outside_bar", "stop_bar_time": bar_time,
            "stop_atr_interval_seconds": 300, "stop_atr_multiple": 2,
            "stop_atr_value": finite_number(self._ts_c(data).get("atr"))}

    def _manual_stop_price(self, data, side, reference, mode, now, sigma=None):
        if mode=='vwap_sigma':
            sigma=finite_number(sigma)
            if sigma is None or sigma<=0:
                raise ValueError('1分钟VWAP标准差未就绪，无法设置1σ止损')
            stop=round(reference+(-1 if side=='long' else 1)*sigma,2)
            if not (0<stop<reference if side=='long' else 0<reference<stop):
                raise ValueError('1σ止损取整后无效')
            return stop
        return self._market_prices(data,side,now=now,reference_px=reference,
            allow_missing_atr=now.weekday()<5 and 570<=now.hour*60+now.minute<580)[1]

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

    def _market_prices(self, data: dict, side: str, *, now=None, allow_missing_atr=False, reference_px=None):
        """手动市价入场的止损取 2×ATR，并至少放到当前 5 分钟 K 线外。"""
        bar = data.get("bar") or {}
        now = now or datetime.now(ET)
        bar_time = finite_number(bar.get("time"))
        low, high, close = (finite_number(bar.get(key)) for key in ("low", "high", "close"))
        if bar_time is None or not bar_time <= now.timestamp() < bar_time + 300:
            raise ValueError("当前 5 分钟 K 线未就绪，无法设置市价单止损")
        if low is None or high is None or close is None or not 0 < low <= close <= high:
            raise ValueError("当前 5 分钟 K 线价格无效，无法设置市价单止损")
        reference = close if reference_px is None else finite_number(reference_px)
        if reference is None or reference<=0:
            raise ValueError('入场参考价无效')

        atr = finite_number(self._ts_c(data).get("atr"))
        long = side == "long"
        extreme = low if long else high
        # Round to the order's cent precision, then enforce a strict outside price.
        outside = round(extreme, 2)
        if (long and outside >= extreme) or (not long and outside <= extreme):
            outside = round(outside + (-0.01 if long else 0.01), 2)
        if atr is None or atr <= 0:
            if not allow_missing_atr:
                raise ValueError("ATR 数据缺失，无法计算市价单止损")
            stop = outside
        else:
            atr_stop = round(reference + (-2 if long else 2) * atr, 2)
            stop = min(atr_stop, outside) if long else max(atr_stop, outside)
        premium = config.ENTRY_LIMIT_PREMIUM_PCT / 100
        limit = round(close * (1 + premium if long else 1 - premium), 2)
        if not math.isfinite(stop) or limit <= 0 or stop <= 0:
            raise ValueError("市价单限价/止损必须为正且有效")
        return limit, stop

    def _market_direction(self, data: dict, now: datetime) -> dict:
        """手动市价单用常规盘一分钟 ST(10, 4)，确认输出优先。"""
        minute_market = data.get("market_1m") or {}
        current = finite_number((minute_market.get("bar") or {}).get("time"))
        if current is None or not current <= now.timestamp() < current + 60:
            raise HTTPException(400, "常规盘 1 分钟 ST 未就绪，暂不能提交市价单")
        previous = finite_number((minute_market.get("bar_c") or {}).get("time"))
        for key, bar_time, confirmed in (
            ("vwap_pullback_c", previous if previous == current - 60 else None, True),
            ("vwap_pullback", current, False),
        ):
            output = data.get(key) or {}
            direction = finite_number(output.get("direction"))
            if bar_time is not None and finite_number(output.get("time")) == bar_time and direction in (1, -1):
                return {"time": bar_time, "direction": int(direction), "confirmed": confirmed}
        raise HTTPException(400, "1 分钟 ST 方向未就绪，暂不能提交市价单")

    def _check_trend_filter(
        self, symbol: str, side: str, force: bool = False, reason: str = "", *,
        require_atr=True, market_direction=None, manual_market_direction=False,
    ) -> None:
        """按下单入口的 ST 方向校验，保留执意开仓确认。"""
        if force:
            logger.warning(
                "执意开仓放行（force）: %s %s 原因: %s", symbol, side, reason
            )
            return
        snap = (self.context.collector.latest() or {}).get(symbol, {})
        ts = self.context.trading._ts_c(snap)
        direction = market_direction["direction"] if market_direction is not None else ts.get("direction")
        indicator = "1 分钟 ST" if market_direction is not None else "ST"
        if require_atr and ts.get("atr") is None:
            raise HTTPException(
                400,
                f"{symbol} ATR 数据缺失，请检查图表上的 trend_state 指标（Atr Value plot）",
            )
        if manual_market_direction:
            # 仅放行用户已确认的市价单方向，价格与风险检查仍在下单链路执行。
            return
        if side == "long" and direction != 1:
            raise HTTPException(
                400,
                {
                    "code": "not_long_candidate",
                    "msg": f"{symbol} 不在多头列表（{indicator}方向={direction}）",
                },
            )
        if side == "short" and direction != -1:
            raise HTTPException(
                400,
                {
                    "code": "not_long_candidate",
                    "msg": f"{symbol} 不在空头列表（{indicator}方向={direction}）",
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
                        selected_stop=(lot.get('signal_snapshot') or {}).get('manual_stop_mode')
                        if selected_stop in ('atr_2','vwap_sigma'):
                            new_stop=lot['stop_px'] if lot.get('filled_qty') else self._manual_stop_price(
                                data,lot['side'],new_limit,selected_stop,datetime.now(ET),
                                (lot.get('signal_snapshot') or {}).get('stop_sigma_value'))
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
        for row in records:
            row.pop("external_account", None)
            row.pop("external_allocations", None)
            row.pop("external_remaining", None)
            row.pop("external_exit_fills", None)
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
        """手动建仓与保护单；生产入口遵守有效候选及同向限制。
        未注入候选检查的独立环境保留原 ST/震荡方向兼容行为。"""
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
        manual_direction = req.market_direction_mode == "manual"
        # rhythm_sigma remains an accepted legacy identifier, using the new ATR policy.
        rhythm_entry = req.entry_mode in ("rhythm_atr", "rhythm_sigma")
        if req.candidate_order is not None and (not rhythm_entry or opening_breakout):
            raise HTTPException(400, "候选预订单设置仅用于轨道市价入口")
        if req.candidate_order is not None:
            shared = self.context.settings._candidate_order_protection()
            if shared.get('version') != 1:
                raise HTTPException(400, '候选保护设置未就绪，请在候选与订单页面重新保存')
            req.candidate_order = type(req.candidate_order)(**{**req.candidate_order.dict(), **shared})
        if rhythm_entry and (req.limit_type != "market" or opening_breakout or manual_direction or req.force):
            raise HTTPException(400, "轨道入场仅用于四宫格市价单，不能混用其他入场方式")
        if manual_direction:
            if req.limit_type != "market" or opening_breakout:
                raise HTTPException(400, "手动方向仅用于手动市价单")
            if not req.range_direction_confirmed:
                raise HTTPException(400, "请先确认按震荡行情入场，不要追高追低")
            if req.force:
                raise HTTPException(400, "手动方向不能同时使用执意开仓")
        elif req.range_direction_confirmed:
            raise HTTPException(400, "震荡行情确认必须与手动方向同时提交")
        if not config.ALLOW_SHORT and req.side == "short":
            raise HTTPException(400, "当前仅允许做多（空头已屏蔽）")
        now = datetime.now(ET)
        admission=getattr(self.context.order_mgr,'entry_admission',None)
        candidate_evidence=None
        if admission is not None and not opening_breakout:
            try:
                permission=admission(req.symbol,req.side,now.timestamp())
            except Exception as error:
                logger.exception('手动入场候选检查不可用')
                raise HTTPException(400,'候选入场限制：候选检查不可用') from error
            if not isinstance(permission,dict) or permission.get('ok') is not True:
                raise HTTPException(400,(permission or {}).get('reason','候选入场限制：候选验证失败') if isinstance(permission,dict) else '候选入场限制：候选验证失败')
            candidate_evidence=permission['evidence']
            if req.force or manual_direction or req.range_direction_confirmed:
                raise HTTPException(400,'候选趋势方向固定，不能使用反向或强制开仓')
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
        market_entry = req.limit_type == "market" and not opening_breakout
        if opening and not market_entry:
            bar_time = finite_number(bar.get("time"))
            low, high = finite_number(bar.get("low")), finite_number(bar.get("high"))
            if (
                bar_time is None or not bar_time <= now.timestamp() < bar_time + 300
                or low is None or high is None or not 0 < low <= high
            ):
                raise HTTPException(400, "当前 5 分钟 K 线高低点未就绪，无法设置开盘止损")
            opening_stop = round(low if req.side == "long" else high, 2)

        rhythm_evidence = None
        if rhythm_entry:
            recommended, rhythm_price, rhythm_stop, rhythm_evidence = self._rhythm_entry_plan(req.symbol, data, now,req.manual_stop_mode)
            if req.side != recommended:
                raise HTTPException(400, "四宫格推荐方向已变化，请重新确认")
            if req.candidate_order is not None and req.candidate_order.vwap_outside_only:
                evidence = candidate_evidence or dict(rhythm_evidence,
                    ema9_price=rhythm_price, ema9_observed_at=data.get('price_observed_at'))
                check = vwap_entry_check(evidence, req.side, now.timestamp())
                if not check['ok']:
                    raise HTTPException(400, check['reason'])
        market_direction = self._market_direction(data, now) if market_entry and not rhythm_entry and candidate_evidence is None else None
        if not rhythm_entry and candidate_evidence is None:
            self.context.trading._check_trend_filter(
                req.symbol, req.side, req.force, req.reason, require_atr=not opening and not opening_breakout,
                market_direction=market_direction,
                manual_market_direction=manual_direction,
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
        elif rhythm_entry:
            limit_px, stop_px = rhythm_price, rhythm_stop
        elif req.limit_type == "market":
            try:
                limit_px, stop_px = self.context.trading._market_prices(data, req.side, now=now,
                    allow_missing_atr=opening or req.manual_stop_mode=='vwap_sigma')
            except ValueError as e:
                raise HTTPException(400, str(e))
        if not opening_breakout and (candidate_evidence is not None or req.manual_stop_mode=='vwap_sigma'):
            evidence=candidate_evidence or rhythm_evidence
            if evidence is None:
                _,_,_,evidence=self._rhythm_entry_plan(req.symbol,data,now,req.manual_stop_mode)
            reference=bar.get('close') if req.limit_type in ('market','chase') else limit_px
            try:
                stop_px=self._manual_stop_price(data,req.side,reference,req.manual_stop_mode,now,evidence.get('sigma'))
            except ValueError as error:
                raise HTTPException(400,str(error))
        risk_budget = config.RISK_PER_TRADE_USD
        reference_px = limit_px
        risk_ref_px = 0.0
        reprice_qty = req.limit_type in ("market", "chase")
        if reprice_qty:
            risk_ref_px = (data.get("bar") or {}).get("close") or 0
            reference_px = risk_ref_px
        if opening and not market_entry and candidate_evidence is None and req.manual_stop_mode!='vwap_sigma':
            stop_px = opening_stop
            if not (0 < stop_px < reference_px if req.side == "long" else 0 < reference_px < stop_px):
                raise HTTPException(400, "开盘止损未形成有效风险距离，暂不能开仓")
        dist = abs(reference_px - stop_px)
        if not math.isfinite(dist) or reference_px <= 0 or stop_px <= 0 or dist <= 0:
            raise HTTPException(400, "风险距离无效（参考价/止损缺失），拒绝下单")
        qty = min(int(risk_budget / dist), config.RISK_MAX_ORDER_QTY)
        equity = self.context.accounts._equity_cache.get("equity") or 0
        position_cap = min(req.max_position_pct, config.RISK_MAX_SYMBOL_POSITION_PCT) if req.max_position_pct > 0 else config.RISK_MAX_SYMBOL_POSITION_PCT
        if equity > 0 and limit_px > 0:
            qty = min(qty, int(equity * position_cap / 100 / limit_px))
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
            market_direction_mode='candidate' if candidate_evidence else "rhythm" if rhythm_entry else req.market_direction_mode,
            entry_mode=req.entry_mode, rhythm_entry=rhythm_evidence,
            manual_stop_mode=req.manual_stop_mode if not opening_breakout else None,
            candidate_admission=candidate_evidence,
            candidate_order=req.candidate_order.dict() if req.candidate_order else None,
            range_direction_confirmed=req.range_direction_confirmed,
            market_direction_st_1m=market_direction,
        )
        if rhythm_entry:
            signal_snapshot.update(stop_source="market_2atr_outside_bar", rhythm_entry=rhythm_evidence,
                                   market_direction_mode="rhythm", stop_bar_time=bar["time"],
                                   stop_atr_interval_seconds=300, stop_atr_multiple=2,
                                   stop_atr_value=rhythm_evidence["stop_atr_value"])
        elif market_entry:
            signal_snapshot.update(stop_source="market_2atr_outside_bar", stop_bar_time=bar["time"],
                                   market_direction_st_1m=market_direction,
                                   market_direction_mode=req.market_direction_mode,
                                   range_direction_confirmed=req.range_direction_confirmed,
                                   manual_market_side=req.side if manual_direction else None)
        elif opening:
            signal_snapshot.update(stop_source="opening_bar_extreme", stop_bar_time=bar["time"])
        if not opening_breakout:
            signal_snapshot.update(manual_stop_mode=req.manual_stop_mode)
            if req.candidate_order is not None:
                signal_snapshot[POLICY_KEY] = req.candidate_order.dict()
            if candidate_evidence:
                signal_snapshot.update(candidate_admission=candidate_evidence,market_direction_mode='candidate')
                if req.manual_stop_mode=='atr_2':
                    signal_snapshot.update(stop_source='market_2atr_outside_bar',stop_atr_multiple=2,
                        stop_atr_interval_seconds=300,stop_bar_time=bar['time'])
            if req.manual_stop_mode=='vwap_sigma':
                signal_snapshot.update(stop_source='manual_vwap_sigma',stop_sigma_multiple=1,
                    stop_sigma_value=(candidate_evidence or rhythm_evidence or evidence)['sigma'])
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
            max_position_pct=position_cap,
            atr_mult=2.0 if rhythm_entry or candidate_evidence is not None else self.context.settings._stop_atr_mult(),
            trader=trader,
            risk_ref_px=risk_ref_px,
            manual_entry=True,
            entry_order_type="MKT" if opening_breakout or rhythm_entry else "",
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
