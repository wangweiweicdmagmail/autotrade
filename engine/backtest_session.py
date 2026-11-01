"""EMA9 金字塔单股单日步进回测，与实盘共用 PyramidEngine。"""

from datetime import datetime
from typing import List, Optional
from zoneinfo import ZoneInfo

import config
from engine.states import ACTIVE, DONE, PENDING
from engine.brokers import SimBroker
from engine.pine_execution import PineExecutionEngine
from engine.pine_models import PineModelRegistry

ET = ZoneInfo("America/New_York")


def _hm(ts: float) -> str:
    return datetime.fromtimestamp(ts, ET).strftime("%H:%M") if ts else ""


class BacktestSession:
    def __init__(self, rows: list, symbol: str, day: str,
                 strategy_name: str, params: dict, *, registry=None) -> None:
        self.model = (registry or PineModelRegistry.load()).get(strategy_name)
        self.symbol = symbol
        self.day = day
        self.strategy_name = strategy_name
        initial_risk = float(params.get("initial_risk_usd") or config.RISK_PER_TRADE_USD)
        if initial_risk <= 0:
            raise ValueError("首仓风险预算必须大于 0")
        auto_breakeven = bool(params.get("pyramid_auto_breakeven",
                                         (config.PYRAMID_AUTO_BREAKEVEN if strategy_name == "ema9_pyramid" else self.model.profile.auto_breakeven)))
        breakeven_r = float(params.get("pyramid_breakeven_r",
                                       config.PYRAMID_BREAKEVEN_R))
        add_unlock_r = float(params.get("pyramid_add_unlock_r",
                                        config.PYRAMID_ADD_UNLOCK_R))
        if breakeven_r <= 0 or add_unlock_r <= 0:
            raise ValueError("保本和加仓解锁 R 门槛必须大于 0")
        self.equity = float(params.get("equity") or 0)
        self.params = {
            "initial_risk_usd": initial_risk,
            "risk_ratios": list(self.model.profile.risk_ratios),
            "max_entries": len(self.model.profile.risk_ratios),
            "equity": self.equity,
            "pyramid_auto_breakeven": auto_breakeven,
            "pyramid_breakeven_r": breakeven_r,
            "pyramid_add_unlock_r": add_unlock_r,
        }
        self._day_rows = []
        for row in rows:
            dt = datetime.fromtimestamp(row["time"], ET)
            if dt.date().isoformat() == day and 9 * 60 + 30 <= dt.hour * 60 + dt.minute < 16 * 60:
                self._day_rows.append(row)
        self._cursor = 0
        self._prev_row: Optional[dict] = None
        self.broker = SimBroker()
        self._events: List[dict] = []
        self._bar_time_now = 0.0
        start_direction = (self.model.from_market(self._day_rows[0]).direction if self._day_rows else 0)
        self.params["start_direction"] = start_direction
        self.engine = PineExecutionEngine(
            self.broker, emit=self._on_event, model=self.model,
            breakeven_enabled=auto_breakeven,
            breakeven_r=breakeven_r, add_unlock_r=add_unlock_r)
        self.engine.start([symbol], initial_risk, {symbol: start_direction})
        self._done = False
        self._settled = False

    def _on_event(self, etype: str, **data) -> None:
        self._events.append({"type": etype, "bar_time": self._bar_time_now,
                             "hm": _hm(self._bar_time_now), **data})

    @property
    def total_bars(self) -> int:
        return len(self._day_rows)

    @property
    def done(self) -> bool:
        return self._done

    async def step(self, n: int = 1) -> dict:
        start = len(self._events)
        for _ in range(max(1, n)):
            if self._done:
                break
            await self._step_one()
        return {"new_events": self._events[start:], **self.state()}

    async def run_all(self) -> dict:
        start = len(self._events)
        while not self._done:
            await self._step_one()
        return {"new_events": self._events[start:], **self.state()}

    async def _step_one(self) -> None:
        if self._cursor >= self.total_bars:
            await self._settle_eod()
            return
        row = self._day_rows[self._cursor]
        self._cursor += 1
        bar = {k: row[k] for k in ("time", "open", "high", "low", "close", "volume")}
        self._bar_time_now = bar["time"]
        dt = datetime.fromtimestamp(bar["time"], ET)
        hm = dt.hour * 60 + dt.minute
        await self.engine.on_price_extremes(
            self.symbol, bar["low"], bar["high"], row.get(self.model.realtime_key) or {}, hm)
        await self.engine.on_bar(self.symbol, bar, row.get(self.model.realtime_key) or {})
        self._prev_row = row
        if self._cursor >= self.total_bars:
            await self._settle_eod()

    async def _settle_eod(self) -> None:
        if self._settled or self._prev_row is None:
            self._done = True
            return
        self._settled = True
        close = self._prev_row["close"]
        for inst in self.engine._instances.values():
            for lot_id in list(inst.lot_ids):
                lot = self.broker.get(lot_id)
                if lot is None:
                    continue
                if lot.state == ACTIVE:
                    await self.broker.close(lot_id, close, "eod", ts=self._prev_row["time"])
                elif lot.state == PENDING:
                    await self.broker.cancel(lot_id)
            await self.engine._refresh(inst)
        self._done = True

    def orders(self) -> List[dict]:
        orders = {}
        for ev in self._events:
            lot_id = ev.get("lot_id")
            if not lot_id:
                continue
            order = orders.setdefault(lot_id, {
                "lot_id": lot_id, "symbol": self.symbol, "side": ev.get("side", ""),
                "qty": 0, "state": "", "placed": None, "filled": None,
                "closed": None, "trails": [],
            })
            if ev["type"] == "pyramid_order_placed":
                order.update({
                    "side": ev["side"], "qty": ev["qty"], "state": PENDING,
                    "entries": ev["entries"], "risk_budget": ev["risk_budget"],
                    "placed": {"t": ev["bar_time"], "hm": ev["hm"],
                               "limit": ev["entry_px"], "stop": ev["stop_px"]},
                })
        out = []
        for lot_id, order in orders.items():
            lot = self.broker.get(lot_id)
            if lot is None:
                continue
            if lot.avg_fill > 0:
                order["filled"] = {"t": lot.fill_time, "hm": _hm(lot.fill_time),
                                   "px": lot.avg_fill}
            order["state"] = lot.state
            if lot.state == DONE:
                order["closed"] = {"t": lot.exit_time, "hm": _hm(lot.exit_time),
                                   "px": lot.exit_px, "reason": lot.exit_reason}
                risk = abs(lot.entry_px - (lot.init_stop_px or lot.stop_px))
                denom = risk * lot.qty if lot.qty else 0
                order["pnl"] = lot.realized_pnl
                order["rr"] = round(lot.realized_pnl / denom, 3) if denom else 0
                order["mfe_r"] = round(lot.mfe / denom, 2) if denom else 0
            out.append(order)
        out.sort(key=lambda o: (o.get("placed") or {}).get("t", 0))
        return out

    def _instances(self) -> list:
        rows = self.engine.instances()
        for row in rows:
            row["max_position_pct"] = (round(row["max_notional"] / self.equity * 100, 2)
                                       if self.equity > 0 else None)
        return rows

    def _timeline(self) -> list:
        out = []
        for ev in self._events:
            etype = ev["type"]
            if etype == "pyramid_signal":
                action = ev["action"]
                side = "多" if ev.get("side") == "long" else "空"
                if action in ("initial", "add"):
                    title = "首仓" if action == "initial" else f"第{ev['entries']}次加仓"
                    detail = (f"{side} EMA9 {ev['entry_px']:.2f} · 本次{ev['qty']}股"
                              f" · 预算${ev['risk_budget']:.2f} · 总{ev['qty_after']}股"
                              f"@{ev['avg_after']:.2f} · 止损{ev['stop_px']:.2f}")
                else:
                    title = "下单失败" if action == "order_failed" else "忽略信号"
                    detail = f"{side} EMA9 {ev['entry_px']:.2f} · {ev['reason']}"
                    if ev.get("qty_before"):
                        detail += (f" · 当前{ev['qty_before']}股@{ev['avg_before']:.2f}"
                                   f" · MFE {ev['mfe_r']:.2f}R")
                    if ev["reason"] == "第二次仓位未达到1R":
                        detail += f" · 第二仓MFE {ev['second_mfe_r']:.2f}R"
            elif etype == "pyramid_unlocked":
                title = f"{ev['unlock_r']:g}R解锁"
                detail = (f"MFE {ev['mfe_r']:.2f}R · {ev['qty']}股"
                          f"@{ev['avg_cost']:.2f} · 当前止损{ev['stop_px']:.2f}")
            elif etype == "pyramid_breakeven":
                title = f"{ev['breakeven_r']:g}R保本"
                detail = (f"MFE {ev['mfe_r']:.2f}R · {ev['qty']}股"
                          f"@{ev['avg_cost']:.2f} · 整组止损{ev['stop_px']:.2f}")
            elif etype == "pyramid_close_requested":
                title = "整组退出"
                detail = (f"{ev['reason']} · {ev['qty']}股@{ev['avg_cost']:.2f}"
                          f" · 退出价{ev['ref_px']:.2f}")
            else:
                continue
            out.append({"time": ev["bar_time"], "hm": ev["hm"],
                        "type": etype, "title": title, "detail": detail,
                        "reason": ev.get("reason", "")})
        return out

    def state(self) -> dict:
        cur = None
        if self._prev_row is not None:
            row = self._prev_row
            e9 = self.model.from_market(row).execution_values()
            cur = {
                "time": row["time"], "hm": _hm(row["time"]),
                "open": row["open"], "high": row["high"],
                "low": row["low"], "close": row["close"],
                "direction": e9.get("direction"), "ema_9": e9.get("entry_price"), "entry_price": e9.get("entry_price"),
                "touch": e9.get("entry_signal"), "atr": e9.get("atr"),
                "long_stop": e9.get("long_stop"), "short_stop": e9.get("short_stop"),
            }
        management = self._timeline()
        return {
            "symbol": self.symbol, "day": self.day,
            "strategy": self.strategy_name, "params": self.params,
            "cursor": self._cursor, "total_bars": self.total_bars,
            "done": self._done, "current_bar": cur,
            "instances": self._instances(), "orders": self.orders(),
            "management": management, "summary": self._summary(),
        }

    def _summary(self) -> dict:
        closed = [o for o in self.orders() if o.get("closed")]
        instances = self._instances()
        if not closed:
            return {"count": 0,
                    "max_position_pct": max((i["max_position_pct"] or 0 for i in instances), default=0)}
        wins = [o for o in closed if o.get("pnl", 0) > 0]
        return {
            "count": len(closed),
            "win_rate": round(len(wins) / len(closed) * 100, 1),
            "total_pnl": round(sum(o.get("pnl", 0) for o in closed), 2),
            "avg_rr": round(sum(o.get("rr", 0) for o in closed) / len(closed), 2),
            "avg_mfe_r": round(sum(o.get("mfe_r", 0) for o in closed) / len(closed), 2),
            "max_mfe_r": round(max(o.get("mfe_r", 0) for o in closed), 2),
            "max_position_pct": max((i["max_position_pct"] or 0 for i in instances), default=0),
        }
