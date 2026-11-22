"""signals application functions and owned state. No console.app dependency."""

from typing import Optional

from fastapi import HTTPException
import asyncio
import time
from datetime import datetime
from console.shared import logger
from console.schemas import OpeningBreakoutOrderRequest
from domain.contracts import finite_number
from engine.opening_range import beyond_opening_range, opening_range
from engine.opening_breakout import CURRENT_RULES, evaluate_breakout
from console.services.strategy_shadow import ET


BREAKOUT_CONFIRM_GRACE_SECONDS = 20


class SignalsService:
    _breakout_policy = CURRENT_RULES

    def __init__(self, context):
        self.context = context
        self._opening_breakout_state = {}
        self._opening_breakouts = []
        self._opening_breakout_day = None
        self._opening_previews = {}
        self._opening_orders = {}

    def opening_breakouts(self, now: float) -> list:
        if self._opening_breakout_day != datetime.fromtimestamp(now, ET).date():
            return []
        return sorted(self._opening_breakouts, key=lambda row: (-row["time"], row["symbol"]))

    def opening_breakout_coverage(self, symbols, now: float) -> dict | None:
        local = datetime.fromtimestamp(now, ET)
        first = int(local.replace(hour=9, minute=40, second=0, microsecond=0).timestamp())
        end = int(local.replace(hour=10, minute=30, second=0, microsecond=0).timestamp())
        target = min(int(now) // 300 * 300, end)
        if local.weekday() >= 5 or target <= first:
            return None
        pending = [
            symbol for symbol in symbols
            if self._opening_breakout_day != local.date()
            or self._opening_breakout_state.get(symbol, (0, None))[0] < target - 300
        ]
        return {"total": len(symbols), "pending": pending, "through": target}

    def _opening_breakout_bars(self, market: dict, now: float):
        local = datetime.fromtimestamp(now, ET)
        first = int(local.replace(hour=9, minute=40, second=0, microsecond=0).timestamp())
        end = int(local.replace(hour=10 if self._breakout_policy.morning_only else 16,
                                minute=30 if self._breakout_policy.morning_only else 0,
                                second=0, microsecond=0).timestamp())
        if local.weekday() >= 5:
            return
        for symbol, data in market.items():
            history = data.get("opening_breakout_history")
            if history is None:
                yield symbol, data, now
                continue
            previous = self._opening_breakout_state.get(symbol, (first - 300, None))[0]
            eligible = {
                row[0]: row for row in history
                if first <= row[0] < end and row[0] + 300 <= now and row[0] > previous
            }
            for time in range(previous + 300, min(int(now), end), 300):
                row = eligible.get(time)
                if row is None:
                    break
                _, open_px, high, low, close, sma, wilder, zone_high, zone_low = row[:9]
                body_ema = row[10] if len(row) > 10 else None
                engulfing = row[11] if len(row) > 11 else None
                context = row[12] if len(row) > 12 and isinstance(row[12], dict) else {}
                yield symbol, {
                    "bar": {"time": time + 300},
                    "bar_c": {"time": time, "open": open_px, "high": high, "low": low,
                              "close": close, "atr_sma10": sma, "body_ema10": body_ema, "prior_engulfing4": engulfing, **context},
                    "ts_c": {"time": time, "atr": wilder},
                    "attack_c": {"time": time, "opening3High": zone_high, "opening3Low": zone_low},
                }, time + 300
                if self._opening_breakout_state.get(symbol, (0, None))[0] != time:
                    break

    def _scan_opening_breakouts(self, market: dict, now: float) -> None:
        day = datetime.fromtimestamp(now, ET).date()
        if self._opening_breakout_day != day:
            self._opening_breakout_day = day
            self._opening_breakout_state.clear()
            self._opening_breakouts.clear()
            self._opening_previews.clear()
            self._opening_orders.clear()

        for symbol, data, as_of in self._opening_breakout_bars(market, now):
            zone = opening_range(data, as_of=as_of, include_session_close=True)
            if zone is None or zone[0] <= zone[1]:
                continue
            bar = data["bar_c"]
            bar_time = bar["time"]
            previous_time, previous_side = self._opening_breakout_state.get(symbol, (0, None))
            if bar_time <= previous_time:
                continue
            if self._breakout_policy.first_signal_only and any(
                row["symbol"] == symbol for row in self._opening_breakouts
            ):
                self._opening_breakout_state[symbol] = (bar_time, previous_side)
                continue
            evaluation = self._evaluate_opening_breakout(symbol, bar, data.get("ts_c") or {}, zone)
            if evaluation is None:
                continue
            side, signal = evaluation
            self._opening_breakout_state[symbol] = (
                bar_time, previous_side if side == previous_side else None,
            )
            if signal is None or side == previous_side:
                continue
            self._opening_breakout_state[symbol] = (bar_time, side)
            self._opening_breakouts.append(signal)
            self.context.events.record_event("opening_range_breakout", **signal)

    def _evaluate_opening_breakout(self, symbol, bar, study, zone):
        return evaluate_breakout(symbol, bar, study, zone, self._breakout_policy)

    def _opening_preview(self, symbol, data, now):
        zone = opening_range(data, as_of=now)
        if zone is None or zone[0] <= zone[1]:
            return None
        bar = data["bar"]
        if self._breakout_policy.first_signal_only and any(
            row["symbol"] == symbol for row in self._opening_breakouts
        ):
            return None
        # A preview cannot bypass an unprocessed earlier candle or the episode dedup.
        previous_time, previous_side = self._opening_breakout_state.get(symbol, (0, None))
        if previous_time != bar["time"] - 300:
            return None
        evaluation = self._evaluate_opening_breakout(symbol, bar, data.get("ts") or {}, zone)
        if evaluation and evaluation[0] != previous_side:
            return evaluation[1]
        return None

    def opening_breakout_display(self, now):
        confirmed = self.opening_breakouts(now)
        if self._opening_breakout_day != datetime.fromtimestamp(now, ET).date():
            return confirmed
        rows = {key: dict(row) for key, row in self._opening_previews.items()}
        for row in confirmed:
            rows[row["id"]] = {**row, "status": "confirmed"}
        for key, order in self._opening_orders.items():
            if key in rows:
                rows[key].update(status=order["status"], status_detail=order.get("detail"))
        return sorted(rows.values(), key=lambda row: (-row["time"], row["symbol"]))

    def prepare_opening_breakout(self, req, trader):
        now = time.time()
        if req.cancel:
            order = self._opening_orders.get(req.signal_id)
            if not order or order["trader"] != trader or order["status"] != "armed" or now >= order["time"]:
                raise HTTPException(409, "当前预约已无法撤销")
            del self._opening_orders[req.signal_id]
            return {"status": "cancelled"}
        row = self._opening_previews.get(req.signal_id)
        if not row or not now < row["time"] or row["status"] != "preview" or row["side"] != req.side:
            raise HTTPException(409, "该预选信号已失效或进入收盘确认")
        close_et = datetime.fromtimestamp(row["time"], ET)
        if close_et.hour >= 16:
            raise HTTPException(409, "该 K 线收盘时常规交易已结束，无法预约开仓")
        market = self.context.collector.latest()
        current = self._opening_preview(row["symbol"], market.get(row["symbol"], {}), now)
        if not current or current["id"] != row["id"] or current["side"] != row["side"]:
            raise HTTPException(409, "当前行情已不符合预选条件")
        if req.signal_id in self._opening_orders:
            raise HTTPException(409, "该信号已有预约")
        self._opening_orders[req.signal_id] = {
            "status": "armed", "side": row["side"], "time": row["time"],
            "trader": trader, "max_position_pct": req.max_position_pct,
        }
        return {"status": "armed"}

    async def _update_opening_previews(self, market, now):
        if self._opening_breakout_day != datetime.fromtimestamp(now, ET).date():
            return
        confirmed = {row["id"]: row for row in self._opening_breakouts}
        for key, preview in sorted(self._opening_previews.items(), key=lambda item: (item[1]["time"], -item[1].get("body_atr", 0), item[0])):
            if now < preview["time"]:
                preview["status"] = "preview_wait"
                continue
            signal = confirmed.get(key)
            processed = self._opening_breakout_state.get(preview["symbol"], (0, None))[0]
            order = self._opening_orders.get(key)
            if signal is None and processed < preview["time"] - 300 and now < preview["time"] + BREAKOUT_CONFIRM_GRACE_SECONDS:
                preview["status"] = "confirming"
                continue
            preview["status"] = "invalid" if signal is None else "confirmed"
            if not order or order["status"] != "armed":
                continue
            bar = (self.context.collector.latest().get(preview["symbol"], {}).get("bar") or {})
            if signal is None or signal["side"] != order["side"]:
                order.update(status="invalid", detail="收盘未确认预约方向，未下单")
            elif (time.time() >= signal["time"] + BREAKOUT_CONFIRM_GRACE_SECONDS
                  or bar.get("time") != signal["time"]):
                order.update(status="invalid", detail="收盘确认已超时，未下单")
            else:
                order["status"] = "submitting"
                try:
                    await self.context.trading.api_place_opening_breakout(
                        OpeningBreakoutOrderRequest(signal_id=key, max_position_pct=order["max_position_pct"]),
                        order["trader"], deadline=signal["time"] + BREAKOUT_CONFIRM_GRACE_SECONDS,
                    )
                except HTTPException as exc:
                    order.update(status="submission_failed", detail=str(exc.detail))
                except Exception:
                    logger.exception("小区间突破预约提交异常 %s", key)
                    order.update(status="submission_failed", detail="提交结果未确认，请查订单明细")
                else:
                    order["status"] = "submitted"

        for symbol, data in market.items():
            row = self._opening_preview(symbol, data, now)
            if row is None:
                continue
            order = self._opening_orders.get(row["id"])
            if order and order["side"] != row["side"]:
                continue
            self._opening_previews[row["id"]] = {**row, "status": "preview"}

    def _shadow_record_event(self, tr) -> None:
        """影子单建档事件（全类型统一出口）。"""
        self.context.events.record_event(
            "shadow_signal",
            shadow_id=tr.shadow_id,
            type=tr.signal_type,
            symbol=tr.symbol,
            ticker=tr.ticker,
            side=tr.side,
            fill_mode=tr.fill_mode,
            entry=tr.entry,
            stop=tr.stop,
            atr=tr.atr,
            bar_time=tr.bar_time,
            meta=tr.meta,
        )

    def _shadow_signal(
        self,
        signal_type: str,
        symbol: str,
        side: str,
        trigger_px: float,
        stop_px: float,
        atr: float,
        bar_time: float,
        meta: Optional[dict] = None,
        fill_mode: str = "market",
        limit_px: float = 0.0,
        dedup_key: Optional[str] = None,
    ):
        """统一影子建档（各信号源调用）：登记 + 写事件流。已建档返回 None。"""
        tr = self.context.shadow_tracker.record(
            signal_type,
            symbol,
            side,
            trigger_px,
            stop_px,
            atr,
            bar_time,
            meta=meta,
            fill_mode=fill_mode,
            limit_px=limit_px,
            dedup_key=dedup_key,
        )
        if tr is not None:
            self.context.signals._shadow_record_event(tr)
        return tr

    def _shadow_stop_for(self, data: dict, side: str, px: float, atr: float) -> float:
        """影子止损位：smart_stop 指标距离优先（所见即所得），缺失回退 1×ATR；
        返回 0 表示无法确定（tracker 还会再兜一次 1×ATR）。"""
        if atr and atr > 0 and px:
            try:
                dist = self.context.trading._smart_stop_dist(data, side, px, atr)
                stop = round(px + (-1 if side == "long" else 1) * dist, 2)
                if stop > 0:
                    return stop
            except Exception:
                pass
        return 0.0

    async def _shadow_loop(self) -> None:
        """统一影子跟踪循环（每 1s）：新收盘 5M bar 喂入 ShadowTracker——
        限价单触及成交判定 + 逐 bar 推演（MFE/MAE/止损/EOD 定稿）+ pullback
        回踩事件自检；事件与结算写入当日事件流（shadow_signal/shadow_settled）。
        外部信号源（FVG 事件/回踩/预警、突破、策略候选）在各自产出处建档。"""
        while True:
            await asyncio.sleep(1.0)
            try:
                market = self.context.collector.latest()
                if not market:
                    continue
                try:
                    await self.context.strategy_shadow.step(
                        market, datetime.now(ET),
                        stale=self.context.collector.data_health()["stale"],
                    )
                except Exception:
                    logger.exception("策略影子执行异常")
                events, settled = self.context.shadow_tracker.process(market)
                for tr in events:
                    self.context.events.record_event(
                        "pullback_event",
                        shadow_id=tr.shadow_id,
                        symbol=tr.symbol,
                        ticker=tr.ticker,
                        side=tr.side,
                        line_type=tr.line_type,
                        bar_time=tr.bar_time,
                        entry=tr.entry,
                        stop=tr.stop,
                        atr=tr.atr,
                    )
                    self.context.signals._shadow_record_event(tr)
                for tr in settled:
                    self.context.events.record_event(
                        "shadow_settled",
                        shadow_id=tr.shadow_id,
                        symbol=tr.symbol,
                        ticker=tr.ticker,
                        side=tr.side,
                        line_type=tr.line_type,
                        stopped=tr.stopped,
                        mfe_rr=tr.mfe_rr,
                        mfe_rr_eod=tr.mfe_rr_eod,
                        outcome=tr.outcome,
                        outcome_rr=tr.outcome_rr,
                    )
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.exception("影子回测循环异常: %s", e)

    def _fvg_record_event(self, sym: str, bt: float, data: dict) -> bool:
        """固化一根确认 bar 的 FVG 事件（如有）。返回是否有事件被固化。"""
        fvg_c = data.get("fvg_c") or {}
        event = fvg_c.get("fvg_event")
        if event not in (1, -1):
            return False
        top = fvg_c.get("fvg_last_top")
        bottom = fvg_c.get("fvg_last_bottom")
        if top is None or bottom is None:
            return False
        ts_c = data.get("ts_c") or data.get("ts") or {}
        atr = ts_c.get("atr")
        height = round(top - bottom, 4)
        ratio = round(height / atr, 2) if atr and atr > 0 else None
        side = "long" if event == 1 else "short"
        self.context.signals._fvg_events.append(
            {
                "id": f"{sym}:{bt}:{side}",
                "symbol": sym,
                "ticker": sym.split(":")[-1],
                "side": side,
                "bar_time": bt,
                "top": top,
                "bottom": bottom,
                "height": height,
                "atr": round(atr, 4) if atr else None,
                "ratio": ratio,
            }
        )
        logger.info(
            "FVG 事件固化: %s %s %.2f~%.2f 高 %.2f ratio %s",
            sym,
            side,
            bottom,
            top,
            height,
            ratio,
        )
        close = (data.get("bar_c") or {}).get("close")
        if close and atr:
            self.context.signals._shadow_signal(
                "fvg_event",
                sym,
                side,
                trigger_px=close,
                stop_px=self.context.signals._shadow_stop_for(data, side, close, atr),
                atr=atr,
                bar_time=bt,
                meta={"top": top, "bottom": bottom, "height": height, "ratio": ratio},
                dedup_key=f"fvg_event:{sym}:{int(bt)}:{side}",
            )
        return True

    async def _fvg_monitor_loop(self) -> None:
        """FVG 事件固化循环（每 1s）：某 symbol 的 bar_c.time 前进（= 新 5M bar
        收盘确认）时读 fvg_c.fvg_event，±1 即固化缺口边沿快照进环形缓冲。
        daemon 的 K线回调与 FVG Study 回调是独立通道，同拍可能读到旧 fvg_c：
        用 fvg_c.time == bar_c.time 校验对齐，不齐则挂起下一拍重试（最多 5 拍）。"""
        while True:
            await asyncio.sleep(1.0)
            try:
                market = self.context.collector.latest()
                if not market:
                    continue
                for sym, data in market.items():
                    bar_c = data.get("bar_c") or {}
                    bt = bar_c.get("time") or 0
                    if not bt:
                        continue
                    bar_rt = data.get("bar") or {}
                    fvg_rt = data.get("fvg") or {}
                    b_rt = bar_rt.get("time") or 0
                    if (
                        b_rt
                        and fvg_rt.get("time") == b_rt
                        and (fvg_rt.get("fvg_event") in (1, -1))
                    ):
                        side_rt = "long" if fvg_rt["fvg_event"] == 1 else "short"
                        warn_key = f"{sym}:{b_rt}:{side_rt}"
                        if warn_key not in self.context.signals._fvg_warned:
                            self.context.signals._fvg_warned[warn_key] = time.time()
                            top = fvg_rt.get("fvg_last_top")
                            bottom = fvg_rt.get("fvg_last_bottom")
                            ts_rt = data.get("ts") or data.get("ts_c") or {}
                            atr = ts_rt.get("atr")
                            height = (
                                round(top - bottom, 4)
                                if top is not None and bottom is not None
                                else None
                            )
                            ratio = (
                                round(height / atr, 2)
                                if height and atr and (atr > 0)
                                else None
                            )
                            w = {
                                "id": warn_key + ":warn",
                                "symbol": sym,
                                "ticker": sym.split(":")[-1],
                                "side": side_rt,
                                "bar_time": b_rt,
                                "top": top,
                                "bottom": bottom,
                                "height": height,
                                "atr": round(atr, 4) if atr else None,
                                "ratio": ratio,
                                "warn_time": time.time(),
                            }
                            self.context.signals._fvg_warnings.append(w)
                            self.context.events.record_event(
                                "fvg_warn",
                                **{
                                    k: w[k]
                                    for k in (
                                        "symbol",
                                        "ticker",
                                        "side",
                                        "bar_time",
                                        "top",
                                        "bottom",
                                        "height",
                                        "atr",
                                        "ratio",
                                    )
                                },
                            )
                            logger.info(
                                "FVG 预警: %s %s 缺口形成中 %.2f~%.2f ratio %s",
                                sym,
                                side_rt,
                                bottom or 0,
                                top or 0,
                                ratio,
                            )
                            close_rt = bar_rt.get("close")
                            if close_rt and atr:
                                self.context.signals._shadow_signal(
                                    "fvg_warn",
                                    sym,
                                    side_rt,
                                    trigger_px=close_rt,
                                    stop_px=self.context.signals._shadow_stop_for(
                                        data, side_rt, close_rt, atr
                                    ),
                                    atr=atr,
                                    bar_time=b_rt,
                                    meta={
                                        "top": top,
                                        "bottom": bottom,
                                        "height": height,
                                        "ratio": ratio,
                                        "forming": True,
                                    },
                                    dedup_key=f"fvg_warn:{warn_key}",
                                )
                    pend = self.context.signals._fvg_pending.get(sym)
                    if pend is not None:
                        p_bt, retries = pend
                        fvg_c = data.get("fvg_c") or {}
                        if fvg_c.get("time") == p_bt:
                            self.context.signals._fvg_pending.pop(sym, None)
                            self.context.signals._fvg_record_event(sym, p_bt, data)
                        elif retries <= 0:
                            self.context.signals._fvg_pending.pop(sym, None)
                            logger.warning(
                                "FVG 事件固化超时放弃: %s bar_time=%s（fvg_c 始终未对齐，事件丢失）",
                                sym,
                                p_bt,
                            )
                        else:
                            self.context.signals._fvg_pending[sym] = (p_bt, retries - 1)
                    prev = self.context.signals._fvg_last_bar.get(sym)
                    if prev is None:
                        self.context.signals._fvg_last_bar[sym] = bt
                        continue
                    if bt <= prev:
                        continue
                    self.context.signals._fvg_last_bar[sym] = bt
                    fvg_c = data.get("fvg_c") or {}
                    fvg_t = fvg_c.get("time")
                    if fvg_t is not None and fvg_t != bt:
                        self.context.signals._fvg_pending[sym] = (bt, 5)
                        continue
                    self.context.signals._fvg_record_event(sym, bt, data)
                for sym, data in market.items():
                    bar = data.get("bar") or {}
                    low, high = (bar.get("low"), bar.get("high"))
                    rt = bar.get("time") or 0
                    if low is None or high is None or (not rt):
                        continue
                    for ev in self.context.signals._fvg_events:
                        if ev["symbol"] != sym or ev.get("retested"):
                            continue
                        if ev["side"] == "long" and low <= ev["top"]:
                            retest_px = low
                        elif ev["side"] == "short" and high >= ev["bottom"]:
                            retest_px = high
                        else:
                            continue
                        ev["retested"] = True
                        self.context.signals._fvg_retests.append(
                            {
                                "id": f"{ev['id']}:rt",
                                "symbol": sym,
                                "ticker": ev["ticker"],
                                "side": ev["side"],
                                "bar_time": ev["bar_time"],
                                "top": ev["top"],
                                "bottom": ev["bottom"],
                                "height": ev["height"],
                                "atr": ev["atr"],
                                "ratio": ev["ratio"],
                                "retest_time": rt,
                                "retest_px": retest_px,
                            }
                        )
                        logger.info(
                            "FVG 首次回踩: %s %s %.2f~%.2f 触价 %.2f",
                            sym,
                            ev["side"],
                            ev["bottom"],
                            ev["top"],
                            retest_px,
                        )
                        edge = ev["top"] if ev["side"] == "long" else ev["bottom"]
                        atr_rt = ev.get("atr")
                        if edge and atr_rt:
                            self.context.signals._shadow_signal(
                                "fvg_retest",
                                sym,
                                ev["side"],
                                trigger_px=edge,
                                stop_px=self.context.signals._shadow_stop_for(
                                    data, ev["side"], edge, atr_rt
                                ),
                                atr=atr_rt,
                                bar_time=ev["bar_time"],
                                meta={
                                    "top": ev["top"],
                                    "bottom": ev["bottom"],
                                    "height": ev["height"],
                                    "ratio": ev["ratio"],
                                    "retest_px": retest_px,
                                },
                                fill_mode="limit",
                                limit_px=edge,
                                dedup_key=f"fvg_retest:{ev['id']}",
                            )
                cutoff = time.time() - self.context.signals.FVG_EVENT_TTL_SECONDS
                while (
                    self.context.signals._fvg_events
                    and self.context.signals._fvg_events[0]["bar_time"] < cutoff
                ):
                    self.context.signals._fvg_events.popleft()
                while (
                    self.context.signals._fvg_warnings
                    and self.context.signals._fvg_warnings[0]["warn_time"] < cutoff
                ):
                    self.context.signals._fvg_warnings.popleft()
                for k in [
                    k
                    for k, ts in self.context.signals._fvg_warned.items()
                    if ts < cutoff
                ]:
                    del self.context.signals._fvg_warned[k]
                while (
                    self.context.signals._fvg_retests
                    and self.context.signals._fvg_retests[0]["bar_time"] < cutoff
                ):
                    self.context.signals._fvg_retests.popleft()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.exception("FVG 监控循环异常: %s", e)

    def _feed_breakout(self, sym: str, bar_c: dict, data: dict, fed_times: set) -> None:
        """把一根已确认 bar（带对齐的 fvg_c.fvg_event）喂给突破跟踪器。"""
        fvg_c = data.get("fvg_c") or {}
        bar = {
            "time": bar_c.get("time"),
            "open": bar_c.get("open"),
            "high": bar_c.get("high"),
            "low": bar_c.get("low"),
            "close": bar_c.get("close"),
            "fvg_event": fvg_c.get("fvg_event") or 0,
        }
        fed_times.add(bar["time"])
        sig = self.context.breakout_tracker.on_bar(sym, bar)
        if sig:
            self.context.signals._emit_breakout(sig)

    def _emit_breakout(self, sig: dict) -> None:
        """突破信号出口：固化进缓冲（前端快照下发）+ 写当日事件流。"""
        sig = dict(sig)
        sig["id"] = f"{sig['symbol']}:{sig['confirm_epoch']}:breakout"
        sig["ts"] = time.time()
        self.context.signals._breakout_signals.append(sig)
        self.context.events.record_event(
            "breakout_signal",
            **{
                k: sig[k]
                for k in (
                    "symbol",
                    "ticker",
                    "side",
                    "confirm_hm",
                    "confirm_time",
                    "break_time",
                    "close",
                    "premarket_high",
                    "premarket_low",
                    "zone_width",
                    "crowd",
                    "triple",
                )
            },
        )
        logger.warning(
            "盘前突破信号: %s %s 确认 %s 突破 %s 收盘 %.2f 盘前 %.2f~%.2f（拥挤 %d%s）",
            sig["symbol"],
            sig["side"],
            sig["confirm_time"],
            sig["break_time"],
            sig["close"],
            sig["premarket_low"],
            sig["premarket_high"],
            sig["crowd"],
            "，三连确认" if sig["triple"] else "",
        )
        sym = sig["symbol"]
        side = "long" if sig["side"] == "站上高点" else "short"
        data = (self.context.collector.latest() or {}).get(sym, {})
        bar_c = data.get("bar_c") or {}
        close = (
            bar_c.get("close")
            if bar_c.get("time") == sig.get("confirm_epoch")
            else None
        ) or sig["close"]
        ts_c = data.get("ts_c") or data.get("ts") or {}
        atr = ts_c.get("atr")
        if close and atr:
            self.context.signals._shadow_signal(
                "breakout",
                sym,
                side,
                trigger_px=close,
                stop_px=self.context.signals._shadow_stop_for(data, side, close, atr),
                atr=atr,
                bar_time=sig["confirm_epoch"],
                meta={
                    "crowd": sig["crowd"],
                    "triple": sig["triple"],
                    "premarket_high": sig["premarket_high"],
                    "premarket_low": sig["premarket_low"],
                    "zone_width": sig["zone_width"],
                    "break_time": sig["break_time"],
                    "side_text": sig["side"],
                },
                dedup_key=f"breakout:{sig['id']}",
            )

    async def _breakout_loop(self) -> None:
        """盘前突破+FVG确认 流式信号（每 1s）：某 symbol 的 bar_c.time 前进
        （= 新 5M bar 收盘确认）时把这根已确认 bar 喂给 BreakoutTracker；
        fvg_c 与 bar_c 的 time 对齐校验同 FVG 固化（不齐挂起最多 5 拍）。
        同一拍全部 symbol 喂完后按 bar 时间调 flush 做拥挤终判出信号。
        盘前区间优先用「5M 进攻区域」指标（attack_c 与 bar_c 的 time 对齐
        且 pmh/pml 有效时注入，每天一次；不对齐本拍跳过下拍自然重试；
        门控 bar 时间 hm>=570——指标盘前输出是累积中的半成品，09:30 首根
        常规K线才锁定终值，盘前启动时提前注入会把半成品区间锁死一整天）——
        指标在 TV 侧自算盘前区间，盘中重启控制台也能拿到正确区间。"""
        from zoneinfo import ZoneInfo
        import datetime as _dt

        et = ZoneInfo("America/New_York")
        while True:
            await asyncio.sleep(1.0)
            try:
                market = self.context.collector.latest()
                now = time.time()
                self._scan_opening_breakouts(market, now)
                await self._update_opening_previews(market, now)
                if not market:
                    continue
                fed_times: set = set()
                for sym, data in market.items():
                    bar_c = data.get("bar_c") or {}
                    bt = bar_c.get("time") or 0
                    if not bt:
                        continue
                    atk = data.get("attack_c") or {}
                    pmh, pml = (atk.get("pmh"), atk.get("pml"))
                    dt_et = _dt.datetime.fromtimestamp(bt, et)
                    if (
                        atk.get("time") == bt
                        and pmh is not None
                        and (pml is not None)
                        and (pmh > pml > 0)
                        and (dt_et.hour * 60 + dt_et.minute >= 570)
                    ):
                        day = dt_et.strftime("%Y-%m-%d")
                        if self.context.signals._breakout_injected.get(sym) != day:
                            if self.context.breakout_tracker.set_premarket(
                                sym, pmh, pml, day
                            ):
                                self.context.signals._breakout_injected[sym] = day
                                logger.info(
                                    "盘前区间注入（进攻区域指标）: %s %.2f~%.2f",
                                    sym,
                                    pml,
                                    pmh,
                                )
                    pend = self.context.signals._breakout_pending.get(sym)
                    if pend is not None:
                        p_bt, p_bar, retries = pend
                        fvg_c = data.get("fvg_c") or {}
                        if fvg_c.get("time") == p_bt:
                            self.context.signals._breakout_pending.pop(sym, None)
                            self.context.signals._feed_breakout(
                                sym, p_bar, data, fed_times
                            )
                        elif retries <= 0:
                            self.context.signals._breakout_pending.pop(sym, None)
                            logger.warning(
                                "突破信号喂入超时放弃: %s bar_time=%s（fvg_c 始终未对齐）",
                                sym,
                                p_bt,
                            )
                        else:
                            self.context.signals._breakout_pending[sym] = (
                                p_bt,
                                p_bar,
                                retries - 1,
                            )
                    prev = self.context.signals._breakout_last_bar.get(sym)
                    if prev is None:
                        self.context.signals._breakout_last_bar[sym] = bt
                        continue
                    if bt <= prev:
                        continue
                    self.context.signals._breakout_last_bar[sym] = bt
                    fvg_c = data.get("fvg_c") or {}
                    fvg_t = fvg_c.get("time")
                    if fvg_t is not None and fvg_t != bt:
                        self.context.signals._breakout_pending[sym] = (
                            bt,
                            dict(bar_c),
                            5,
                        )
                        continue
                    self.context.signals._feed_breakout(sym, bar_c, data, fed_times)
                for t in sorted(fed_times):
                    for sig in self.context.breakout_tracker.flush(t):
                        self.context.signals._emit_breakout(sig)
                cutoff = time.time() - self.context.signals.FVG_EVENT_TTL_SECONDS
                while (
                    self.context.signals._breakout_signals
                    and self.context.signals._breakout_signals[0]["ts"] < cutoff
                ):
                    self.context.signals._breakout_signals.popleft()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.exception("盘前突破监控循环异常: %s", e)

    def compute_screen(self) -> dict:
        """横截面选股（双向）：SuperTrend 方向（ts_c.direction）1 进多头列、-1 进空头列；
        震荡（trend_state=0）但 ST 方向明确也列入。发散度仅作列内强→弱排序展示。
        盘前区间过滤（settings.screen_premarket_filter，默认关）：多头候选现价
        <= attack_c.pmh 剔除、空头候选现价 >= attack_c.pml 剔除；无 attack 数据
        的标的一律保留（不误杀）。现价 = collector 实时 bar close。"""
        market = self.context.collector.latest()
        pm_filter = self.context.settings._screen_premarket_filter()
        opening_filter = self.context.settings._screen_opening_range_filter()
        now = time.time()
        long_rows, short_rows = ([], [])
        for symbol, data in market.items():
            ts = self.context.trading._ts_c(data)
            direction = ts.get("direction")
            bar = data.get("bar") or {}
            close = bar.get("close")
            if direction not in (1, -1) or close is None:
                continue
            pmh, pml = self.context.trading._attack_zone(data)
            if pm_filter and (
                not self.context.trading._premarket_pass(
                    "long" if direction == 1 else "short", close, pmh, pml
                )
            ):
                continue
            zone = opening_range(data, as_of=now) if opening_filter else None
            if opening_filter:
                confirmed_close = (data.get("bar_c") or {}).get("close")
                if not (
                    beyond_opening_range(zone, direction, confirmed_close)
                    and beyond_opening_range(zone, direction, close)
                ):
                    continue
            divergence = ts.get("divergence")
            if divergence is None:
                ema_f, ema_s, atr = (
                    ts.get("ema_fast"),
                    ts.get("ema_slow"),
                    ts.get("atr"),
                )
                if ema_f is not None and ema_s is not None and atr and (atr > 0):
                    divergence = abs(ema_f - ema_s) / atr
            # This live plot already uses closed bars internally. Using e9_c
            # would shift the slope back by one more bar.
            ema = data.get("e9") or {}
            bar_time = finite_number(bar.get("time"))
            slope = (
                finite_number(ema.get("ema_slope_atr"))
                if bar_time is not None and finite_number(ema.get("time")) == bar_time
                else None
            )
            fvg = data.get("fvg_c") or data.get("fvg") or {}
            row = {
                "symbol": symbol,
                "ticker": symbol.split(":")[-1],
                "side": "long" if direction == 1 else "short",
                "trend_state": ts.get("trend_state"),
                "ema_slope_atr": slope,
                "divergence": round(abs(divergence), 2)
                if divergence is not None
                else None,
                "st_duration_bars": ts.get("st_duration_bars"),
                "st_push_count": ts.get("st_push_count"),
                "fvg_cum_count": fvg.get("fvg_cum_count"),
                "fvg_last_dir": fvg.get("fvg_last_dir"),
                "fvg_event": fvg.get("fvg_event"),
                "close": round(close, 2),
                "pmh": round(pmh, 2) if pmh is not None else None,
                "pml": round(pml, 2) if pml is not None else None,
                "opening_high": zone[0] if zone else None,
                "opening_low": zone[1] if zone else None,
                "qualified": True,
            }
            (long_rows if direction == 1 else short_rows).append(row)
        long_rows.sort(key=lambda x: -(x["divergence"] or 0))
        short_rows.sort(key=lambda x: -(x["divergence"] or 0))
        return {"long": long_rows, "short": short_rows}

    def compute_momentum(self) -> dict:
        """Rank confirmed EMA7 progress toward or beyond the opening range edges."""
        now = time.time()
        result = {"long": [], "short": [], "ready_count": 0,
                  "first_breaks": {"long": [], "short": []}, "first_break_ready": 0}
        for symbol, data in (self.context.collector.latest() or {}).items():
            zone = opening_range(data, as_of=now)
            history = data.get("ema7_closed") or []
            if not zone or zone[0] <= zone[1]:
                continue
            confirmed_time = data["bar_c"]["time"]
            high, low = zone
            first_by_side = {}
            opening_bar = datetime.fromtimestamp(confirmed_time, ET).replace(
                hour=9, minute=40, second=0, microsecond=0,
            ).timestamp()
            samples = int((confirmed_time - opening_bar) // 300) + 1
            if samples > 0 and len(history) >= samples:
                for index in reversed(range(samples)):
                    row = history[index]
                    stamp = confirmed_time - index * 300
                    ema = finite_number(row.get("ema"))
                    if finite_number(row.get("time")) != stamp or ema is None or ema <= 0:
                        first_by_side.clear()
                        break
                    if ema > high:
                        first_by_side.setdefault("long", stamp + 300)
                    elif ema < low:
                        first_by_side.setdefault("short", stamp + 300)
                else:
                    result["first_break_ready"] += 1
                    for side, stamp in first_by_side.items():
                        leaders = result["first_breaks"][side]
                        if leaders and stamp < leaders[0]["time"]:
                            leaders.clear()
                        if not leaders or stamp == leaders[0]["time"]:
                            leaders.append({"symbol": symbol, "ticker": symbol.split(":")[-1],
                                            "side": side, "time": stamp})
            if not history or finite_number(history[0].get("time")) != confirmed_time:
                continue
            latest_ema = finite_number(history[0].get("ema"))
            if latest_ema is None or latest_ema <= 0:
                continue
            half_width = (high - low) / 2
            position = (latest_ema - (high + low) / 2) / half_width
            momentum = None
            if len(history) >= 4:
                # The 15-minute lookback may include premarket bars.
                for index, row in enumerate(history[1:4], start=1):
                    ema = finite_number(row.get("ema"))
                    if (finite_number(row.get("time")) != confirmed_time - 300 * index
                            or ema is None or ema <= 0):
                        break
                else:
                    momentum = (latest_ema - ema) / half_width
            result["ready_count"] += 1
            side, direction = ("long", 1) if position >= 0 else ("short", -1)
            # Every sample compares the same confirmed bar's close and Pine EMA.
            success_count = None
            if len(history) >= 20:
                matches = 0
                for index, row in enumerate(history[:20]):
                    price = finite_number(row.get("close"))
                    ema = finite_number(row.get("ema"))
                    if (finite_number(row.get("time")) != confirmed_time - 300 * index
                            or price is None or price <= 0 or ema is None or ema <= 0):
                        break
                    matches += direction * (price - ema) > 0
                else:
                    success_count = matches
            close = finite_number(data["bar"].get("close"))
            result[side].append({
                "symbol": symbol, "ticker": symbol.split(":")[-1], "side": side,
                "position": position, "momentum": momentum,
                "breakout_distance": direction * position - 1,
                "ema7_success_count": success_count,
                "first_break_time": first_by_side.get(side),
                "outside": beyond_opening_range(zone, direction, close) if close is not None else None,
            })
        for side, direction in (("long", 1), ("short", -1)):
            result[side].sort(key=lambda row: (
                -row["breakout_distance"], row["momentum"] is None,
                -direction * (row["momentum"] or 0), row["symbol"],
            ))
        return result

    def _fvg_event_state(
        self, side: str, close: Optional[float], top: float, bottom: float
    ) -> str:
        """由现价实时计算缺口状态：open=未回踩 / retest=回踩中 / filled=已回补。
        多头：现价 ≤ top 回踩中，≤ bottom 已回补；空头镜像。"""
        if close is None:
            return "open"
        if side == "long":
            if close <= bottom:
                return "filled"
            if close <= top:
                return "retest"
            return "open"
        if close >= top:
            return "filled"
        if close >= bottom:
            return "retest"
        return "open"

    def _fvg_monitor_snapshot(self) -> dict:
        """FVG 监控快照：以全部 symbol 的最大 bar_c.time 为锚，向前按 300s 步进
        生成最近 12 个 bar 槽位（60 分钟），缓冲内事件实时计算回踩状态后入槽。
        盘前区间过滤（settings.fvg_premarket_filter，默认关）：多头事件现价
        <= attack_c.pmh 剔除、空头事件现价 >= attack_c.pml 剔除（槽位事件、
        首次回踩、预警同口径）；无 attack 数据的一律保留。现价=实时 bar close。"""
        market = self.context.collector.latest() or {}
        pm_filter = self.context.settings._fvg_premarket_filter()
        anchor = 0
        closes: dict = {}
        zones: dict = {}
        for sym, data in market.items():
            bt = (data.get("bar_c") or {}).get("time") or 0
            if bt > anchor:
                anchor = bt
            closes[sym] = (data.get("bar") or {}).get("close")
            zones[sym] = self.context.trading._attack_zone(data)
        if not anchor:
            return {"slots": [], "retests": [], "warnings": []}

        def _pm_ok(side: str, sym: str) -> bool:
            if not pm_filter:
                return True
            pmh, pml = zones.get(sym, (None, None))
            return self.context.trading._premarket_pass(side, closes.get(sym), pmh, pml)

        slot_map = {
            anchor - i * 300: {"bar_time": anchor - i * 300, "long": [], "short": []}
            for i in range(12)
        }
        oldest = anchor - 11 * 300
        for ev in self.context.signals._fvg_events:
            bt = ev["bar_time"]
            if bt < oldest or bt > anchor:
                continue
            if (
                ev.get("atr") is None
                or ev.get("top") is None
                or ev.get("bottom") is None
            ):
                continue
            if not _pm_ok(ev["side"], ev["symbol"]):
                continue
            slot = slot_map.get(bt)
            if slot is None:
                continue
            item = dict(ev)
            item["state"] = self.context.signals._fvg_event_state(
                ev["side"], closes.get(ev["symbol"]), ev["top"], ev["bottom"]
            )
            item["retested"] = bool(ev.get("retested"))
            slot[ev["side"]].append(item)
        slots = sorted(slot_map.values(), key=lambda s: -s["bar_time"])
        retests = sorted(
            (
                r
                for r in self.context.signals._fvg_retests
                if _pm_ok(r["side"], r["symbol"])
            ),
            key=lambda r: r.get("retest_time") or 0,
            reverse=True,
        )[:50]
        warnings = sorted(
            (
                w
                for w in self.context.signals._fvg_warnings
                if _pm_ok(w["side"], w["symbol"])
            ),
            key=lambda w: w.get("warn_time") or 0,
            reverse=True,
        )[:20]
        return {"slots": slots, "retests": retests, "warnings": warnings}

    async def api_market(self) -> dict:
        """TV 实时数据快照（5M K线 + trend_state）+ FVG 监控槽位（前端首屏用）。"""
        health = self.context.collector.data_health()
        return {
            "symbols": self.context.collector.latest(),
            "market_frame_id": self.context.collector.market_frame_id,
            "market_session": self.context.collector.market_session_status(),
            "market_audit": self.context.market_audit.status(),
            "research_archive": self.context.research_archive.status(),
            "age": health["content_age"],
            "transport_age": health["transport_age"],
            "bar_age": health["bar_age"],
            "stale": health["stale"],
            "stale_reason": health["reason"],
            "running": self.context.collector.running,
            "fvg_monitor": self.context.signals._fvg_monitor_snapshot(),
        }

    async def api_screen(self) -> dict:
        """横截面选股（双向）：trend_state 多空两列，按 EMA 发散度排序。"""
        return self.context.signals.compute_screen()
