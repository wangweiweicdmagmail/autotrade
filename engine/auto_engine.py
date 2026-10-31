# -*- coding: utf-8 -*-
"""
策略引擎 (auto_engine.py)
==========================

**一个引擎 → 多个策略实例**。每个实例 = 一个 symbol 的一次完整交易
生命周期（WATCHING → AWAITING_CONFIRM → PENDING → ACTIVE → DONE；
非确认模式跳过 AWAITING_CONFIRM）。理论并行无上限，实际上限
AUTO_MAX_INSTANCES（5）。

运行模型（用户定稿）：
- **进场 tick 触发**：WATCHING 实例每次价格更新时检查——实时成交价触及
  线值（指标永远用上一根已收盘 5M 的确认值 ts_c）→ 生成 EntryPlan；
- **实时开仓人工确认**：确认模式只产生候选并提醒，用户批准后才挂 LMT + STP；
  候选仅在产生它的 5M bar 内有效，拒绝/过期不会下单；
- **ACTIVE tick 保本**：最新价/极值达到 breakeven_r（2R）时立即将止损
  移到实际入场成本（券商确认后才标记，1 次，只紧不松）；
- **其余持仓管理在 bar 收盘执行**：
  1. 保本后按 supertrend 线值棘轮（单向，1 美分防抖）；
  2. 趋势反向（ts_c.direction 与持仓相反）→ 平仓；
  3. 手动取消/平仓（含 TWS 操作，对账发现）→ 实例 DONE；
- **本地持仓恢复管理**：引擎收养 OrderManager 中未被实例管理的 ACTIVE lot
  （含重启后的 auto_ lot），同一套 2R 保本 + ST 跟踪 + 反向平仓逻辑
  （跳过 WATCHING/PENDING；不扫描外部 TWS 持仓）。

确定性（策略与管理回测口径）：结果 = 数据 × 策略代码 × 参数。
- 决策只读「已收盘确认值」+ 触发价，无时钟/随机/外部状态；
- 回测与实盘共用同一 StrategyEngine；数据源（LiveFeed/BacktestFeed）与
  执行网关（LiveBroker/SimBroker）可插拔；
- 回测关闭人工确认以验证原始信号和自动管理；实盘的人工选股是其上游过滤；
- tick 进场的回测口径：bar 极值建模（多: low≤线 / 空: high≥线 = 曾触及），
  bar 内时序不可知，统一此口径（见 on_bar_extremes）。
"""

import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

# 实例状态
WATCHING = "WATCHING"   # 监控进场（tick 触发）
AWAITING_CONFIRM = "AWAITING_CONFIRM"  # 候选已产生，等待人工确认（尚未下单）
PENDING = "PENDING"     # 限价单已挂，等成交
ACTIVE = "ACTIVE"       # 持仓中（bar 收盘管理）
DONE = "DONE"           # 实例结束

BAR_SECONDS = 300


@dataclass
class EntryPlan:
    """策略 entry_trigger 的返回：完整入场计划（价格由策略算好）。"""
    symbol: str
    side: str             # long / short
    limit_px: float       # 限价（线值）
    stop_px: float        # 止损（限价 ∓ 1×ATR）
    line: str             # 踩的线：ema / channel / fvg
    reason: str = ""


@dataclass
class StrategyInstance:
    """一个 symbol 的一次交易生命周期。"""
    inst_id: str
    symbol: str
    strategy: str
    state: str = WATCHING
    side: str = ""
    lot_id: Optional[str] = None
    entry_px: float = 0.0
    stop_px: float = 0.0
    init_entry_px: float = 0.0      # 下单时计划限价（R 分母基准）
    init_stop_px: float = 0.0       # 下单时计划止损（R 分母基准）
    r_dist: float = 0.0
    qty: int = 0
    line: str = ""
    adopted: bool = False           # 收养的 OrderManager 本地 lot
    activated: bool = False         # 本轮是否真正成交并进入 ACTIVE（未成交撤单不触发 2R 重入锁）
    breakeven_done: bool = False
    breakeven_attempt_at: float = 0.0
    created_bar_time: float = 0.0   # PENDING 挂单时的 bar 时间
    entries_today: int = 0
    unrealized_r: float = 0.0
    mfe_r: float = 0.0
    done_reason: str = ""
    entry_plan: Optional[EntryPlan] = None
    proposal: Optional[dict] = None
    proposal_bar_c: dict = field(default_factory=dict)
    proposal_processing: bool = False
    last_proposal_bar_time: float = 0.0
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def view(self) -> dict:
        row = {k: getattr(self, k) for k in (
            "inst_id", "symbol", "strategy", "state", "side", "lot_id",
            "entry_px", "stop_px", "r_dist", "qty", "line", "adopted",
            "breakeven_done", "unrealized_r", "mfe_r", "done_reason",
            "entries_today", "created_at")}
        row["proposal_id"] = (self.proposal or {}).get("proposal_id")
        return row


# ---------------------------------------------------------------------------
# 策略基类
# ---------------------------------------------------------------------------

class AutoStrategy(ABC):
    """策略即插件：只实现进场信号；五件套管理由引擎统一执行。"""

    name = "base"
    params = {
        "expire_bars": 3,            # 埋单 N 根 bar 未成交撤单回 WATCHING
        "breakeven_r": 2.0,          # 浮盈 ≥ 2R（1:2 盈亏比）移保本
        "entry_start_hm": 9 * 60 + 45,   # 9:45 ET 前不开新仓（开盘前 15 分钟）
        "entry_end_hm": 15 * 60,     # 15:00 ET 后不开新仓（最后一小时）
        "max_entries_per_day": 2,    # 每标的每日进场上限
        "use_fvg_line": True,        # FVG 区沿作为入场线（需求上沿 / 供应下沿）
        "require_same_dir_fvg": False,  # 要求最近 FVG 方向与 ST 方向一致
        "min_fvg_cum": 0,            # 同向 FVG 累积计数下限（0=不限制）
        "stop_atr_mult": 1.0,        # 进场止损 ATR 倍数（控制台启动时注入全局值，--param 可覆盖）
        "ema9_auto_entry": True,     # 当前 5M 实时触及 EMA9 时直接挂可成交限价单，不等人工确认
        "reentry_min_mfe_r": 2.0,    # EMA9 仓位达到过该 MFE 后，完结时才允许下一次触碰进场
    }

    @abstractmethod
    def entry_trigger(self, ctx: dict, symbol: str,
                      price: float) -> Optional[EntryPlan]:
        """tick 级进场判断。ctx = {"ts_c":..., "bar_c":..., "fvg_c":...}（确认值）。
        price = 最新成交价。返回 EntryPlan 或 None。"""

    def on_trade_closed(self, inst: StrategyInstance, result: dict) -> None:
        """可选统计钩子：实例 DONE（平仓/止损/撤销）时调用。"""


# ---------------------------------------------------------------------------
# 执行网关：SimBroker（回测，touch 成交模型）
# ---------------------------------------------------------------------------

@dataclass
class SimLot:
    lot_id: str
    symbol: str
    side: str
    qty: int
    entry_px: float
    stop_px: float
    init_stop_px: float = 0.0   # 初始止损（跟踪移动后不变，RR/MFE_R 的风险基准）
    state: str = PENDING
    avg_fill: float = 0.0
    exit_px: float = 0.0
    exit_reason: str = ""
    realized_pnl: float = 0.0
    mfe: float = 0.0                # 最大浮盈（美元）
    fill_time: float = 0.0          # 成交 bar 时间
    exit_time: float = 0.0          # 了结 bar 时间
    limit_type: str = ""
    reason: str = ""


class SimBroker:
    """回测网关：LMT 触价成交、STP 触价成交，与 LiveBroker 同接口。"""

    def __init__(self) -> None:
        self._lots: Dict[str, SimLot] = {}
        self._seq = 0

    def get(self, lot_id: str) -> Optional[SimLot]:
        return self._lots.get(lot_id)

    async def place_limit_stop(self, symbol: str, side: str, qty: int,
                               limit_px: float, stop_px: float,
                               limit_type: str = "", reason: str = "",
                               meta: Optional[dict] = None) -> SimLot:
        self._seq += 1
        lot = SimLot(lot_id=f"sim-{self._seq:04d}", symbol=symbol, side=side,
                     qty=qty, entry_px=limit_px, stop_px=stop_px,
                     init_stop_px=stop_px,
                     limit_type=limit_type, reason=reason)
        self._lots[lot.lot_id] = lot
        return lot

    async def cancel(self, lot_id: str) -> bool:
        lot = self._lots.get(lot_id)
        if lot and lot.state == PENDING:
            lot.state = "CANCELLED"
            return True
        return False

    async def close(self, lot_id: str, ref_px: float, reason: str,
                    ts: float = 0.0) -> bool:
        lot = self._lots.get(lot_id)
        if lot is None or lot.state != ACTIVE:
            return False
        self._settle(lot, ref_px, reason)
        lot.exit_time = ts
        return True

    async def move_stop(self, lot_id: str, new_stop: float) -> bool:
        lot = self._lots.get(lot_id)
        if lot is None or lot.state not in (PENDING, ACTIVE):
            return False
        lot.stop_px = new_stop
        return True

    def _settle(self, lot: SimLot, px: float, reason: str) -> None:
        lot.state = DONE
        lot.exit_px = round(px, 4)
        lot.exit_reason = reason
        sign = 1 if lot.side == "long" else -1
        lot.realized_pnl = round((lot.exit_px - lot.avg_fill) * sign * lot.qty, 2)

    # ---- 价格驱动（回测 feed 调用）----

    def on_price(self, symbol: str, price: float, ts: float = 0.0) -> None:
        """tick 驱动：PENDING 触价成交；ACTIVE 触及止损价成交。"""
        for lot in self._lots.values():
            if lot.symbol != symbol:
                continue
            if lot.state == PENDING:
                if (lot.side == "long" and price <= lot.entry_px) or \
                   (lot.side == "short" and price >= lot.entry_px):
                    lot.state = ACTIVE
                    lot.avg_fill = lot.entry_px
                    lot.fill_time = ts
            elif lot.state == ACTIVE:
                self._track_mfe(lot, price)
                if (lot.side == "long" and price <= lot.stop_px) or \
                   (lot.side == "short" and price >= lot.stop_px):
                    self._settle(lot, lot.stop_px, "stop")
                    lot.exit_time = ts

    def _track_mfe(self, lot: SimLot, price: float) -> None:
        if lot.avg_fill <= 0:
            return
        sign = 1 if lot.side == "long" else -1
        lot.mfe = max(lot.mfe, round((price - lot.avg_fill) * sign * lot.qty, 2))

    def closed_results(self) -> List[SimLot]:
        return [l for l in self._lots.values() if l.state == DONE]


# ---------------------------------------------------------------------------
# 执行网关：LiveBroker（OrderManager → IBKR / dry-run）
# ---------------------------------------------------------------------------

class LiveBroker:
    """实盘网关：包装 OrderManager（LMT 父 + STP 子，dry-run 虚拟成交）。"""

    def __init__(self, order_mgr) -> None:
        self._om = order_mgr

    def get(self, lot_id: str):
        return self._om.get(lot_id)

    async def place_limit_stop(self, symbol: str, side: str, qty: int,
                               limit_px: float, stop_px: float,
                               limit_type: str = "", reason: str = "",
                               meta: Optional[dict] = None):
        lot, err = await self._om.place_limit_stop_lot(
            symbol=symbol, side=side, qty=qty,
            limit_px=limit_px, stop_px=stop_px,
            limit_type=limit_type, reason=reason,
            signal_snapshot={**(meta or {}), "auto_strategy": True},
            trader="auto")   # 策略引擎下单归属标记（与人工单区分）
        if lot is None:
            raise RuntimeError(err)
        return lot

    async def mark_seed(self, lot_id: str, risk_dist: float,
                        initial_risk_usd: float) -> None:
        self._om.mark_pyramid_seed(lot_id, risk_dist, initial_risk_usd)

    async def cancel(self, lot_id: str) -> bool:
        ok, _ = await self._om.cancel_lot(lot_id)
        return ok

    async def close(self, lot_id: str, ref_px: float, reason: str,
                    ts: float = 0.0) -> bool:
        ok, _ = await self._om.close_lot(lot_id, ref_px=ref_px,
                                         exit_reason=reason)
        return ok

    async def move_stop(self, lot_id: str, new_stop: float) -> bool:
        ok, _ = await self._om.update_trail(lot_id, new_stop)
        return ok


# ---------------------------------------------------------------------------
# 策略引擎
# ---------------------------------------------------------------------------

class StrategyEngine:
    """常驻引擎：管理多个策略实例的全生命周期（五件套）。"""

    def __init__(self, broker, risk_gate=None, gateway=None,
                 emit: Optional[Callable] = None,
                 max_instances: int = 5,
                 require_entry_confirmation: bool = False) -> None:
        self._broker = broker
        self._risk = risk_gate
        self._gw = gateway
        self._emit = emit or (lambda *a, **k: None)
        self._max_instances = max_instances
        self._require_entry_confirmation = require_entry_confirmation
        self._instances: Dict[str, StrategyInstance] = {}   # symbol -> 实例
        self._strategies: Dict[str, AutoStrategy] = {}
        self._done: List[StrategyInstance] = []
        self._seq = 0
        self._proposal_seq = 0

    # ---- 实例管理 ----

    def start(self, strategy: AutoStrategy, symbols: List[str]) -> List[str]:
        """为每个 symbol 建 WATCHING 实例（受实例上限约束）。返回已建 symbol。"""
        started = []
        for sym in symbols:
            inst = self._instances.get(sym)
            if inst is not None and inst.state != DONE:
                continue
            if self._active_count() >= self._max_instances:
                logger.warning("实例上限 %d，%s 未启动", self._max_instances, sym)
                break
            self._seq += 1
            inst = StrategyInstance(
                inst_id=f"ai-{self._seq:04d}", symbol=sym,
                strategy=strategy.name)
            self._instances[sym] = inst
            self._strategies[sym] = strategy
            started.append(sym)
            self._emit("auto_started", symbol=sym, strategy=strategy.name,
                       inst_id=inst.inst_id)
            logger.info("策略实例启动 %s (%s)", sym, strategy.name)
        return started

    def stop_symbol(self, symbol: str, close_position: bool = False) -> None:
        inst = self._instances.get(symbol)
        if inst is None or inst.state == DONE:
            return
        # 停止新开仓；存量按 close_position 决定
        if inst.state in (WATCHING, AWAITING_CONFIRM) \
                or (inst.state == PENDING and not close_position):
            self._clear_proposal(inst)
            self._finish(inst, "manual_stop")
        elif close_position:
            inst.state = DONE
            inst.done_reason = "manual_stop_close"
            # 平仓由调用方（app/auto_runner）执行 order_mgr.close_lot
        self._emit("auto_stopped", symbol=symbol,
                   reason=inst.done_reason or "manual_stop")

    def _active_count(self) -> int:
        return sum(1 for i in self._instances.values() if i.state != DONE)

    def instances(self) -> List[dict]:
        rows = [i.view() for i in self._instances.values()]
        rows.sort(key=lambda x: (x["state"] == DONE, x["created_at"]))
        return rows

    def proposals(self) -> List[dict]:
        """当前待人工确认的开仓候选，按趋势发散度辅助排序。"""
        rows = []
        for inst in self._instances.values():
            if inst.state != AWAITING_CONFIRM or not inst.proposal:
                continue
            row = dict(inst.proposal)
            row["processing"] = inst.proposal_processing
            rows.append(row)
        rows.sort(key=lambda x: (-(x.get("divergence") or 0), x["created_at"]))
        return rows

    # ---- 收养本地 lot ----

    async def adopt_manual_lots(self, lots: List[dict]) -> List[str]:
        """收养 OrderManager 本地且未被管理的 ACTIVE lot（含重启后的 auto_ lot）。"""
        adopted = []
        managed_lots = {i.lot_id for i in self._instances.values()
                        if i.state != DONE and i.lot_id}
        for d in lots:
            if d["state"] != ACTIVE or d["lot_id"] in managed_lots:
                continue
            sym = d["symbol"]
            old = self._instances.get(sym)
            replace_watching = (old is not None and old.state == WATCHING
                                and not old.lot_id)
            if old is not None and old.state != DONE and not replace_watching:
                logger.warning("无法收养 %s lot %s：现有实例 %s 状态 %s（lot %s），"
                               "单 symbol 暂不能安全接管",
                               sym, d["lot_id"], old.inst_id, old.state, old.lot_id)
                continue
            managed_count = sum(1 for i in self._instances.values()
                                if i.state not in (WATCHING, DONE))
            if managed_count >= self._max_instances:
                logger.warning("非 WATCHING 实例已达上限 %d，%s lot %s 暂不收养",
                               self._max_instances, sym, d["lot_id"])
                continue
            entry = d.get("avg_fill") or d["entry_px"]
            stop = d["stop_px"]
            init_entry = d.get("init_entry_px") or d["entry_px"]
            init_stop = d.get("init_stop_px") or stop
            r_dist = abs(init_entry - init_stop)
            if r_dist <= 0:
                logger.warning("无法收养 %s lot %s：成交价与止损相同，缺少 R 风险基准",
                               sym, d["lot_id"])
                continue
            self._seq += 1
            side = d["side"]
            # 止损已在成本有利侧 = 已保过本
            be_done = (stop >= entry) if side == "long" else (stop <= entry)
            inst = StrategyInstance(
                inst_id=f"ai-{self._seq:04d}", symbol=sym,
                strategy="adopted", state=ACTIVE, side=side,
                lot_id=d["lot_id"], entry_px=entry, stop_px=stop,
                init_entry_px=init_entry, init_stop_px=init_stop,
                r_dist=r_dist, qty=d.get("filled_qty") or d["qty"],
                adopted=True, breakeven_done=be_done)
            self._instances[sym] = inst
            managed_lots.add(inst.lot_id)
            adopted.append(sym)
            self._emit("auto_adopted", symbol=sym, lot_id=d["lot_id"],
                       side=side, entry=entry, stop=stop)
            logger.info("收养本地 lot %s %s x%s @%.2f 损 %.2f",
                        sym, side, inst.qty, entry, stop)
        return adopted

    # ---- tick 入口（进场触发；WATCHING 实例专用）----

    async def on_price(self, symbol: str, price: float, ts_c: dict,
                       bar_c: dict, now_hm: int,
                       fvg_c: Optional[dict] = None) -> None:
        inst = self._instances.get(symbol)
        if inst is not None and inst.state == DONE:
            self._try_rearm(symbol, inst)
        if inst is not None and inst.state == ACTIVE and price > 0:
            await self._on_active_price(inst, price)
            return
        if inst is None or inst.state != WATCHING or price <= 0:
            return
        bt = bar_c.get("time") or 0
        if self._require_entry_confirmation and bt \
                and inst.last_proposal_bar_time == bt:
            return
        strategy = self._strategies.get(symbol)
        if strategy is None:
            return
        if now_hm < strategy.params.get("entry_start_hm", 0) \
                or now_hm >= strategy.params["entry_end_hm"]:
            return   # 时段闸：开盘前 15 分钟 / 最后一小时不开新仓
        ctx = {"ts_c": ts_c, "bar_c": bar_c, "inst": inst,
               "fvg_c": fvg_c or {}}
        plan = strategy.entry_trigger(ctx, symbol, price)
        if plan is None:
            return
        ema9_auto = (plan.line == "ema9"
                     and strategy.params.get("ema9_auto_entry", True))
        if self._require_entry_confirmation and not ema9_auto:
            await self._propose_entry(inst, plan, ts_c, bar_c, fvg_c=fvg_c)
        else:
            await self._open(inst, strategy, plan, bar_c)

    async def on_price_extremes(self, symbol: str, low: float, high: float,
                                ts_c: dict, bar_c: dict, now_hm: int,
                                fvg_c: Optional[dict] = None) -> None:
        """实时极值输入：按当前方向先喂有利极值，再喂不利极值。"""
        inst = self._instances.get(symbol)
        side = inst.side if inst and inst.side else (
            "long" if ts_c.get("direction") == 1 else "short")
        first, second = (high, low) if side == "long" else (low, high)
        for price in (first, second):
            if price and price > 0:
                await self.on_price(symbol, price, ts_c, bar_c, now_hm,
                                    fvg_c=fvg_c)

    async def _on_active_price(self, inst: StrategyInstance, price: float) -> None:
        """ACTIVE tick 更新 MFE；达到 breakeven_r 立即移动保本。"""
        lot = self._broker.get(inst.lot_id) if inst.lot_id else None
        if lot is None or lot.state in ("CLOSING", "CLOSED", "CANCELLED", "FAILED", DONE):
            return
        if inst.r_dist <= 0:
            return
        sign = 1 if inst.side == "long" else -1
        inst.unrealized_r = round((price - inst.entry_px) * sign / inst.r_dist, 3)
        inst.mfe_r = max(inst.mfe_r, inst.unrealized_r)
        await self._try_breakeven(inst)

    async def _try_breakeven(self, inst: StrategyInstance) -> None:
        if inst.breakeven_done or inst.r_dist <= 0:
            return
        threshold = self._param(inst.symbol, "breakeven_r", 2.0)
        if inst.unrealized_r < threshold:
            return
        now = time.time()
        if now - inst.breakeven_attempt_at < 5.0:
            return
        inst.breakeven_attempt_at = now
        ok = await self._broker.move_stop(inst.lot_id, round(inst.entry_px, 2))
        if ok:
            inst.breakeven_done = True
            inst.stop_px = round(inst.entry_px, 2)
            self._emit("auto_breakeven", symbol=inst.symbol, side=inst.side,
                       unrealized_r=inst.unrealized_r, new_stop=inst.stop_px,
                       lot_id=inst.lot_id)
            logger.info("策略保本 %s 浮盈 %.2fR，止损移成本 %.2f",
                        inst.symbol, inst.unrealized_r, inst.stop_px)

    async def _position_details(self, plan: EntryPlan) -> dict:
        equity = None
        if self._gw is not None:
            try:
                equity = await self._gw.net_liquidation()
            except Exception:
                equity = None
        qty = (self._risk.size_position(plan.limit_px, plan.stop_px, equity)
               if self._risk is not None else 0)
        dist = abs(plan.limit_px - plan.stop_px)
        amount = round(qty * plan.limit_px, 2)
        return {
            "equity": equity,
            "qty": qty,
            "risk_usd": round(qty * dist, 2),
            "amount": amount,
            "position_pct": (round(amount / equity * 100, 2)
                             if equity else None),
        }

    async def _propose_entry(self, inst: StrategyInstance, plan: EntryPlan,
                             ts_c: dict, bar_c: dict,
                             fvg_c: Optional[dict] = None) -> None:
        bt = bar_c.get("time") or 0
        inst.state = AWAITING_CONFIRM
        inst.entry_plan = plan
        inst.proposal_processing = True
        inst.last_proposal_bar_time = bt
        details = await self._position_details(plan)
        if inst.state != AWAITING_CONFIRM:
            return
        fvg_c = fvg_c or {}
        divergence = ts_c.get("divergence")
        if divergence is None:
            ema_f = ts_c.get("ema_fast")
            ema_s = ts_c.get("ema_slow")
            atr = ts_c.get("atr")
            if ema_f is not None and ema_s is not None and atr and atr > 0:
                divergence = abs(ema_f - ema_s) / atr
        self._proposal_seq += 1
        now = time.time()
        proposal = {
            "proposal_id": f"ep-{self._proposal_seq:04d}",
            "inst_id": inst.inst_id,
            "symbol": inst.symbol,
            "strategy": inst.strategy,
            "side": plan.side,
            "line": plan.line,
            "limit_px": plan.limit_px,
            "stop_px": plan.stop_px,
            "reason": plan.reason,
            "trend_state": ts_c.get("trend_state"),
            "ema_fast": ts_c.get("ema_fast"),
            "ema_9": ts_c.get("ema_9"),
            "ema_slow": ts_c.get("ema_slow"),
            "atr": ts_c.get("atr"),
            "divergence": (round(abs(divergence), 3)
                           if divergence is not None else None),
            "st_duration_bars": ts_c.get("st_duration_bars"),
            "st_push_count": ts_c.get("st_push_count"),
            # FVG 快照（候选卡片人工比较用；缺失为 None）
            "fvg_cum_count": fvg_c.get("fvg_cum_count"),
            "fvg_last_dir": fvg_c.get("fvg_last_dir"),
            "fvg_last_top": fvg_c.get("fvg_last_top"),
            "fvg_last_bottom": fvg_c.get("fvg_last_bottom"),
            "qty": details["qty"],
            "risk_usd": details["risk_usd"],
            "amount": details["amount"],
            "position_pct": details["position_pct"],
            "bar_time": bt,
            "created_at": now,
            "expires_at": now + BAR_SECONDS,
            "error": "",
        }
        inst.proposal = proposal
        inst.proposal_bar_c = dict(bar_c)
        inst.proposal_processing = False
        inst.updated_at = now
        self._emit("auto_entry_proposed", proposal_id=proposal["proposal_id"],
                   symbol=inst.symbol, side=plan.side, line=plan.line,
                   limit_px=plan.limit_px, stop_px=plan.stop_px,
                   divergence=proposal["divergence"], qty=proposal["qty"],
                   amount=proposal["amount"], bar_time=bt,
                   st_duration_bars=proposal["st_duration_bars"],
                   st_push_count=proposal["st_push_count"])
        logger.info("开仓候选 %s %s %s线 @%.2f 损 %.2f，等待人工确认",
                    inst.symbol, plan.side, plan.line,
                    plan.limit_px, plan.stop_px)

    def _find_proposal(self, proposal_id: str) -> Optional[StrategyInstance]:
        for inst in self._instances.values():
            if (inst.state == AWAITING_CONFIRM and inst.proposal
                    and inst.proposal.get("proposal_id") == proposal_id):
                return inst
        return None

    @staticmethod
    def _clear_proposal(inst: StrategyInstance) -> None:
        inst.entry_plan = None
        inst.proposal = None
        inst.proposal_bar_c = {}
        inst.proposal_processing = False

    def _expire_entry(self, inst: StrategyInstance, reason: str = "bar_close") -> None:
        proposal = dict(inst.proposal or {})
        self._emit("auto_entry_expired",
                   proposal_id=proposal.get("proposal_id"),
                   symbol=inst.symbol, reason=reason)
        inst.state = WATCHING
        self._clear_proposal(inst)
        inst.updated_at = time.time()

    async def approve_entry(self, proposal_id: str,
                            bar_c: Optional[dict] = None):
        """人工批准候选。返回 (结果, 错误)；批准前不会创建任何订单。"""
        inst = self._find_proposal(proposal_id)
        if inst is None:
            return None, "候选不存在或已处理"
        if inst.proposal_processing:
            return None, "候选正在处理，请勿重复点击"
        proposal = dict(inst.proposal or {})
        current_bt = (bar_c or {}).get("time") or 0
        proposal_bt = proposal.get("bar_time") or 0
        if (time.time() >= proposal.get("expires_at", 0)
                or (current_bt and proposal_bt and current_bt > proposal_bt)):
            self._expire_entry(inst, "approval_too_late")
            return None, "候选已过期，请等待新信号"
        strategy = self._strategies.get(inst.symbol)
        plan = inst.entry_plan
        if strategy is None or plan is None:
            return None, "候选策略数据缺失"
        inst.proposal_processing = True
        inst.proposal["error"] = ""
        self._emit("auto_entry_approved", proposal_id=proposal_id,
                   symbol=inst.symbol, side=plan.side, line=plan.line,
                   limit_px=plan.limit_px, stop_px=plan.stop_px)
        lot, error = await self._open(inst, strategy, plan,
                                      bar_c or inst.proposal_bar_c)
        if error:
            if inst.state == AWAITING_CONFIRM:
                inst.proposal_processing = False
                if inst.proposal is not None:
                    inst.proposal["error"] = error
                if time.time() >= proposal.get("expires_at", 0):
                    self._expire_entry(inst, "approval_failed_expired")
            return None, error
        result = {
            "proposal_id": proposal_id,
            "symbol": inst.symbol,
            "side": inst.side,
            "qty": inst.qty,
            "lot_id": lot.lot_id,
        }
        self._clear_proposal(inst)
        return result, ""

    async def reject_entry(self, proposal_id: str, reason: str = ""):
        """人工忽略候选；同一 bar 不重复产生该实例的候选。"""
        inst = self._find_proposal(proposal_id)
        if inst is None:
            return None, "候选不存在或已处理"
        if inst.proposal_processing:
            return None, "候选正在处理，不能忽略"
        proposal = dict(inst.proposal or {})
        self._emit("auto_entry_rejected", proposal_id=proposal_id,
                   symbol=inst.symbol, side=proposal.get("side"),
                   reason=reason)
        inst.state = WATCHING
        self._clear_proposal(inst)
        inst.updated_at = time.time()
        return {"proposal_id": proposal_id, "symbol": inst.symbol,
                "status": "rejected"}, ""

    def _try_rearm(self, symbol: str, inst: StrategyInstance) -> None:
        """实例 DONE 后重新武装；EMA9 成交轮次须先达到 reentry_min_mfe_r。
        未成交撤单不受该门槛限制；收养实例不重新武装。"""
        strategy = self._strategies.get(symbol)
        if strategy is None or inst.adopted:
            return
        if inst.entries_today >= strategy.params["max_entries_per_day"]:
            return
        if inst.activated and inst.line == "ema9":
            min_mfe = float(strategy.params.get("reentry_min_mfe_r", 2.0))
            if inst.mfe_r + 1e-9 < min_mfe:
                return
        inst.state = WATCHING
        self._clear_proposal(inst)
        inst.side = ""
        inst.lot_id = None
        inst.entry_px = 0.0
        inst.stop_px = 0.0
        inst.init_entry_px = 0.0
        inst.init_stop_px = 0.0
        inst.r_dist = 0.0
        inst.qty = 0
        inst.line = ""
        inst.activated = False
        inst.breakeven_done = False
        inst.breakeven_attempt_at = 0.0
        inst.unrealized_r = 0.0
        inst.mfe_r = 0.0
        inst.done_reason = ""
        self._emit("auto_rearmed", symbol=symbol,
                   entries_today=inst.entries_today)
        logger.info("实例重新武装 %s（当日已进场 %d 次）",
                    symbol, inst.entries_today)

    async def _open(self, inst: StrategyInstance, strategy: AutoStrategy,
                    plan: EntryPlan, bar_c: dict):
        if inst.entries_today >= strategy.params["max_entries_per_day"]:
            return None, "每标的每日进场上限已到"
        details = await self._position_details(plan)
        qty = details["qty"]
        if qty <= 0:
            logger.warning("%s 以损定量为 0，放弃进场", plan.symbol)
            return None, "以损定量为 0，无法开仓"
        try:
            lot = await self._broker.place_limit_stop(
                symbol=plan.symbol, side=plan.side, qty=qty,
                limit_px=plan.limit_px, stop_px=plan.stop_px,
                limit_type=f"auto_{strategy.name}",
                reason=f"auto:{strategy.name} {plan.line} {plan.reason}"[:80])
        except Exception as e:
            logger.warning("策略下单失败 %s: %s", plan.symbol, e)
            self._emit("auto_order_failed", symbol=plan.symbol, error=str(e))
            return None, str(e)
        inst.state = PENDING
        inst.side = plan.side
        inst.lot_id = lot.lot_id
        inst.entry_px = plan.limit_px
        inst.stop_px = plan.stop_px
        inst.init_entry_px = plan.limit_px
        inst.init_stop_px = plan.stop_px
        inst.r_dist = abs(plan.limit_px - plan.stop_px)
        inst.qty = qty
        inst.line = plan.line
        inst.entries_today += 1
        inst.created_bar_time = bar_c.get("time") or 0
        inst.updated_at = time.time()
        self._emit("auto_order_placed", symbol=plan.symbol, side=plan.side,
                   qty=qty, limit_px=plan.limit_px, stop_px=plan.stop_px,
                   line=plan.line, lot_id=lot.lot_id, inst_id=inst.inst_id)
        logger.info("策略建仓 %s %s %s线 x%d @%.2f 损 %.2f",
                    plan.symbol, plan.side, plan.line, qty,
                    plan.limit_px, plan.stop_px)
        return lot, ""

    # ---- bar 收盘入口（管理；PENDING/ACTIVE 专用）----

    async def on_bar(self, symbol: str, bar: dict, ts_c: dict) -> None:
        inst = self._instances.get(symbol)
        if inst is None:
            return
        bt = bar.get("time") or 0
        if inst.state == AWAITING_CONFIRM:
            proposal = inst.proposal or {}
            proposal_bt = proposal.get("bar_time") or 0
            expired = ((bt and proposal_bt and bt > proposal_bt)
                       or (proposal and time.time() >= proposal.get("expires_at", 0)))
            if expired and not inst.proposal_processing:
                self._expire_entry(inst)
            return
        if inst.state in (WATCHING, DONE):
            return
        lot = self._broker.get(inst.lot_id) if inst.lot_id else None
        if lot is not None and lot.state == "CLOSING":
            return   # 平仓等待实际成交期间暂停保本/跟踪/重复平仓

        # 0) 实例所管 lot 的终态检测（手动取消/平仓、STP 触发、对账收敛）
        if lot is not None and lot.state in ("CLOSED", "CANCELLED", "FAILED", DONE):
            self._finish(inst, getattr(lot, "exit_reason", "") or lot.state.lower())
            return

        if inst.state == PENDING:
            # 成交检测（实盘 watcher 会置 ACTIVE；SimBroker on_price 已置）
            if lot is not None and lot.state == ACTIVE:
                inst.state = ACTIVE
                inst.activated = True
                if inst.init_entry_px <= 0:
                    inst.init_entry_px = inst.entry_px
                if inst.init_stop_px <= 0:
                    inst.init_stop_px = inst.stop_px
                if inst.r_dist <= 0:
                    inst.r_dist = abs(inst.init_entry_px - inst.init_stop_px)
                avg = getattr(lot, "avg_fill", 0) or inst.entry_px
                inst.entry_px = avg   # 实际成本用于保本/现价浮盈，r_dist 保持下单初始风险
                self._emit("auto_filled", symbol=symbol, side=inst.side,
                           qty=inst.qty, avg_fill=avg, lot_id=inst.lot_id)
                logger.info("策略成交 %s %s x%d @%.2f",
                            symbol, inst.side, inst.qty, avg)
                # 成交后的第一次 bar 收盘就做一次管理（落到下面 ACTIVE 段）
            else:
                # 埋单过期：N 根 bar 未成交 → 撤单回 WATCHING（不追价）
                strategy = self._strategies.get(symbol)
                expire = (strategy.params["expire_bars"] if strategy
                          else 3) * BAR_SECONDS
                if bt and inst.created_bar_time \
                        and bt - inst.created_bar_time >= expire:
                    expired_lot = inst.lot_id
                    ok = await self._broker.cancel(inst.lot_id)
                    if ok:
                        inst.state = WATCHING
                        inst.lot_id = None
                        self._emit("auto_expired", symbol=symbol, ok=ok,
                                   lot_id=expired_lot, inst_id=inst.inst_id)
                        logger.info("埋单过期回 WATCHING %s", symbol)
                    else:
                        logger.warning("埋单过期撤单失败 %s lot %s，保持 PENDING 等待重试",
                                       symbol, expired_lot)
                return

        # ACTIVE：tick MFE 保本；bar 收盘更新浮盈并管理跟踪/反向
        if inst.state != ACTIVE or inst.r_dist <= 0:
            return
        close = bar.get("close")
        if close:
            sign = 1 if inst.side == "long" else -1
            inst.unrealized_r = round(
                (close - inst.entry_px) * sign / inst.r_dist, 3)
            inst.mfe_r = max(inst.mfe_r, inst.unrealized_r)
            await self._try_breakeven(inst)

        # ST 越过实际成本后即可跟踪；已保本后继续跟踪（单向棘轮，1 美分防抖）
        trail_ready = inst.breakeven_done
        if not trail_ready:
            st_ready = ts_c.get("supertrend")
            trail_ready = bool(
                st_ready and st_ready > 0 and (
                    (inst.side == "long" and st_ready > inst.entry_px + 0.01)
                    or (inst.side == "short" and st_ready < inst.entry_px - 0.01)))
        if trail_ready and close:
            st = ts_c.get("supertrend")
            if st and st > 0:
                better = (st > inst.stop_px + 0.01) if inst.side == "long" \
                    else (st < inst.stop_px - 0.01)
                # 止损不能越过当前价（会立即触发）
                sane = (st < close) if inst.side == "long" else (st > close)
                if better and sane:
                    ok = await self._broker.move_stop(inst.lot_id, round(st, 2))
                    if ok:
                        old = inst.stop_px
                        inst.stop_px = round(st, 2)
                        self._emit("auto_trail", symbol=symbol, side=inst.side,
                                   old_stop=old, new_stop=inst.stop_px,
                                   lot_id=inst.lot_id)
                        logger.info("ST 跟踪 %s 止损 %.2f → %.2f",
                                    symbol, old, inst.stop_px)

        # 3) 趋势反向 → 平仓
        direction = ts_c.get("direction")
        flipped = (inst.side == "long" and direction == -1) or \
                  (inst.side == "short" and direction == 1)
        if flipped:
            ref = close or inst.entry_px
            ok = await self._broker.close(inst.lot_id, ref, "trend_reverse",
                                          ts=bt)
            self._emit("auto_reverse_close", symbol=symbol, side=inst.side,
                       ref_px=ref, ok=ok, lot_id=inst.lot_id,
                       status=getattr(lot, "state", ""))
            if ok:
                logger.info("趋势反向平仓已提交 %s %s @%.2f", symbol, inst.side, ref)
                if getattr(lot, "state", "") in ("CLOSED", DONE):
                    self._finish(inst, "trend_reverse")
            else:
                logger.warning("趋势反向平仓失败 %s lot %s，保持 ACTIVE 等待重试",
                               symbol, inst.lot_id)

    # ---- 回测专用：bar 极值模拟 tick（口径统一，见模块 docstring）----

    async def on_bar_extremes(self, symbol: str, bar: dict, ts_c_prev: dict,
                              bar_c_prev: dict, now_hm: int,
                              fvg_c: Optional[dict] = None) -> None:
        """回测 feed 用：bar 内 tick 路径不可知，用极值建模——
        多头看 low（触线/触止损），空头看 high。先推进 broker 成交与
        ACTIVE tick 保本，再交给随后的 on_bar 做跟踪/反向管理。"""
        inst = self._instances.get(symbol)
        side_hint = inst.side if inst and inst.state != WATCHING else (
            "long" if (ts_c_prev.get("trend_state") == 1) else "short")
        bt = bar.get("time") or 0.0
        low, high = bar.get("low"), bar.get("high")
        if hasattr(self._broker, "on_price"):
            # 两个极值都喂给 broker：有利极值先行（MFE 跟踪），不利极值随后
            # （止损触发/限价成交）——顺序固定 = 确定性
            first, second = ((high, low) if side_hint == "long"
                             else (low, high))
            for px in (first, second):
                if px:
                    self._broker.on_price(symbol, px, ts=bt)
                    await self.on_price(symbol, px, ts_c_prev, bar_c_prev, now_hm,
                                        fvg_c=fvg_c)
        # 触发/成交用的极端价：多 low / 空 high
        touch = low if side_hint == "long" else high
        if touch:
            await self.on_price(symbol, touch, ts_c_prev, bar_c_prev, now_hm,
                                fvg_c=fvg_c)
            # 触发后补两次成交回填：第 1 次让新挂单当根成交（实盘 tick 语义），
            # 第 2 次让刚成交的仓位当根检查止损——同根既触线又穿止损时
            # 按保守口径算止损触发（bar 内时序不可知，宁取不利）
            if hasattr(self._broker, "on_price"):
                self._broker.on_price(symbol, touch, ts=bt)
                self._broker.on_price(symbol, touch, ts=bt)

    # ---- 内部 ----

    def _param(self, symbol: str, key: str, default):
        s = self._strategies.get(symbol)
        return s.params.get(key, default) if s else default

    def _finish(self, inst: StrategyInstance, reason: str) -> None:
        inst.state = DONE
        inst.done_reason = reason
        inst.updated_at = time.time()
        self._done.append(inst)
        strategy = self._strategies.get(inst.symbol)
        if strategy is not None:
            try:
                strategy.on_trade_closed(inst, {"reason": reason})
            except Exception:
                logger.exception("on_trade_closed 钩子异常 %s", inst.symbol)
        self._emit("auto_done", symbol=inst.symbol, side=inst.side,
                   reason=reason, mfe_r=inst.mfe_r,
                   entries_today=inst.entries_today, adopted=inst.adopted,
                   lot_id=inst.lot_id)
        logger.info("策略实例结束 %s (%s)，MFE %.2fR",
                    inst.symbol, reason, inst.mfe_r)
