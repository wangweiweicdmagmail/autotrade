"""Historical Pine-output replay using the common execution engine and SimBroker."""

from datetime import datetime
import hashlib
import json
from pathlib import Path
from typing import List, Optional
from zoneinfo import ZoneInfo

import config
from engine.states import ACTIVE, DONE, PENDING
from engine.brokers import SimBroker
from engine.pine_execution import PineExecutionEngine
from engine.pine_models import PineModelRegistry
from domain.contracts import ClosedBar

ET = ZoneInfo("America/New_York")


def _hm(ts: float) -> str:
    return datetime.fromtimestamp(ts, ET).strftime("%H:%M") if ts else ""


class BacktestSession:
    def __init__(self, rows: list, symbol: str, day: str,
                 strategy_name: str, params: dict, *, registry=None,
                 start_time: Optional[str] = None) -> None:
        self.model = (registry or PineModelRegistry.load()).get(strategy_name)
        self.symbol = symbol
        self.day = day
        self.strategy_name = strategy_name
        self.start_time = start_time
        start_stamp = None
        if start_time is not None:
            try:
                start_dt = datetime.strptime(f"{day} {start_time}", "%Y-%m-%d %H:%M").replace(tzinfo=ET)
            except ValueError as error:
                raise ValueError("启动时间应为美东时间 HH:mm") from error
            start_stamp = start_dt.timestamp()
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
        prior_rows = []
        for row in sorted(rows, key=lambda item: item["time"]):
            dt = datetime.fromtimestamp(row["time"], ET)
            if start_stamp is not None and row["time"] + 5 * 60 <= start_stamp:
                prior_rows.append(row)
            if dt.date().isoformat() == day and 9 * 60 + 30 <= dt.hour * 60 + dt.minute < 16 * 60:
                if start_stamp is None or row["time"] >= start_stamp:
                    self._day_rows.append(row)
        self._prior_rows = prior_rows
        self._model_revision = hashlib.sha256(json.dumps(
            {**self.model.describe(), "profile": self.model.profile.__dict__},
            sort_keys=True,
        ).encode()).hexdigest()[:16]
        execution_source = Path(__file__).with_name("pine_execution.py").read_bytes()
        broker_source = (Path(__file__).parent / "brokers" / "simulated.py").read_bytes()
        self._execution_revision = hashlib.sha256(execution_source + b"\n" + broker_source + Path(__file__).read_bytes()).hexdigest()[:16]
        self._data_revision = hashlib.sha256(json.dumps(self._day_rows, sort_keys=True).encode()).hexdigest()[:16]
        self._cursor = 0
        self._prev_row: Optional[dict] = None
        self._replay_states = []
        self.broker = SimBroker()
        self._events: List[dict] = []
        self._bar_time_now = 0.0
        if start_stamp is not None:
            # At manual startup only a completed bar is available, never a future
            # final value from the first bar that will later be replayed.
            start_direction = self.model.from_market(prior_rows[-1]).direction if prior_rows else 0
        else:
            # Preserve the legacy step API's historical startup behavior.
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
        closed_bar = ClosedBar.from_mapping(self.symbol, row)
        bar = closed_bar.values()
        self._bar_time_now = bar["time"]
        dt = datetime.fromtimestamp(bar["time"], ET)
        hm = dt.hour * 60 + dt.minute
        await self.engine.on_price_extremes(
            self.symbol, bar["low"], bar["high"], row.get(self.model.realtime_key) or {}, hm)
        historical_output = self.model.from_market(row).for_bar(closed_bar.open_time, confirmed=True)
        await self.engine.on_confirmed_bar(closed_bar, historical_output)
        inst = self.engine.get_instance(self.symbol)
        self._replay_states.append({
            "time": row["time"], "hm": _hm(row["time"]), "close": row["close"],
            **{key: value for key, value in self.model.from_market(row).execution_values().items() if key != "time"},
            "execution_stop": inst.stop_px if inst and inst.total_qty else None,
        })
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
                    "planned_risk": ev.get("planned_risk_usd", round(abs(ev["entry_px"] - ev["stop_px"]) * ev["qty"], 2)),
                    "entry_order_type": ev.get("entry_order_type", "LMT"),
                    "placed": {"t": ev["bar_time"], "hm": ev["hm"],
                               "limit": ev["entry_px"], "stop": ev["stop_px"],
                               "reference": ev["entry_px"],
                               "risk_reference": ev.get("risk_reference_price", ev["entry_px"]),
                               "order_type": ev.get("entry_order_type", "LMT")},
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
                risk = abs((lot.risk_reference_price or lot.entry_px) - (lot.init_stop_px or lot.stop_px))
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
                    detail = (f"{side} 入场参考价 {ev['entry_px']:.2f} · 本次{ev['qty']}股"
                              f" · 预算${ev['risk_budget']:.2f} · 总{ev['qty_after']}股"
                              f"@{ev['avg_after']:.2f} · 止损{ev['stop_px']:.2f}")
                else:
                    title = "下单失败" if action == "order_failed" else "忽略信号"
                    detail = f"{side} 入场参考价 {ev['entry_px']:.2f} · {ev['reason']}"
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
            "model_name": self.model.name,
            "execution_profile": self.model.execution_profile,
            "run_context": {
                "start_time": self.start_time,
                "timezone": "America/New_York", "bar_interval": "5m",
                "first_bar_time": _hm(self._day_rows[0]["time"]) if self._day_rows else None,
                "last_bar_time": _hm(self._day_rows[-1]["time"]) if self._day_rows else None,
                "startup_direction_source": "previous_completed_bar" if self._prior_rows else "unavailable" if self.start_time else "legacy_first_bar",
                "model_revision": self._model_revision,
                "execution_revision": self._execution_revision,
                "data_revision": self._data_revision,
                "indicator_id": self.model.indicator_id,
                "revision_basis": {
                    "model_revision": "model_declaration_and_execution_profile",
                    "execution_revision": "on_disk_python_source_files",
                    "data_revision": "post_start_historical_rows",
                },
            },
            "validation": self._validation(),
            "replay_bars": list(self._replay_states),
        }

    def _validation(self) -> dict:
        signals = []
        missing = 0
        for row in self._day_rows[:self._cursor]:
            raw = row.get(self.model.realtime_key) or {}
            if not set(self.model.fields.values()) <= raw.keys():
                missing += 1
            output = self.model.from_market(row)
            if output.entry_signal:
                signals.append({
                    "time": row["time"], "hm": _hm(row["time"]),
                    "direction": output.direction, "entry_price": output.entry_price,
                    "long_stop": output.long_stop, "short_stop": output.short_stop,
                    "errors": list(output.errors),
                })
        orders = self.orders()
        warnings = [
            "历史数据未接入 Attack 小区间输出，本次回测不验证实盘小区间入场过滤。",
            "历史输出配合 OHLC 极值路径模拟，不能重建启动时的逐笔行情或根内信号时点。",
            "Pine 入场使用 MKT，按信号处理时的模拟观察价成交，不等待回到 Pine 参考价；现有极值路径先观察多头最高价/空头最低价，可能显著影响入场数量和结果。",
            "未计手续费、滑点和跳空成交差异；未覆盖实盘 OrderManager/RiskGate 的全部账户风控。",
            "启动时间由人工选择，系统不判断该时机是否适合交易；收盘按最后一根回放 K 线收盘价结算。",
            "启动方向由最近已完成 K 线近似，可能与实盘启动瞬间未收盘指标的方向不同。",
        ]
        if missing:
            warnings.append(f"{missing} 根 K 线缺少所选策略模型输出，不能将无订单解释为策略无信号。")
        if self.start_time and not self._prior_rows:
            warnings.append("缺少启动前已完成 K 线，启动方向按未知处理，可能与实盘人工启动状态不同。")
        return {
            "signal_bars": len(signals),
            "invalid_signal_bars": sum(bool(s["errors"]) for s in signals),
            "missing_output_bars": missing,
            "placed_orders": len(orders),
            "filled_orders": sum(bool(o["filled"]) for o in orders),
            "cancelled_orders": sum(o["state"] == "CANCELLED" for o in orders),
            "signals": signals, "warnings": warnings,
        }

    def _summary(self) -> dict:
        closed = [o for o in self.orders() if o.get("closed")]
        instances = self._instances()
        if not closed:
            return {"count": 0, "win_rate": None, "total_pnl": 0,
                    "avg_rr": None, "avg_mfe_r": None, "max_mfe_r": None,
                    "max_position_pct": max((i["max_position_pct"] or 0 for i in instances), default=None) if self.equity > 0 else None}
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
