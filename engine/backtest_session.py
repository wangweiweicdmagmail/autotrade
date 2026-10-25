# -*- coding: utf-8 -*-
"""
回测工作台会话 (BacktestSession)
=================================

会话式单股单日回测：加载数据后可**单步（逐 bar）**或**一次性全跑**，
驱动与实盘同一个 StrategyEngine + SimBroker，并把引擎事件按 bar 时间
归集成**每笔订单的全生命周期记录**：

    挂单(时间/限价/止损/股数) → 成交(时间/价格) → 保本(时间/新止损)
    → ST 跟踪(时间/旧→新止损，可多次) → 止损/平仓(时间/价格/原因)
    → 最终盈亏 / RR / MFE_R

与 backtest_auto.py 同一口径：bar 内 tick 用极值建模（多 low / 空 high），
进场指标 = 上一根 bar 确认值，管理 = 本根收盘确认值。
"""

import logging
from datetime import datetime
from typing import List, Optional
from zoneinfo import ZoneInfo

import config
from engine.auto_engine import ACTIVE, DONE, PENDING, SimBroker, StrategyEngine
from engine.strategies import build_strategy
from execution.risk import RiskConfig, RiskGate

logger = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")
ENTRY_START_HM = 9 * 60 + 30


def _hm(ts: float) -> str:
    if not ts:
        return ""
    return datetime.fromtimestamp(ts, ET).strftime("%H:%M")


class BacktestSession:
    """单股单日的步进式回测会话。"""

    def __init__(self, rows: list, symbol: str, day: str,
                 strategy_name: str, params: dict) -> None:
        self.symbol = symbol
        self.day = day
        self.strategy_name = strategy_name
        # 当日 RTH bar 及全局索引（取上一根用）
        self._day_rows = []
        for i, row in enumerate(rows):
            d = datetime.fromtimestamp(row["time"], ET)
            if d.date().isoformat() == day:
                hm = d.hour * 60 + d.minute
                if ENTRY_START_HM <= hm < 16 * 60:
                    self._day_rows.append((i, row, hm))
        self._rows = rows
        self._cursor = 0                    # 下一个待处理 bar 的下标
        self._prev_row: Optional[dict] = None

        strategy = build_strategy(strategy_name, **params)
        self.params = strategy.params
        self.broker = SimBroker()
        self._events: List[dict] = []       # 全量事件（带 bar_time）
        self._bar_time_now = 0.0
        self.engine = StrategyEngine(
            self.broker, risk_gate=self._make_risk(), gateway=None,
            emit=self._on_event)
        self.engine.start(strategy, [symbol])
        self._done = False
        self._settled = False

    @staticmethod
    def _make_risk() -> RiskGate:
        return RiskGate(RiskConfig(
            per_trade_pct=config.RISK_PER_TRADE_PCT,
            per_trade_usd=config.RISK_PER_TRADE_USD,
            max_order_qty=config.RISK_MAX_ORDER_QTY,
            max_open_positions=config.RISK_MAX_OPEN_POSITIONS,
            daily_loss_limit_usd=config.RISK_DAILY_LOSS_LIMIT_USD,
        ))

    def _on_event(self, etype: str, **data) -> None:
        self._events.append({"type": etype, "bar_time": self._bar_time_now,
                             "hm": _hm(self._bar_time_now), **data})

    @property
    def total_bars(self) -> int:
        return len(self._day_rows)

    @property
    def done(self) -> bool:
        return self._done

    # ------------------------------------------------------------------
    # 步进
    # ------------------------------------------------------------------

    async def step(self, n: int = 1) -> dict:
        """前进 n 根 bar（到末尾自动 EOD 了结）。返回增量事件 + 全量状态。"""
        new_events_start = len(self._events)
        for _ in range(max(1, n)):
            if self._done:
                break
            await self._step_one()
        return {"new_events": self._events[new_events_start:],
                **self.state()}

    async def run_all(self) -> dict:
        """一次性跑完剩余全部 bar。"""
        new_events_start = len(self._events)
        while not self._done:
            await self._step_one()
        return {"new_events": self._events[new_events_start:],
                **self.state()}

    async def _step_one(self) -> None:
        if self._cursor >= self.total_bars:
            await self._settle_eod()
            return
        _, row, hm = self._day_rows[self._cursor]
        self._cursor += 1
        bar = {k: row[k] for k in ("time", "open", "high", "low", "close", "volume")}
        ts = row.get("ts") or {}
        self._bar_time_now = bar["time"]
        if self._prev_row is not None:
            prev_bar = {k: self._prev_row[k] for k in
                        ("time", "open", "high", "low", "close", "volume")}
            prev_ts = self._prev_row.get("ts") or {}
            await self.engine.on_bar_extremes(self.symbol, bar, prev_ts,
                                              prev_bar, hm,
                                              fvg_c=self._prev_row.get("fvg"))
        await self.engine.on_bar(self.symbol, bar, ts)
        self._prev_row = row
        if self._cursor >= self.total_bars:
            await self._settle_eod()

    async def _settle_eod(self) -> None:
        if self._settled or self._prev_row is None:
            self._done = True
            return
        self._settled = True
        last_close = self._prev_row["close"]
        last_bt = self._prev_row["time"]
        self._bar_time_now = last_bt
        for inst in self.engine._instances.values():
            if inst.state == DONE or not inst.lot_id:
                continue
            lot = self.broker.get(inst.lot_id)
            if lot is None:
                continue
            if lot.state == ACTIVE:
                await self.broker.close(inst.lot_id, last_close, "eod",
                                        ts=last_bt)
                self.engine._finish(inst, "eod")
            elif lot.state == PENDING:
                await self.broker.cancel(inst.lot_id)
                self.engine._finish(inst, "eod_cancel")
        self._done = True

    # ------------------------------------------------------------------
    # 订单生命周期（按 lot 归集事件 + 终态）
    # ------------------------------------------------------------------

    def orders(self) -> List[dict]:
        orders: dict = {}
        for ev in self._events:
            lid = ev.get("lot_id")
            if not lid:
                continue
            o = orders.setdefault(lid, {
                "lot_id": lid, "symbol": ev.get("symbol", self.symbol),
                "side": ev.get("side", ""), "qty": 0, "state": "",
                "placed": None, "filled": None, "expired": None,
                "breakeven": None, "trails": [], "closed": None,
            })
            t, hm = ev.get("bar_time", 0), ev.get("hm", "")
            et = ev["type"]
            if et == "auto_order_placed":
                o["placed"] = {"t": t, "hm": hm, "limit": ev.get("limit_px"),
                               "stop": ev.get("stop_px")}
                o["qty"] = ev.get("qty", 0)
                o["side"] = ev.get("side", o["side"])
                o["state"] = "PENDING"
            elif et == "auto_filled":
                o["filled"] = {"t": t, "hm": hm, "px": ev.get("avg_fill")}
                o["state"] = "ACTIVE"
            elif et == "auto_expired":
                o["expired"] = {"t": t, "hm": hm}
                o["state"] = "EXPIRED"
            elif et == "auto_breakeven":
                o["breakeven"] = {"t": t, "hm": hm, "stop": ev.get("new_stop"),
                                  "unrealized_r": ev.get("unrealized_r")}
            elif et == "auto_trail":
                o["trails"].append({"t": t, "hm": hm,
                                    "old": ev.get("old_stop"),
                                    "new": ev.get("new_stop")})
            elif et in ("auto_reverse_close",):
                o["closed"] = {"t": t, "hm": hm, "px": ev.get("ref_px"),
                               "reason": "trend_reverse"}
                o["state"] = "CLOSED"
        # 终态合并（SimBroker 为准：止损/平仓/EOD）
        out = []
        for lid, o in orders.items():
            lot = self.broker.get(lid)
            if lot is not None:
                # 同根「成交+止损」的订单：引擎 auto_filled 事件来不及发，
                # 用 lot 的成交记录补齐生命周期
                if o["filled"] is None and lot.avg_fill > 0:
                    o["filled"] = {"t": lot.fill_time, "hm": _hm(lot.fill_time),
                                   "px": lot.avg_fill}
                o["state"] = lot.state
                if lot.state == DONE:
                    o["closed"] = {"t": lot.exit_time, "hm": _hm(lot.exit_time),
                                   "px": lot.exit_px, "reason": lot.exit_reason}
                    # 风险基准 = 初始止损距离（跟踪移动不影响 RR 口径）
                    risk_amt = abs(lot.entry_px - (lot.init_stop_px or lot.stop_px))
                    denom = risk_amt * lot.qty if lot.qty > 0 else 0
                    o["pnl"] = lot.realized_pnl
                    o["rr"] = round(lot.realized_pnl / denom, 3) if denom else 0
                    o["mfe_r"] = round(lot.mfe / denom, 2) if denom else 0
            out.append(o)
        out.sort(key=lambda x: (x.get("placed") or {}).get("t", 0))
        return out

    # ------------------------------------------------------------------
    # 状态快照
    # ------------------------------------------------------------------

    def state(self) -> dict:
        cur = None
        if self._prev_row is not None:
            ts = self._prev_row.get("ts") or {}
            cur = {
                "time": self._prev_row["time"], "hm": _hm(self._prev_row["time"]),
                "open": self._prev_row["open"], "high": self._prev_row["high"],
                "low": self._prev_row["low"], "close": self._prev_row["close"],
                "trend_state": ts.get("trend_state"),
                "direction": ts.get("direction"),
                "ema_fast": ts.get("ema_fast"),
                "channel_edge": ts.get("channel_edge"),
                "supertrend": ts.get("supertrend"),
                "atr": ts.get("atr"),
                "divergence": ts.get("divergence"),
                "fvg": self._prev_row.get("fvg"),
            }
        return {
            "symbol": self.symbol, "day": self.day,
            "strategy": self.strategy_name, "params": self.params,
            "cursor": self._cursor, "total_bars": self.total_bars,
            "done": self._done,
            "current_bar": cur,
            "instances": self.engine.instances(),
            "orders": self.orders(),
            "summary": self._summary(),
        }

    def _summary(self) -> dict:
        closed = [o for o in self.orders() if o.get("closed")]
        n = len(closed)
        if not n:
            return {"count": 0}
        wins = [o for o in closed if o.get("pnl", 0) > 0]
        stopped = [o for o in closed if o["closed"].get("reason") == "stop"]
        no_1r = [o for o in stopped if o.get("mfe_r", 0) < 1]
        return {
            "count": n,
            "win_rate": round(len(wins) / n * 100, 1),
            "total_pnl": round(sum(o.get("pnl", 0) for o in closed), 2),
            "avg_rr": round(sum(o.get("rr", 0) for o in closed) / n, 2),
            "avg_mfe_r": round(sum(o.get("mfe_r", 0) for o in closed) / n, 2),
            "max_mfe_r": round(max(o.get("mfe_r", 0) for o in closed), 2),
            "no_1r_count": len(no_1r),
            "no_1r_pct": round(len(no_1r) / n * 100, 1),
        }
