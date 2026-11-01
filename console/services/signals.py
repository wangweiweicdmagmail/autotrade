"""signals application functions and owned state. No console.app dependency."""

from typing import Optional
import asyncio
import time
from console.shared import logger


class SignalsService:
    def __init__(self, context):
        self.context = context

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
        long_rows, short_rows = ([], [])
        for symbol, data in market.items():
            ts = self.context.trading._ts_c(data)
            direction = ts.get("direction")
            close = (data.get("bar") or {}).get("close")
            if direction not in (1, -1) or close is None:
                continue
            pmh, pml = self.context.trading._attack_zone(data)
            if pm_filter and (
                not self.context.trading._premarket_pass(
                    "long" if direction == 1 else "short", close, pmh, pml
                )
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
            fvg = data.get("fvg_c") or data.get("fvg") or {}
            row = {
                "symbol": symbol,
                "ticker": symbol.split(":")[-1],
                "side": "long" if direction == 1 else "short",
                "trend_state": ts.get("trend_state"),
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
                "qualified": True,
            }
            (long_rows if direction == 1 else short_rows).append(row)
        long_rows.sort(key=lambda x: -(x["divergence"] or 0))
        short_rows.sort(key=lambda x: -(x["divergence"] or 0))
        return {"long": long_rows, "short": short_rows}

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
