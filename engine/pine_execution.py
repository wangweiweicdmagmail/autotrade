"""可复用 Pine 执行状态机；信号、方向和初始止损由指标输出。"""

import logging
import math
import time
import uuid
from dataclasses import asdict, dataclass, field, fields
from typing import Dict, List, Optional
from datetime import datetime
from zoneinfo import ZoneInfo

from engine.states import ACTIVE, DONE, PENDING, WATCHING
from engine.broker_protocols import TradeBroker, SimulatedPriceFeed
from engine.pine_models import PineModel, PineModelRegistry
from domain.contracts import ClosedBar, ExecutionGroupState, OrderPlan, PriceObservation, StrategyOutput, TradingGroupKey, TERMINAL_ORDER_VALUES, OPEN_ORDER_VALUES

logger = logging.getLogger(__name__)

DEFAULT_BREAKEVEN_R = 3.0
DEFAULT_ADD_UNLOCK_R = 2.0
_TERMINAL = TERMINAL_ORDER_VALUES
_OPEN = OPEN_ORDER_VALUES


@dataclass
class ExecutionInstance:
    symbol: str
    strategy_id: str = "ema9_pyramid"
    strategy_instance_id: str = "default"
    execution_profile: str = "ema9_legacy_pyramid"
    risk_ratios: tuple = (1.0, 2 / 3, 1 / 3)
    group_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    enabled: bool = True
    state: str = WATCHING
    side: str = ""
    start_direction: int = (
        0  # 启动时锁定的方向：1 只多 / -1 只空 / 0 双向（旧实例兼容）
    )
    lot_ids: List[str] = field(default_factory=list)
    pending_lot_id: Optional[str] = None
    entries: int = 0
    initial_risk_usd: float = 200.0
    initial_r: float = 0.0
    avg_cost: float = 0.0
    total_qty: int = 0
    stop_px: float = 0.0
    unlocked: bool = False
    breakeven_done: bool = False
    unrealized_r: float = 0.0
    mfe_r: float = 0.0
    second_mfe_r: float = 0.0
    entry_risks: Dict[str, float] = field(default_factory=dict)
    entry_indices: Dict[str, int] = field(default_factory=dict)
    fill_versions: Dict[str, list] = field(default_factory=dict)
    mfe_since: float = 0.0
    exit_requested_reason: str = ""
    recovery_block_reason: str = ""
    restored: bool = False
    last_order_budget: float = 0.0
    last_order_risk: float = 0.0
    last_order_qty: int = 0
    last_order_stop: float = 0.0
    max_notional: float = 0.0
    last_signal_bar: float = 0.0
    last_signal_event_bar: float = 0.0
    done_reason: str = ""
    close_attempt_at: float = 0.0
    source: str = "auto"
    created_at: float = field(default_factory=time.time)
    started_at: float = 0.0
    startup_params: dict = field(default_factory=dict)
    last_input_at: float = 0.0
    last_decision_at: float = 0.0
    last_decision_reason: str = ""

    @property
    def key(self) -> TradingGroupKey:
        return TradingGroupKey(self.strategy_id, self.strategy_instance_id, self.symbol, self.group_id)

    def domain_state(self) -> ExecutionGroupState:
        return ExecutionGroupState(self.key, self.state, self.entries, self.total_qty,
                                   self.avg_cost, self.stop_px, self.enabled)

    def view(self) -> dict:
        state = self.domain_state()
        return {
            "inst_id": (
                f"pyramid-{self.symbol}"
                if self.strategy_id == "ema9_pyramid"
                and self.strategy_instance_id == "default"
                else f"pine-{self.strategy_id}-{self.strategy_instance_id}-{self.symbol}"
            ),
            "symbol": self.symbol,
            "strategy": self.strategy_id,
            "strategy_instance_id": self.strategy_instance_id,
            "state": self.state,
            "side": self.side,
            "start_direction": self.start_direction,
            "lot_id": self.lot_ids[0] if self.lot_ids else None,
            "lot_ids": list(self.lot_ids),
            "entry_px": round(self.avg_cost, 4),
            "avg_cost": round(self.avg_cost, 4),
            "qty": state.quantity,
            "stop_px": self.stop_px,
            "entries": self.entries,
            "initial_risk_usd": self.initial_risk_usd,
            "risk_budgets": [
                round(self.initial_risk_usd * ratio, 2) for ratio in self.risk_ratios
            ],
            "execution_profile": self.execution_profile,
            "initial_r": round(self.initial_r, 4),
            "breakeven_done": self.breakeven_done,
            "unlocked": self.unlocked,
            "unrealized_r": self.unrealized_r,
            "mfe_r": self.mfe_r,
            "second_mfe_r": self.second_mfe_r,
            "max_notional": round(self.max_notional, 2),
            "done_reason": self.done_reason,
            "source": self.source,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "startup_params": dict(self.startup_params),
            "last_input_at": self.last_input_at,
            "last_decision_at": self.last_decision_at,
            "last_decision_reason": self.last_decision_reason,
            "group_id": self.group_id,
            "enabled": self.enabled,
            "pending_lot_id": self.pending_lot_id,
            "exit_requested_reason": self.exit_requested_reason,
            "recovery_block_reason": self.recovery_block_reason,
            "restored": self.restored,
            "last_order_budget": self.last_order_budget,
            "last_order_risk": self.last_order_risk,
            "last_order_qty": self.last_order_qty,
            "last_order_stop": self.last_order_stop,
        }


class PineExecutionEngine:
    """一个标的一组仓位；实时触线建仓，独立 R 门槛解锁加仓与保本。"""

    def __init__(
        self,
        broker: TradeBroker,
        emit=None,
        max_instances: int = 5,
        breakeven_enabled: Optional[bool] = None,
        breakeven_r: float = DEFAULT_BREAKEVEN_R,
        add_unlock_r: float = DEFAULT_ADD_UNLOCK_R,
        *,
        model: Optional[PineModel] = None,
        instance_id: str = "default",
        execution_profile=None,
    ) -> None:
        if breakeven_r <= 0 or add_unlock_r <= 0:
            raise ValueError("保本和加仓解锁 R 门槛必须大于 0")
        self.model = model or PineModelRegistry.load().get("ema9_pyramid")
        self.profile = execution_profile or self.model.profile
        self.instance_id = instance_id
        self._broker = broker
        sink = emit or (lambda *_a, **_k: None)
        self._emit = lambda event, **data: sink(
            event,
            strategy_id=self.model.strategy_id,
            strategy_instance_id=self.instance_id,
            **data,
        )
        self._max_instances = max_instances
        self._instances: Dict[str, ExecutionInstance] = {}
        self._placements_inflight = set()
        self._manual_protection_inflight = set()
        self.breakeven_enabled = (
            self.profile.auto_breakeven
            if breakeven_enabled is None
            else bool(breakeven_enabled)
        )
        self.breakeven_r = float(breakeven_r)
        self.add_unlock_r = float(add_unlock_r)
        self.enabled = False

    def _persist(self, inst: ExecutionInstance) -> None:
        if hasattr(self._broker, "save_pyramid_state"):
            self._broker.save_pyramid_state(inst.group_id, asdict(inst))

    def _second_lot(self, inst: ExecutionInstance):
        lot_id = next((k for k in inst.lot_ids if inst.entry_indices.get(k) == 2), None)
        return self._broker.get(lot_id) if lot_id else None

    def _record_start(self, inst: ExecutionInstance) -> None:
        inst.started_at = time.time()
        inst.startup_params = {
            "initial_risk_usd": inst.initial_risk_usd,
            "start_direction": inst.start_direction,
            "execution_profile": self.profile.name,
            "risk_ratios": list(inst.risk_ratios),
            "auto_breakeven": self.breakeven_enabled,
            "breakeven_r": self.breakeven_r,
            "add_unlock_r": self.add_unlock_r,
            "direction_source": "startup_pine_snapshot",
        }

    def start(
        self,
        symbols: List[str],
        initial_risk_usd: float,
        directions: Optional[dict] = None,
    ) -> List[str]:
        if initial_risk_usd <= 0:
            raise ValueError("首仓风险预算必须大于 0")
        started = []
        for symbol in symbols:
            inst = self._instances.get(symbol)
            if inst is not None and inst.state != DONE:
                if inst.recovery_block_reason or inst.exit_requested_reason:
                    continue
                inst.enabled = True
                if not inst.entries:
                    inst.initial_risk_usd = float(initial_risk_usd)
                self._record_start(inst)
                self._persist(inst)
                started.append(symbol)
                continue
            if (
                len([i for i in self._instances.values() if i.state != DONE])
                >= self._max_instances
            ):
                break
            start_direction = int(
                (directions or {}).get(symbol, 0) if directions is not None else 0
            )
            self._instances[symbol] = ExecutionInstance(
                symbol=symbol,
                strategy_id=self.model.strategy_id,
                strategy_instance_id=self.instance_id,
                execution_profile=self.profile.name,
                risk_ratios=self.profile.risk_ratios,
                initial_risk_usd=float(initial_risk_usd),
                start_direction=start_direction,
            )
            started.append(symbol)
            self._record_start(self._instances[symbol])
            self._persist(self._instances[symbol])
            self._emit(
                "pyramid_started",
                symbol=symbol,
                initial_risk_usd=initial_risk_usd,
                start_direction=start_direction,
            )
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
        self._persist(inst)

    async def _stop_close(
        self, inst: ExecutionInstance, ref_px: float, reason: str
    ) -> None:
        now = time.time()
        if inst.state == "CLOSING" and now - inst.close_attempt_at < 5.0:
            return
        if ref_px <= 0:
            ref_px = inst.avg_cost
        inst.close_attempt_at = now
        inst.state = "CLOSING"
        inst.exit_requested_reason = reason
        inst.enabled = False
        inst.unlocked = False
        self._persist(inst)
        errors = []
        for lot_id in list(inst.lot_ids):
            lot = self._broker.get(lot_id)
            if lot is None:
                continue
            if lot.state == PENDING:
                ok = await self._broker.cancel(lot_id)
                # 撤单期间可能成交；cancel_lot 会将实际成交仓置 ACTIVE。
                lot = self._broker.get(lot_id)
                if lot is not None and lot.state == ACTIVE:
                    ok = await self._broker.close(lot_id, ref_px, reason)
            elif lot.state == ACTIVE:
                ok = await self._broker.close(lot_id, ref_px, reason)
            else:
                continue
            if not ok:
                errors.append(lot_id)
        self._persist(inst)
        self._emit(
            "pyramid_close_requested",
            symbol=inst.symbol,
            reason=reason,
            lot_ids=list(inst.lot_ids),
            qty=inst.total_qty,
            avg_cost=inst.avg_cost,
            ref_px=ref_px,
            ok=not errors,
            failed_lot_ids=errors,
        )

    def instances(self) -> List[dict]:
        rows = []
        for inst in self._instances.values():
            row = inst.view()
            row.update(
                {
                    "auto_breakeven": self.breakeven_enabled,
                    "breakeven_r": self.breakeven_r,
                    "add_unlock_r": self.add_unlock_r,
                }
            )
            rows.append(row)
        return rows

    def proposals(self) -> list:
        return []

    def get_instance(self, symbol: str):
        return self._instances.get(symbol)

    async def close_symbol(
        self, symbol: str, ref_px: float = 0, reason: str = "manual_stop"
    ) -> None:
        inst = self.get_instance(symbol)
        if inst is not None:
            await self._stop_close(inst, ref_px, reason)

    async def adopt_seed(
        self,
        symbol: str,
        lot: dict,
        initial_risk_usd: float,
        e9: dict,
        enabled: bool = True,
    ) -> ExecutionInstance:
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
        if not direction or self.model.decode(e9).direction != direction:
            raise ValueError("手动仓方向与当前 EMA9 策略方向不一致")
        if (
            existing
            and existing.start_direction
            and existing.start_direction != direction
        ):
            raise ValueError("手动仓方向与启动时锁定方向不一致")
        init_entry = float(lot.get("init_entry_px") or entry)
        init_stop = float(lot.get("init_stop_px") or stop)
        risk = abs(init_entry - init_stop)
        if risk <= 0:
            raise ValueError("手动仓缺少有效的首仓风险距离")
        if existing is None:
            if (
                len([i for i in self._instances.values() if i.state != DONE])
                >= self._max_instances
            ):
                raise ValueError("策略实例已达上限")
            existing = ExecutionInstance(
                symbol=symbol,
                strategy_id=self.model.strategy_id,
                strategy_instance_id=self.instance_id,
            )
            self._instances[symbol] = existing
        existing.enabled = enabled
        existing.execution_profile = self.profile.name
        existing.risk_ratios = self.profile.risk_ratios
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
        existing.breakeven_done = False
        existing.unrealized_r = 0.0
        existing.mfe_r = 0.0
        existing.second_mfe_r = 0.0
        existing.entry_risks = {lot["lot_id"]: risk}
        existing.entry_indices = {lot["lot_id"]: 1}
        existing.max_notional = qty * entry
        existing.source = "manual_seed"
        existing.done_reason = ""
        if hasattr(self._broker, "mark_seed"):
            await self._broker.mark_seed(lot["lot_id"], risk, initial_risk_usd)
        if hasattr(self._broker, "mark_pyramid_group"):
            await self._broker.mark_pyramid_group(
                lot["lot_id"],
                {
                    "group_id": existing.group_id,
                    "initial_r": risk,
                    "initial_risk_usd": initial_risk_usd,
                    "start_direction": direction,
                    "entry_index": 1,
                    "strategy_id": self.model.strategy_id,
                    "strategy_instance_id": self.instance_id,
                },
            )
        existing.fill_versions = {lot["lot_id"]: [qty, entry]}
        if enabled:
            self._record_start(existing)
            existing.startup_params["direction_source"] = "manual_seed_and_pine_snapshot"
        existing.mfe_since = time.time() if lot.get("entry_time") else 0.0
        self._persist(existing)
        self.enabled = self.enabled or enabled
        self._emit(
            "pyramid_manual_adopted",
            symbol=symbol,
            side=side,
            qty=qty,
            avg_cost=entry,
            initial_r=risk,
            lot_id=lot["lot_id"],
        )
        return existing

    def _can_resume(self, inst: ExecutionInstance, session_date) -> bool:
        return (
            session_date is not None
            and inst.enabled
            and inst.started_at > 0
            and datetime.fromtimestamp(inst.started_at, ZoneInfo("America/New_York")).date()
            == session_date
            and inst.execution_profile == self.profile.name
            and tuple(inst.risk_ratios) == self.profile.risk_ratios
            and not inst.recovery_block_reason
            and not inst.exit_requested_reason
            and not inst.done_reason
        )

    def restore_watch(self, snapshot: dict, session_date) -> None:
        allowed = {f.name for f in fields(ExecutionInstance)}
        inst = ExecutionInstance(**{k: v for k, v in snapshot.items() if k in allowed})
        if (
            inst.symbol not in self._instances
            and inst.state == WATCHING
            and not inst.lot_ids
            and not inst.pending_lot_id
            and not inst.entries
            and math.isfinite(inst.initial_risk_usd)
            and inst.initial_risk_usd > 0
            and self._can_resume(inst, session_date)
        ):
            inst.restored = True
            self._instances[inst.symbol] = inst
            self.enabled = True

    async def adopt_manual_lots(
        self, lots: List[dict], signals: Optional[dict] = None, *,
        session_date=None, resumable_groups=(),
    ) -> List[str]:
        saved = (
            self._broker.pyramid_states()
            if hasattr(self._broker, "pyramid_states")
            else {}
        )
        grouped = {}
        for lot in lots:
            meta = lot.get("signal_snapshot") or {}
            owner = meta.get("strategy_id") or "ema9_pyramid"
            owner_instance = meta.get("strategy_instance_id") or "default"
            if (
                owner == self.model.strategy_id
                and owner_instance == self.instance_id
                and (
                    lot.get("limit_type") == f"auto_{self.model.strategy_id}"
                    or meta.get("pyramid_seed")
                )
                and lot.get("state") in _OPEN
            ):
                grouped.setdefault(lot["symbol"], []).append(lot)
        adopted = []
        for symbol, rows in grouped.items():
            old = self._instances.get(symbol)
            if old is not None and (old.lot_ids or old.entries):
                continue
            rows.sort(key=lambda r: r.get("created_at", 0))
            first = rows[0]
            first_meta = first.get("signal_snapshot") or {}
            group_ids = {(r.get("signal_snapshot") or {}).get("group_id") for r in rows}
            group_id = first_meta.get("group_id")
            snapshot = saved.get(group_id) if len(group_ids) == 1 and group_id else None
            if (
                snapshot
                and snapshot.get("symbol") == symbol
                and snapshot.get("strategy_id", "ema9_pyramid")
                == self.model.strategy_id
                and snapshot.get("strategy_instance_id", "default") == self.instance_id
            ):
                allowed = {f.name for f in fields(ExecutionInstance)}
                inst = ExecutionInstance(
                    **{k: v for k, v in snapshot.items() if k in allowed}
                )
                if not inst.side:
                    inst.side = first["side"]
                if inst.initial_r <= 0:
                    inst.initial_r = float(first_meta.get("initial_r") or 0)
                if (
                    inst.initial_r <= 0
                    or inst.initial_risk_usd <= 0
                    or inst.side != first["side"]
                ):
                    inst.recovery_block_reason = (
                        "整组风险基准缺失或方向冲突，自动加仓已暂停"
                    )
            else:
                inst = ExecutionInstance(
                    symbol=symbol,
                    strategy_id=self.model.strategy_id,
                    strategy_instance_id=self.instance_id,
                )
                inst.group_id = group_id or inst.group_id
                inst.side = first["side"]
                inst.start_direction = int(
                    first_meta.get("start_direction")
                    or (1 if first.get("side") == "long" else -1)
                )
                inst.entries = max(
                    int((r.get("signal_snapshot") or {}).get("entry_index") or 1)
                    for r in rows
                )
                inst.initial_risk_usd = float(first_meta.get("initial_risk_usd") or 0)
                inst.initial_r = float(first_meta.get("initial_r") or 0)
                if not inst.initial_r and first_meta.get("entry_index") == 1:
                    inst.initial_r = float(first_meta.get("entry_risk_dist") or 0)
                inst.stop_px = rows[-1]["stop_px"]
                # 无整组快照无法证明累计次数/MFE，仍管理保护与退出，但禁止加仓。
                inst.recovery_block_reason = "缺少完整整组快照，自动加仓已暂停"
            if len(group_ids) != 1 or len({r["side"] for r in rows}) != 1:
                inst.recovery_block_reason = (
                    "同标的存在不同交易组或方向，自动加仓已暂停"
                )
            inst.enabled = (
                group_id in resumable_groups
                and self._can_resume(inst, session_date)
                and sum(row["state"] == PENDING for row in rows) <= 1
                and not any(
                    row.get("entry_submission_pending") or row["state"] == "CLOSING"
                    for row in rows
                )
            )
            inst.execution_profile = self.profile.name
            inst.risk_ratios = self.profile.risk_ratios
            inst.restored = True
            inst.lot_ids = [r["lot_id"] for r in rows]
            for row in rows:
                meta = row.get("signal_snapshot") or {}
                inst.entry_indices[row["lot_id"]] = int(meta.get("entry_index") or 1)
                inst.entries = max(inst.entries, inst.entry_indices[row["lot_id"]])
                inst.entry_risks[row["lot_id"]] = float(
                    meta.get("entry_risk_dist") or 0
                )
            inst.pending_lot_id = next(
                (r["lot_id"] for r in rows if r["state"] == PENDING), None
            )
            if (first.get("signal_snapshot") or {}).get("pyramid_seed"):
                inst.source = "manual_seed"
            self._instances[symbol] = inst
            await self._refresh(inst)
            self.enabled = self.enabled or (inst.enabled and inst.state != DONE)
            self._persist(inst)
            adopted.append(symbol)
        for lot in lots:
            if (
                self.model.strategy_id != "ema9_pyramid"
                or self.instance_id != "default"
                or lot.get("state") != ACTIVE
                or lot.get("limit_type") != "market"
            ):
                continue
            symbol = lot["symbol"]
            inst = self._instances.get(symbol)
            if inst is None or not inst.enabled or inst.lot_ids:
                continue
            e9 = ((signals or {}).get(symbol) or {}).get(self.model.realtime_key) or {}
            try:
                await self.adopt_seed(
                    symbol, lot, inst.initial_risk_usd, e9, enabled=True
                )
                adopted.append(symbol)
            except ValueError as e:
                logger.warning("手动市价首仓接管失败 %s: %s", symbol, e)
        return adopted

    async def _refresh(self, inst: ExecutionInstance) -> None:
        if inst.state == DONE and not inst.lot_ids:
            return
        previous_avg = inst.avg_cost
        had_fill = any(
            (
                self._broker.get(lot_id) is not None
                and (
                    getattr(self._broker.get(lot_id), "avg_fill", 0) > 0
                    or getattr(self._broker.get(lot_id), "filled_qty", 0) > 0
                )
            )
            for lot_id in inst.lot_ids
        )
        kept = []
        for lot_id in list(inst.lot_ids):
            lot = self._broker.get(lot_id)
            if lot is None:
                continue
            if lot.state in _TERMINAL:
                if (
                    not (getattr(lot, "filled_qty", 0) or getattr(lot, "avg_fill", 0))
                    and inst.entries > 0
                ):
                    inst.entries -= 1
                if inst.pending_lot_id == lot_id:
                    inst.pending_lot_id = None
                continue
            kept.append(lot_id)
            if lot.state == ACTIVE and inst.pending_lot_id == lot_id:
                inst.pending_lot_id = None
                self._emit(
                    "pyramid_entry_filled",
                    symbol=inst.symbol,
                    side=inst.side,
                    qty=int(getattr(lot, "filled_qty", 0) or getattr(lot, "qty", 0)),
                    entry_px=(
                        getattr(lot, "avg_fill", 0) or getattr(lot, "entry_px", 0)
                    ),
                    entries=inst.entries,
                    lot_id=lot_id,
                )
        inst.lot_ids = kept
        qty = 0
        cost = 0.0
        new_fill = False
        for lot_id in kept:
            lot = self._broker.get(lot_id)
            if lot.state != ACTIVE:
                continue
            lot_qty = int(getattr(lot, "filled_qty", 0) or getattr(lot, "qty", 0))
            avg = getattr(lot, "avg_fill", 0) or getattr(lot, "entry_px", 0)
            previous = inst.fill_versions.get(lot_id)
            if (
                previous is None
                or lot_qty > previous[0]
                or abs(avg - previous[1]) > 1e-6
            ):
                new_fill = True
                if inst.entry_indices.get(lot_id) == 2:
                    inst.second_mfe_r = 0.0
            inst.fill_versions[lot_id] = [lot_qty, avg]
            qty += lot_qty
            cost += avg * lot_qty
        inst.total_qty = qty
        if qty:
            inst.avg_cost = cost / qty
        if new_fill or (qty and abs(inst.avg_cost - previous_avg) > 1e-6):
            inst.unrealized_r = 0.0
            inst.mfe_r = 0.0
            inst.unlocked = False
            inst.breakeven_done = False
            # 实盘从发现新成交之后采样；回测使用模拟成交时钟。
            inst.mfe_since = max(
                (
                    time.time()
                    if getattr(self._broker.get(k), "entry_time", 0)
                    else getattr(self._broker.get(k), "fill_time", 0)
                    for k in kept
                    if self._broker.get(k).state == ACTIVE
                ),
                default=0,
            )
        active_stops = [
            self._broker.get(k).stop_px
            for k in kept
            if self._broker.get(k).state in (ACTIVE, PENDING)
        ]
        if active_stops:
            inst.stop_px = (
                min(active_stops) if inst.side == "long" else max(active_stops)
            )
            if qty:
                target = round(inst.avg_cost, 2)
                inst.breakeven_done = all(
                    p >= target if inst.side == "long" else p <= target
                    for p in active_stops
                )
        if not kept:
            side = inst.side
            if had_fill and (not inst.enabled or inst.start_direction):
                inst.state = DONE
                inst.done_reason = inst.done_reason or "round_finished"
                inst.total_qty = 0
                inst.pending_lot_id = None
                inst.unlocked = False
                inst.breakeven_done = False
                inst.unrealized_r = 0.0
                if side:
                    self._emit(
                        "pyramid_flat", symbol=inst.symbol, side=side, had_fill=had_fill
                    )
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
            inst.breakeven_done = False
            inst.unrealized_r = 0.0
            inst.mfe_r = 0.0
            inst.second_mfe_r = 0.0
            inst.entry_risks.clear()
            inst.entry_indices.clear()
            inst.fill_versions.clear()
            inst.pending_lot_id = None
            if side:
                self._emit(
                    "pyramid_flat", symbol=inst.symbol, side=side, had_fill=had_fill
                )
        elif inst.exit_requested_reason:
            inst.state = "CLOSING"
        elif inst.pending_lot_id:
            inst.state = PENDING
        elif inst.state != "CLOSING":
            inst.state = ACTIVE

    async def _move_all_stops(self, inst: ExecutionInstance, new_stop: float) -> bool:
        ok = True
        for lot_id in inst.lot_ids:
            lot = self._broker.get(lot_id)
            if lot is not None and lot.state == ACTIVE:
                # 已改成功的单不重复提交；支持某一笔失败后的幂等重试。
                if (inst.side == "long" and lot.stop_px >= round(new_stop, 2)) or (
                    inst.side == "short" and lot.stop_px <= round(new_stop, 2)
                ):
                    continue
                ok = (
                    bool(await self._broker.move_stop(lot_id, round(new_stop, 2)))
                    and ok
                )
        if ok:
            inst.stop_px = round(new_stop, 2)
        return ok

    async def manual_breakeven(self, symbol: str, group_id: str, expected_qty: int,
                               expected_avg_cost: float) -> dict:
        """Manually tighten the current filled group to its actual average cost.

        Block new entries while processing; exit/cancel still wins through the
        broker's existing per-order coordinator. Partial results are not rolled
        back (that could loosen protection).
        """
        inst = self._instances.get(symbol)
        if inst is None or inst.group_id != group_id:
            raise ValueError("交易组已变化，请刷新执行监控后再试")
        key = inst.key
        if key in self._placements_inflight or key in self._manual_protection_inflight:
            raise ValueError("该交易组正在下单或修改保护，请稍后重试")
        self._manual_protection_inflight.add(key)
        try:
            if any(self._broker.get(lot_id) is None for lot_id in inst.lot_ids):
                raise ValueError("关联订单缺失，请先核对执行账本与订单明细")
            await self._refresh(inst)
            if inst.state in (DONE, "CLOSING") or inst.exit_requested_reason:
                raise ValueError("该交易组已结束或正在退出，不可保本")
            if inst.recovery_block_reason:
                raise ValueError("交易组恢复状态待核验，请先核对订单明细")
            if inst.pending_lot_id:
                raise ValueError("存在待成交订单，请等待成交或撤单确认后再保本")
            lots = [self._broker.get(lot_id) for lot_id in inst.lot_ids]
            if not lots or any(lot is None or lot.state != ACTIVE for lot in lots):
                raise ValueError("关联订单尚未全部确认为持仓状态，请核对订单明细")
            if any(not getattr(lot, "avg_fill", 0) > 0
                   or not getattr(lot, "filled_qty", getattr(lot, "qty", 0)) > 0
                   or getattr(lot, "partial_exit", None)
                   or getattr(lot, "entry_submission_pending", False)
                   or getattr(lot, "symbol", symbol) != symbol
                   or getattr(lot, "side", inst.side) != inst.side
                   or (getattr(lot, "entry_trade", None) is not None and not lot.entry_trade.isDone())
                   for lot in lots):
                raise ValueError("存在未确认成交、未完成委托或减仓，请先核对订单明细")
            target = round(inst.avg_cost, 2)
            if inst.total_qty <= 0 or target <= 0 or not math.isfinite(target):
                raise ValueError("没有可保本的实际持仓或成交均价")
            if inst.total_qty != expected_qty or target != round(expected_avg_cost, 2):
                raise ValueError("持仓数量或成本已变化，请刷新后重新确认")
            result = {"ok": True, "group_id": group_id, "target_stop": target,
                      "qty": inst.total_qty, "updated": [], "unchanged": [], "failed": []}
            for lot in lots:
                if self._instances.get(symbol) is not inst or inst.exit_requested_reason or inst.state == "CLOSING":
                    result["failed"].append({"lot_id": lot.lot_id, "reason": "交易组已替换或退出请求优先"})
                    continue
                if lot.state != ACTIVE:
                    result["failed"].append({"lot_id": lot.lot_id, "reason": "订单状态已变化"})
                    continue
                await self._refresh(inst)
                if inst.total_qty != expected_qty or round(inst.avg_cost, 2) != target:
                    result["failed"].append({"lot_id": lot.lot_id, "reason": "持仓数量或成本已变化，停止后续改单"})
                    continue
                favorable = (inst.side == "long" and lot.stop_px >= target) or (
                    inst.side == "short" and 0 < lot.stop_px <= target)
                if favorable:
                    result["unchanged"].append(lot.lot_id)
                    continue
                try:
                    ok = await self._broker.move_stop(lot.lot_id, target)
                except Exception as error:
                    result["failed"].append({"lot_id": lot.lot_id, "reason": str(error) or "保护修改失败"})
                else:
                    if ok:
                        result["updated"].append(lot.lot_id)
                    else:
                        result["failed"].append({"lot_id": lot.lot_id, "reason": "保护修改未成功，请核对订单回报"})
            await self._refresh(inst)
            if (self._instances.get(symbol) is not inst or inst.exit_requested_reason
                    or inst.state == "CLOSING" or inst.total_qty != expected_qty
                    or round(inst.avg_cost, 2) != target):
                result["failed"].append({"lot_id": "交易组", "reason": "处理期间持仓、成本或退出状态发生变化，请重新核对"})
            result["ok"] = not result["failed"]
            self._persist(inst)
            self._emit("manual_group_breakeven", symbol=symbol, **result)
            return result
        finally:
            self._manual_protection_inflight.discard(key)

    async def _place(self, inst: ExecutionInstance, e9: dict, now_hm: int, price: float, observed_at=None) -> None:
        key = inst.key
        if key in self._placements_inflight or key in self._manual_protection_inflight:
            return
        self._placements_inflight.add(key)
        try:
            await self._place_once(inst, e9, now_hm, price, observed_at)
        finally:
            self._placements_inflight.discard(key)

    async def _place_once(self, inst: ExecutionInstance, e9: dict, now_hm: int, price: float, observed_at=None) -> None:
        if (
            not self.enabled
            or not inst.enabled
            or inst.pending_lot_id
            or inst.recovery_block_reason
            or inst.exit_requested_reason
            or inst.state in ("CLOSING", DONE)
        ):
            return
        if e9.get("entry_signal") != 1:
            return
        bar_time = e9.get("time") or 0
        if bar_time and inst.last_signal_event_bar == bar_time:
            return
        direction = e9.get("direction")
        side = "long" if direction == 1 else "short" if direction == -1 else ""
        ema = e9.get("entry_price")
        before_qty, before_avg = inst.total_qty, inst.avg_cost
        mfe = inst.mfe_r

        def signal(action: str, reason: str, **details) -> None:
            inst.last_decision_at = time.time()
            inst.last_decision_reason = reason or action
            self._emit(
                "pyramid_signal",
                symbol=inst.symbol,
                side=side,
                entry_px=ema,
                action=action,
                reason=reason,
                qty_before=before_qty,
                avg_before=before_avg,
                mfe_r=mfe,
                second_mfe_r=inst.second_mfe_r,
                **details,
            )

        if block := e9.get("entry_block_reason"):
            if inst.last_decision_reason != block:
                signal("ignored", block)
            return
        inst.last_signal_event_bar = bar_time
        if not self.profile.entry_start_hm <= now_hm < self.profile.entry_end_hm:
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
            signal("ignored", f"平均成本未达到{self.add_unlock_r:g}R")
            return
        second = self._second_lot(inst)
        second_active = second is not None and second.state == ACTIVE
        if (
            before_qty
            and inst.entries == 2
            and (not second_active or inst.second_mfe_r + 1e-9 < 1.0)
        ):
            signal("ignored", "第二次仓位未达到1R")
            return
        if before_qty and inst.entries >= len(self.profile.risk_ratios):
            signal("ignored", "已达到三次建仓上限")
            return
        budget = inst.initial_risk_usd * self.profile.risk_ratios[inst.entries]
        try:
            plan = OrderPlan.sized(inst.symbol, side, ema,
                                   signal_stop if not before_qty else inst.stop_px,
                                   budget, group=inst.key, signal_bar_time=bar_time,
                                   created_at=time.time(), risk_reference_price=price, entry_order_type="MKT")
        except ValueError as error:
            signal("ignored", str(error))
            return
        entry_px, order_stop, qty = plan.entry_price, plan.stop_price, plan.qty
        risk_dist = plan.risk_distance
        inst.last_signal_bar = bar_time
        self._emit(
            "pyramid_entry_decision",
            symbol=inst.symbol, side=side, group_id=inst.group_id,
            signal_bar_time=bar_time, entry_index=inst.entries + 1,
            decision_at=plan.created_at, price_observed_at=observed_at,
            signal_reference_price=ema, observed_price=price,
            qty=qty, stop_px=order_stop, entry_order_type="MKT",
        )
        try:
            lot = await self._broker.place_limit_stop(
                symbol=inst.symbol,
                side=side,
                qty=qty,
                limit_px=entry_px,
                stop_px=order_stop,
                limit_type=f"auto_{self.model.strategy_id}",
                reason=f"{self.model.name} 第{inst.entries + 1}次 预算${budget:.2f}",
                meta={
                    "entry_order_type": "MKT",
                    "risk_reference_price": plan.reference_price,
                    "execution_observed_at": observed_at,
                    "order_plan": plan.values(),
                    "entry_risk_dist": risk_dist,
                    "entry_index": inst.entries + 1,
                    "start_direction": inst.start_direction,
                    "strategy_id": self.model.strategy_id,
                    "strategy_instance_id": self.instance_id,
                    "execution_profile": self.profile.name,
                    "group_id": inst.group_id,
                    "initial_risk_usd": inst.initial_risk_usd,
                    "initial_r": inst.initial_r or risk_dist,
                    "risk_budget": budget,
                    "planned_risk_usd": plan.planned_risk,
                },
            )
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
        inst.entries += 1
        inst.entry_risks[lot.lot_id] = risk_dist
        inst.entry_indices[lot.lot_id] = inst.entries
        inst.pending_lot_id = lot.lot_id
        inst.lot_ids.append(lot.lot_id)
        inst.state = PENDING
        inst.last_order_budget = budget
        inst.last_order_risk = round(qty * risk_dist, 2)
        inst.last_order_qty = qty
        inst.last_order_stop = order_stop
        self._persist(inst)
        after_qty = before_qty + qty
        after_avg = (
            (before_avg * before_qty + plan.reference_price * qty) / after_qty if after_qty else plan.reference_price
        )
        signal(
            "add" if before_qty else "initial",
            f"{self.add_unlock_r:g}R已解锁" if before_qty else "首次有效触线",
            entries=inst.entries,
            qty=qty,
            qty_after=after_qty,
            avg_after=after_avg,
            risk_budget=budget,
            stop_px=order_stop,
            planned_risk_usd=inst.last_order_risk,
            lot_id=lot.lot_id,
        )
        self._emit(
            "pyramid_order_placed",
            symbol=inst.symbol,
            side=side,
            qty=qty,
            entry_px=ema,
            entry_order_type="MKT",
            risk_reference_price=plan.reference_price,
            planned_risk_usd=plan.planned_risk,
            stop_px=order_stop,
            entries=inst.entries,
            risk_budget=budget,
            lot_id=lot.lot_id,
        )
        # A terminal intent may arrive while the broker submission is awaiting.
        # Attach the accepted order first, then finish the original exit intent.
        # Plain pause deliberately keeps an already-submitted order unchanged.
        superseded = self._instances.get(inst.symbol) is not inst
        if superseded or inst.exit_requested_reason or inst.done_reason == "manual_stop_close":
            inst.close_attempt_at = 0
            reason = inst.exit_requested_reason or ("superseded_group" if superseded else "manual_stop")
            await self._stop_close(inst, entry_px, reason)

    async def on_observation(
        self, observation: PriceObservation, output, now_hm: int, *, entry_block_reason=""
    ) -> None:
        output = output.for_bar(observation.bar_open_time, received_at=observation.received_at)
        await self.on_output(observation.symbol, observation.price, output, now_hm,
                             observed_at=observation.observed_at, entry_block_reason=entry_block_reason)

    async def on_confirmed_bar(self, bar: ClosedBar, output) -> None:
        output = output.for_bar(bar.open_time, confirmed=True, received_at=output.received_at)
        if output.alignment == "mismatch":
            raise ValueError("确认指标与收盘 K 线时间不一致")
        if output.strategy_id != self.model.strategy_id:
            raise ValueError("确认指标与执行实例归属不一致")
        await self.on_bar(bar.symbol, bar.values(), output)

    async def on_price(
        self,
        symbol: str,
        price: float,
        e9: dict,
        now_hm: int,
        *,
        observed_at: Optional[float] = None,
    ) -> None:
        try:
            await self.on_output(
                symbol, price, self.model.decode(e9), now_hm, observed_at=observed_at
            )
        finally:
            inst = self._instances.get(symbol)
            if inst is not None:
                self._persist(inst)

    async def on_output(
        self,
        symbol: str,
        price: float,
        output,
        now_hm: int,
        *,
        observed_at: Optional[float] = None,
        entry_block_reason: str = "",
    ) -> None:
        if output.strategy_id != self.model.strategy_id:
            raise ValueError("Pine 输出与执行实例的策略归属不一致")
        values = output.execution_values()
        if entry_block_reason:
            values["entry_block_reason"] = entry_block_reason
        instance = self._instances.get(symbol)
        if instance is not None:
            instance.last_input_at = observed_at or time.time()
        try:
            await self._on_price(
                symbol,
                price,
                values,
                now_hm,
                observed_at=observed_at,
            )
        finally:
            inst = self._instances.get(symbol)
            if inst is not None:
                self._persist(inst)

    async def _on_price(
        self,
        symbol: str,
        price: float,
        e9: dict,
        now_hm: int,
        *,
        observed_at: Optional[float] = None,
    ) -> None:
        inst = self._instances.get(symbol)
        if inst is None or not math.isfinite(price) or price <= 0:
            return
        await self._refresh(inst)
        if inst.exit_requested_reason and inst.lot_ids:
            await self._stop_close(inst, price, inst.exit_requested_reason)
            return
        if observed_at is not None and observed_at < inst.mfe_since:
            return
        if inst.total_qty and inst.initial_r > 0:
            sign = 1 if inst.side == "long" else -1
            inst.unrealized_r = round(
                (price - inst.avg_cost) * sign / inst.initial_r, 3
            )
            inst.mfe_r = max(inst.mfe_r, inst.unrealized_r)
            inst.max_notional = max(inst.max_notional, inst.total_qty * price)
            if inst.entries >= 2:
                second = self._second_lot(inst)
                second_risk = inst.entry_risks.get(getattr(second, "lot_id", ""), 0.0)
                if second is not None and second.state == ACTIVE and second_risk > 0:
                    second_entry = getattr(second, "avg_fill", 0) or second.entry_px
                    second_r = (price - second_entry) * sign / second_risk
                    inst.second_mfe_r = max(inst.second_mfe_r, round(second_r, 3))
            if not inst.unlocked and inst.mfe_r >= self.add_unlock_r:
                inst.unlocked = True
                self._emit(
                    "pyramid_unlocked",
                    symbol=inst.symbol,
                    avg_cost=inst.avg_cost,
                    mfe_r=inst.mfe_r,
                    qty=inst.total_qty,
                    stop_px=inst.stop_px,
                    unlock_r=self.add_unlock_r,
                )
            if (
                self.breakeven_enabled
                and inst.key not in self._manual_protection_inflight
                and not inst.breakeven_done
                and inst.mfe_r >= self.breakeven_r
            ):
                if await self._move_all_stops(inst, inst.avg_cost):
                    inst.breakeven_done = True
                    self._emit(
                        "pyramid_breakeven",
                        symbol=inst.symbol,
                        avg_cost=inst.avg_cost,
                        mfe_r=inst.mfe_r,
                        qty=inst.total_qty,
                        stop_px=inst.stop_px,
                        breakeven_r=self.breakeven_r,
                    )
        await self._place(inst, e9, now_hm, price, observed_at)

    async def on_price_extremes(
        self, symbol: str, low: float, high: float, e9: dict, now_hm: int
    ) -> None:
        """仅回测使用 OHLC 极值路径；实盘必须调用 on_price。"""
        if not isinstance(self._broker, SimulatedPriceFeed):
            raise ValueError("实盘持仓管理必须使用最新价，不能重放 K 线历史极值")
        inst = self._instances.get(symbol)
        side = (
            inst.side
            if inst and inst.side
            else ("long" if self.model.decode(e9).direction == 1 else "short")
        )
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
        e9_c = (e9_c if isinstance(e9_c, StrategyOutput) else self.model.decode(e9_c)).execution_values()
        inst = self._instances.get(symbol)
        if inst is None:
            return
        await self._refresh(inst)
        if not inst.enabled and inst.done_reason == "manual_stop_close":
            await self._stop_close(inst, bar.get("close") or 0, "manual_stop")
            return
        if inst.exit_requested_reason and inst.lot_ids:
            await self._stop_close(
                inst, bar.get("close") or 0, inst.exit_requested_reason
            )
        elif inst.lot_ids:
            direction = e9_c.get("direction")
            flipped = (inst.side == "long" and direction == -1) or (
                inst.side == "short" and direction == 1
            )
            if flipped:
                await self._stop_close(inst, bar.get("close") or 0, "trend_reverse")
            elif self.profile.follow_pine_stop and inst.total_qty:
                stop = e9_c.get("long_stop" if inst.side == "long" else "short_stop")
                close = bar.get("close") or 0
                if (
                    stop
                    and stop > 0
                    and close
                    and (
                        (inst.side == "long" and stop < close)
                        or (inst.side == "short" and stop > close)
                    )
                ):
                    await self._move_all_stops(inst, stop)
        self._persist(inst)
