"""EMA9 2R 解锁、3-2-1 金字塔自动策略。"""

import logging
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from engine.auto_engine import ACTIVE, DONE, PENDING, WATCHING

logger = logging.getLogger(__name__)

_TERMINAL = {"CLOSED", "CANCELLED", "FAILED", DONE}
_OPEN = {PENDING, ACTIVE, "CLOSING"}


@dataclass
class PyramidInstance:
    symbol: str
    enabled: bool = True
    state: str = WATCHING
    side: str = ""
    start_direction: int = 0          # 启动时锁定的方向：1 只多 / -1 只空 / 0 双向（旧实例兼容）
    lot_ids: List[str] = field(default_factory=list)
    pending_lot_id: Optional[str] = None
    entries: int = 0
    initial_risk_usd: float = 200.0
    initial_r: float = 0.0
    avg_cost: float = 0.0
    total_qty: int = 0
    stop_px: float = 0.0
    unlocked: bool = False
    unrealized_r: float = 0.0
    mfe_r: float = 0.0
    second_mfe_r: float = 0.0
    entry_risks: Dict[str, float] = field(default_factory=dict)
    max_notional: float = 0.0
    last_signal_bar: float = 0.0
    last_signal_event_bar: float = 0.0
    done_reason: str = ""
    close_attempt_at: float = 0.0
    source: str = "auto"
    created_at: float = field(default_factory=time.time)

    def view(self) -> dict:
        return {
            "inst_id": f"pyramid-{self.symbol}", "symbol": self.symbol,
            "strategy": "ema9_pyramid", "state": self.state, "side": self.side,
            "start_direction": self.start_direction,
            "lot_id": self.lot_ids[0] if self.lot_ids else None,
            "lot_ids": list(self.lot_ids), "entry_px": round(self.avg_cost, 4),
            "avg_cost": round(self.avg_cost, 4), "qty": self.total_qty,
            "stop_px": self.stop_px, "entries": self.entries,
            "initial_risk_usd": self.initial_risk_usd,
            "risk_budgets": [round(self.initial_risk_usd * ratio, 2)
                             for ratio in (1.0, 2.0 / 3.0, 1.0 / 3.0)],
            "initial_r": round(self.initial_r, 4),
            "breakeven_done": self.unlocked, "unlocked": self.unlocked,
            "unrealized_r": self.unrealized_r, "mfe_r": self.mfe_r,
            "second_mfe_r": self.second_mfe_r,
            "max_notional": round(self.max_notional, 2),
            "done_reason": self.done_reason, "source": self.source,
            "created_at": self.created_at,
        }


class PyramidEngine:
    """一个标的一组仓位；实时触线建仓，2R 后加仓，最多三次。"""

    def __init__(self, broker, emit=None, max_instances: int = 5) -> None:
        self._broker = broker
        self._emit = emit or (lambda *_a, **_k: None)
        self._max_instances = max_instances
        self._instances: Dict[str, PyramidInstance] = {}
        self.enabled = False

    def start(self, symbols: List[str], initial_risk_usd: float,
              directions: Optional[dict] = None) -> List[str]:
        if initial_risk_usd <= 0:
            raise ValueError("首仓风险预算必须大于 0")
        started = []
        for symbol in symbols:
            inst = self._instances.get(symbol)
            if inst is not None and inst.state != DONE:
                inst.enabled = True
                inst.initial_risk_usd = float(initial_risk_usd)
                continue
            if len([i for i in self._instances.values() if i.state != DONE]) >= self._max_instances:
                break
            start_direction = int((directions or {}).get(symbol, 0)
                                  if directions is not None else 0)
            self._instances[symbol] = PyramidInstance(
                symbol=symbol, initial_risk_usd=float(initial_risk_usd),
                start_direction=start_direction)
            started.append(symbol)
            self._emit("pyramid_started", symbol=symbol,
                       initial_risk_usd=initial_risk_usd,
                       start_direction=start_direction)
        self.enabled = True
        return started

    def stop_symbol(self, symbol: str, close_position: bool = False) -> None:
        inst = self._instances.get(symbol)
        if inst is None:
            return
        inst.enabled = False
        if not inst.lot_ids:
            inst.state = DONE
        if close_position:
            inst.done_reason = "manual_stop_close"
        else:
            inst.done_reason = "manual_stop"

    async def _stop_close(self, inst: PyramidInstance, ref_px: float, reason: str) -> None:
        now = time.time()
        if inst.state == "CLOSING" and now - inst.close_attempt_at < 5.0:
            return
        if ref_px <= 0:
            ref_px = inst.avg_cost
        inst.close_attempt_at = now
        inst.state = "CLOSING"
        inst.unlocked = False
        errors = []
        for lot_id in list(inst.lot_ids):
            lot = self._broker.get(lot_id)
            if lot is None:
                continue
            if lot.state == PENDING:
                ok = await self._broker.cancel(lot_id)
            elif lot.state == ACTIVE:
                ok = await self._broker.close(lot_id, ref_px, reason)
            else:
                continue
            if not ok:
                errors.append(lot_id)
        self._emit("pyramid_close_requested", symbol=inst.symbol, reason=reason,
                   lot_ids=list(inst.lot_ids), qty=inst.total_qty,
                   avg_cost=inst.avg_cost, ref_px=ref_px,
                   ok=not errors, failed_lot_ids=errors)

    def instances(self) -> List[dict]:
        return [i.view() for i in self._instances.values()]

    def proposals(self) -> list:
        return []

    async def adopt_seed(self, symbol: str, lot: dict, initial_risk_usd: float,
                         e9: dict, enabled: bool = True) -> PyramidInstance:
        if initial_risk_usd <= 0:
            raise ValueError("首仓风险预算必须大于 0")
        existing = self._instances.get(symbol)
        if existing and lot["lot_id"] in existing.lot_ids:
            return existing
        if existing and existing.lot_ids:
            raise ValueError("已有金字塔仓，手动单不能静默并入")
        if lot.get("state") != ACTIVE:
            raise ValueError("仅接管已实际成交的 ACTIVE 手动仓")
        qty = int(lot.get("filled_qty") or 0)
        entry = float(lot.get("avg_fill") or lot.get("entry_px") or 0)
        stop = float(lot.get("stop_px") or 0)
        side = lot.get("side")
        direction = 1 if side == "long" else -1 if side == "short" else 0
        if qty <= 0 or entry <= 0 or stop <= 0:
            raise ValueError("手动仓成交数量、成本或止损无效")
        if not direction or e9.get("direction") != direction:
            raise ValueError("手动仓方向与当前 EMA9 策略方向不一致")
        if existing and existing.start_direction and existing.start_direction != direction:
            raise ValueError("手动仓方向与启动时锁定方向不一致")
        init_entry = float(lot.get("init_entry_px") or entry)
        init_stop = float(lot.get("init_stop_px") or stop)
        risk = abs(init_entry - init_stop)
        if risk <= 0:
            raise ValueError("手动仓缺少有效的首仓风险距离")
        if existing is None:
            if len([i for i in self._instances.values() if i.state != DONE]) >= self._max_instances:
                raise ValueError("策略实例已达上限")
            existing = PyramidInstance(symbol=symbol)
            self._instances[symbol] = existing
        existing.enabled = enabled
        existing.state = ACTIVE
        existing.side = side
        existing.start_direction = direction
        existing.lot_ids = [lot["lot_id"]]
        existing.pending_lot_id = None
        existing.entries = 1
        existing.initial_risk_usd = float(initial_risk_usd)
        existing.initial_r = risk
        existing.avg_cost = entry
        existing.total_qty = qty
        existing.stop_px = stop
        existing.unlocked = False
        existing.unrealized_r = 0.0
        existing.mfe_r = 0.0
        existing.second_mfe_r = 0.0
        existing.entry_risks = {lot["lot_id"]: risk}
        existing.max_notional = qty * entry
        existing.source = "manual_seed"
        existing.done_reason = ""
        if hasattr(self._broker, "mark_seed"):
            await self._broker.mark_seed(lot["lot_id"], risk, initial_risk_usd)
        self.enabled = self.enabled or enabled
        self._emit("pyramid_manual_adopted", symbol=symbol, side=side, qty=qty,
                   avg_cost=entry, initial_r=risk, lot_id=lot["lot_id"])
        return existing

    async def adopt_manual_lots(self, lots: List[dict], signals: Optional[dict] = None) -> List[str]:
        grouped = {}
        for lot in lots:
            meta = lot.get("signal_snapshot") or {}
            if ((lot.get("limit_type") == "auto_ema9_pyramid" or meta.get("pyramid_seed"))
                    and lot.get("state") in _OPEN):
                grouped.setdefault(lot["symbol"], []).append(lot)
        adopted = []
        for symbol, rows in grouped.items():
            if symbol in self._instances:
                continue
            rows.sort(key=lambda r: r.get("created_at", 0))
            inst = PyramidInstance(symbol=symbol, enabled=False)
            inst.lot_ids = [r["lot_id"] for r in rows]
            inst.entries = min(3, len(rows))
            first = rows[0]
            first_meta = first.get("signal_snapshot") or {}
            inst.start_direction = int(first_meta.get("start_direction") or (
                1 if first.get("side") == "long" else -1))
            inst.initial_risk_usd = float(
                first_meta.get("initial_risk_usd") or first.get("init_risk_usd") or 200.0)
            inst.initial_r = abs((first.get("init_entry_px") or first["entry_px"])
                                 - (first.get("init_stop_px") or first["stop_px"]))
            inst.stop_px = rows[-1]["stop_px"]
            for row in rows:
                meta = row.get("signal_snapshot") or {}
                inst.entry_risks[row["lot_id"]] = float(
                    meta.get("entry_risk_dist") or inst.initial_r)
            inst.pending_lot_id = next((r["lot_id"] for r in rows if r["state"] == PENDING), None)
            if (first.get("signal_snapshot") or {}).get("pyramid_seed"):
                inst.source = "manual_seed"
            self._instances[symbol] = inst
            await self._refresh(inst)
            adopted.append(symbol)
        for lot in lots:
            if lot.get("state") != ACTIVE or lot.get("limit_type") != "market":
                continue
            symbol = lot["symbol"]
            inst = self._instances.get(symbol)
            if inst is None or not inst.enabled or inst.lot_ids:
                continue
            e9 = ((signals or {}).get(symbol) or {}).get("e9") or {}
            try:
                await self.adopt_seed(symbol, lot, inst.initial_risk_usd, e9,
                                      enabled=True)
                adopted.append(symbol)
            except ValueError as e:
                logger.warning("手动市价首仓接管失败 %s: %s", symbol, e)
        return adopted

    async def _refresh(self, inst: PyramidInstance) -> None:
        if inst.state == DONE and not inst.lot_ids:
            return
        had_fill = any((self._broker.get(lot_id) is not None
                        and getattr(self._broker.get(lot_id), "filled_qty", 0) > 0)
                       for lot_id in inst.lot_ids)
        kept = []
        for lot_id in list(inst.lot_ids):
            lot = self._broker.get(lot_id)
            if lot is None:
                continue
            if lot.state in _TERMINAL:
                if not getattr(lot, "filled_qty", 0) and inst.entries > 0:
                    inst.entries -= 1
                if inst.pending_lot_id == lot_id:
                    inst.pending_lot_id = None
                continue
            kept.append(lot_id)
            if lot.state == ACTIVE and inst.pending_lot_id == lot_id:
                inst.pending_lot_id = None
        inst.lot_ids = kept
        qty = 0
        cost = 0.0
        for lot_id in kept:
            lot = self._broker.get(lot_id)
            if lot.state != ACTIVE:
                continue
            lot_qty = int(getattr(lot, "filled_qty", 0) or getattr(lot, "qty", 0))
            qty += lot_qty
            cost += (getattr(lot, "avg_fill", 0) or getattr(lot, "entry_px", 0)) * lot_qty
        inst.total_qty = qty
        if qty:
            inst.avg_cost = cost / qty
        if not kept:
            side = inst.side
            if had_fill and (not inst.enabled or inst.start_direction):
                inst.state = DONE
                inst.done_reason = inst.done_reason or "round_finished"
                inst.total_qty = 0
                inst.pending_lot_id = None
                inst.unlocked = False
                inst.unrealized_r = 0.0
                if side:
                    self._emit("pyramid_flat", symbol=inst.symbol, side=side)
                return
            inst.state = WATCHING if inst.enabled else DONE
            inst.side = ""
            inst.source = "auto"
            inst.close_attempt_at = 0.0
            inst.entries = 0
            inst.initial_r = 0.0
            inst.avg_cost = 0.0
            inst.total_qty = 0
            inst.stop_px = 0.0
            inst.unlocked = False
            inst.unrealized_r = 0.0
            inst.mfe_r = 0.0
            inst.second_mfe_r = 0.0
            inst.entry_risks.clear()
            inst.pending_lot_id = None
            if side:
                self._emit("pyramid_flat", symbol=inst.symbol, side=side)
        elif inst.pending_lot_id:
            inst.state = PENDING
        elif inst.state != "CLOSING":
            inst.state = ACTIVE

    async def _move_all_stops(self, inst: PyramidInstance, new_stop: float) -> bool:
        ok = True
        for lot_id in inst.lot_ids:
            lot = self._broker.get(lot_id)
            if lot is not None and lot.state == ACTIVE:
                ok = bool(await self._broker.move_stop(lot_id, round(new_stop, 2))) and ok
        if ok:
            inst.stop_px = round(new_stop, 2)
        return ok

    async def _place(self, inst: PyramidInstance, e9: dict, now_hm: int) -> None:
        if (not self.enabled or not inst.enabled or inst.pending_lot_id
                or inst.state in ("CLOSING", DONE)):
            return
        if e9.get("touch") != 1:
            return
        bar_time = e9.get("time") or 0
        if bar_time and inst.last_signal_event_bar == bar_time:
            return
        inst.last_signal_event_bar = bar_time
        direction = e9.get("direction")
        side = "long" if direction == 1 else "short" if direction == -1 else ""
        ema = e9.get("ema_9")
        before_qty, before_avg = inst.total_qty, inst.avg_cost
        mfe = inst.mfe_r

        def signal(action: str, reason: str, **details) -> None:
            self._emit("pyramid_signal", symbol=inst.symbol, side=side,
                       entry_px=ema, action=action, reason=reason,
                       qty_before=before_qty, avg_before=before_avg,
                       mfe_r=mfe, second_mfe_r=inst.second_mfe_r, **details)

        if not 9 * 60 + 45 <= now_hm < 15 * 60:
            signal("ignored", "非进场时段")
            return
        signal_stop = e9.get("long_stop") if side == "long" else e9.get("short_stop")
        if inst.start_direction and direction != inst.start_direction:
            signal("ignored", "非启动时锁定方向")
            return
        if not side or not ema or ema <= 0 or not signal_stop or signal_stop <= 0:
            signal("ignored", "方向或价格数据缺失")
            return
        if before_qty and side != inst.side:
            signal("ignored", "反向信号，不加仓")
            return
        if before_qty and not inst.unlocked:
            signal("ignored", "平均成本未达到2R")
            return
        second_active = (len(inst.lot_ids) >= 2
                         and self._broker.get(inst.lot_ids[1]) is not None
                         and self._broker.get(inst.lot_ids[1]).state == ACTIVE)
        if before_qty and inst.entries == 2 and (not second_active
                                                or inst.second_mfe_r + 1e-9 < 1.0):
            signal("ignored", "第二次仓位未达到1R")
            return
        if before_qty and inst.entries >= 3:
            signal("ignored", "已达到三次建仓上限")
            return
        budget = inst.initial_risk_usd * (1.0, 2.0 / 3.0, 1.0 / 3.0)[inst.entries]
        risk_dist = abs(ema - signal_stop)
        qty = int(budget / risk_dist) if risk_dist > 0 else 0
        order_stop = signal_stop if not before_qty else inst.stop_px
        if qty <= 0:
            signal("ignored", "风险预算不足一股")
            return
        inst.last_signal_bar = bar_time
        try:
            lot = await self._broker.place_limit_stop(
                symbol=inst.symbol, side=side, qty=qty,
                limit_px=round(ema, 2), stop_px=round(order_stop, 2),
                limit_type="auto_ema9_pyramid",
                reason=f"EMA9金字塔 第{inst.entries + 1}次 预算${budget:.2f}",
                meta={"entry_risk_dist": risk_dist, "entry_index": inst.entries + 1,
                      "start_direction": inst.start_direction})
        except Exception as e:
            logger.warning("金字塔下单失败 %s: %s", inst.symbol, e)
            signal("order_failed", str(e))
            self._emit("pyramid_order_failed", symbol=inst.symbol, error=str(e))
            return
        if not before_qty:
            inst.side = side
            inst.source = "auto"
            inst.initial_r = risk_dist
            inst.stop_px = round(order_stop, 2)
            inst.max_notional = 0.0
        inst.unrealized_r = 0.0
        inst.mfe_r = 0.0
        inst.entries += 1
        inst.entry_risks[lot.lot_id] = risk_dist
        if inst.entries == 2:
            inst.second_mfe_r = 0.0
        inst.pending_lot_id = lot.lot_id
        inst.lot_ids.append(lot.lot_id)
        inst.unlocked = False
        inst.state = PENDING
        after_qty = before_qty + qty
        after_avg = ((before_avg * before_qty + ema * qty) / after_qty
                     if after_qty else ema)
        signal("add" if before_qty else "initial", "2R已解锁" if before_qty else "首次有效触线",
               entries=inst.entries, qty=qty, qty_after=after_qty,
               avg_after=after_avg, risk_budget=budget, stop_px=order_stop,
               lot_id=lot.lot_id)
        self._emit("pyramid_order_placed", symbol=inst.symbol, side=side, qty=qty,
                   entry_px=ema, stop_px=order_stop, entries=inst.entries,
                   risk_budget=budget, lot_id=lot.lot_id)

    async def on_price(self, symbol: str, price: float, e9: dict, now_hm: int) -> None:
        inst = self._instances.get(symbol)
        if inst is None or price <= 0:
            return
        await self._refresh(inst)
        if inst.total_qty and inst.initial_r > 0:
            sign = 1 if inst.side == "long" else -1
            inst.unrealized_r = round((price - inst.avg_cost) * sign / inst.initial_r, 3)
            inst.mfe_r = max(inst.mfe_r, inst.unrealized_r)
            inst.max_notional = max(inst.max_notional, inst.total_qty * price)
            if inst.entries >= 2 and len(inst.lot_ids) >= 2:
                second = self._broker.get(inst.lot_ids[1])
                second_risk = inst.entry_risks.get(inst.lot_ids[1], 0.0)
                if second is not None and second.state == ACTIVE and second_risk > 0:
                    second_entry = getattr(second, "avg_fill", 0) or second.entry_px
                    second_r = (price - second_entry) * sign / second_risk
                    second_qty = int(getattr(second, "filled_qty", 0)
                                     or getattr(second, "qty", 0))
                    historical_r = (float(getattr(second, "mfe", 0.0))
                                    / (second_risk * second_qty) if second_qty else 0.0)
                    inst.second_mfe_r = max(inst.second_mfe_r, round(second_r, 3),
                                            round(historical_r, 3))
            if not inst.unlocked and inst.mfe_r >= 2.0:
                if await self._move_all_stops(inst, inst.avg_cost):
                    inst.unlocked = True
                    self._emit("pyramid_unlocked", symbol=inst.symbol,
                               avg_cost=inst.avg_cost, mfe_r=inst.mfe_r,
                               qty=inst.total_qty, stop_px=inst.stop_px)
        await self._place(inst, e9, now_hm)

    async def on_price_extremes(self, symbol: str, low: float, high: float,
                                e9: dict, now_hm: int) -> None:
        inst = self._instances.get(symbol)
        side = inst.side if inst and inst.side else (
            "long" if e9.get("direction") == 1 else "short")
        first, second = (high, low) if side == "long" else (low, high)
        for price in (first, second):
            if price and price > 0:
                if hasattr(self._broker, "on_price"):
                    self._broker.on_price(symbol, price, ts=e9.get("time") or 0)
                await self.on_price(symbol, price, e9, now_hm)
        if hasattr(self._broker, "on_price"):
            touch = low if side == "long" else high
            if touch:
                self._broker.on_price(symbol, touch, ts=e9.get("time") or 0)
                self._broker.on_price(symbol, touch, ts=e9.get("time") or 0)

    async def on_bar(self, symbol: str, bar: dict, e9_c: dict) -> None:
        inst = self._instances.get(symbol)
        if inst is None:
            return
        await self._refresh(inst)
        if not inst.enabled and inst.done_reason == "manual_stop_close":
            await self._stop_close(inst, bar.get("close") or 0, "manual_stop")
            return
        if inst.total_qty:
            direction = e9_c.get("direction")
            flipped = ((inst.side == "long" and direction == -1)
                       or (inst.side == "short" and direction == 1))
            if flipped:
                await self._stop_close(inst, bar.get("close") or 0, "trend_reverse")
