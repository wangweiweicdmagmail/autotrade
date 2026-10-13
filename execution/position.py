# -*- coding: utf-8 -*-
"""
仓位状态机 (PositionManager)
============================

管理单个仓位单元的生命周期（参考 nautilus_ibkr_helloworld 的
PENDING_ENTRY → ACTIVE → CLOSED 单元模型）：

    人工批准提案
      -> open_position(): 过风控 -> 提交入场单 -> PENDING_ENTRY
      -> 入场成交（watcher 轮询订单状态）-> 挂保护止损/止盈 -> ACTIVE
      -> 止损/止盈成交 或 人工在 TWS 平仓（对账发现净仓=0）-> CLOSED

追踪止损（棘轮式，继承 nautilus 经验）：
- broker 端常驻 STOP_MARKET GTC（引擎宕机也有保护）；
- 每根 bar 收盘由 main.py 用指标线值调 update_trailing_stop()，
  只许向有利方向移动（多仓只升、空仓只降），变化 < 0.01 不提交（防抖）。

dry-run 模式下入场单立即"虚拟成交"，直接进入 ACTIVE，
全流程可演练而真实账户零风险。
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field, asdict
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ORDERS_LOG = PROJECT_ROOT / "logs" / "orders.jsonl"
DRYRUN_LOG = PROJECT_ROOT / "logs" / "dryrun_orders.jsonl"

# 追踪止损防抖：变化小于 1 美分不提交
_RATCHET_EPS = 0.01


class UnitState(str, Enum):
    PENDING_ENTRY = "PENDING_ENTRY"   # 入场单已提交未成交
    ACTIVE = "ACTIVE"                 # 已成交，保护单在挂
    FAILED = "FAILED"                 # 入场失败
    CLOSED = "CLOSED"


@dataclass
class PositionUnit:
    symbol: str
    side: str               # 'long' | 'short'
    qty: int
    entry_px: float
    stop_px: float          # TRAIL 初始触发价（1M ST(10,2) 线值）
    trail_amount: float = 0.0   # TRAIL 跟踪幅度（2×ATR）
    reason: str = ""
    state: UnitState = UnitState.PENDING_ENTRY
    entry_order_id: Optional[int] = None
    trail_order_id: Optional[int] = None
    filled_qty: int = 0
    avg_fill: float = 0.0
    opened_at: float = field(default_factory=time.time)
    closed_at: float = 0.0
    trail_trade: object = None        # ib_async Trade 句柄（dry-run 为 None）
    notes: List[str] = field(default_factory=list)


class PositionManager:
    """
    用法::

        pm = PositionManager(gateway, risk_gate)
        unit = await pm.open_position(proposal)   # proposal: engine Proposal
        await pm.update_trailing_stop(symbol, new_stop)  # 每根 bar 收盘
        await pm.reconcile(broker_positions)             # 周期对账
    """

    def __init__(self, gateway, risk_gate,
                 orders_log: Path = ORDERS_LOG) -> None:
        self._gw = gateway
        self._risk = risk_gate
        self._log_path = Path(orders_log)
        self.units: Dict[str, PositionUnit] = {}   # symbol -> 活跃单元

    # ------------------------------------------------------------------
    # 开仓
    # ------------------------------------------------------------------

    async def open_position(self, proposal) -> Optional[PositionUnit]:
        """人工批准的提案入口：风控 -> 括号单（入场+TRAIL 子单）-> 状态机。"""
        daily_pnl = await self._gw.daily_pnl()
        ok, reason = self._risk.check_new_order(
            proposal.symbol, proposal.side, proposal.entry, proposal.stop,
            proposal.qty, open_positions=len(self.units),
            daily_pnl=daily_pnl)
        if not ok:
            logger.warning("🚫 风控拦截 %s %s: %s",
                           proposal.symbol, proposal.side, reason)
            self._log("risk_blocked", proposal, {"reason": reason})
            return None

        unit = PositionUnit(
            symbol=proposal.symbol, side=proposal.side, qty=proposal.qty,
            entry_px=proposal.entry, stop_px=proposal.stop,
            trail_amount=getattr(proposal, "trail_amount", 0.0),
            reason=proposal.reason,
        )
        result = await self._gw.place_bracket(unit)
        unit.entry_order_id = result.get("entry_order_id")
        unit.trail_order_id = result.get("trail_order_id")
        if not result.get("ok"):
            unit.state = UnitState.FAILED
            unit.notes.append(f"括号单失败: {result.get('error')}")
            self._log("entry_failed", unit, {"error": result.get("error")})
            return None

        self.units[unit.symbol] = unit
        self._log("bracket_submitted", unit,
                  {"limit_price": result.get("limit_price")})

        if result.get("dry_run"):
            # dry-run：立即虚拟成交，TRAIL 视为已在 broker 托管
            unit.state = UnitState.ACTIVE
            unit.filled_qty = unit.qty
            unit.avg_fill = unit.entry_px
            self._log("dryrun_filled", unit, {})
            logger.info("[DRY-RUN] %s 虚拟成交，单元 ACTIVE "
                        "(qty=%d TRAIL 触发 %.2f 幅度 %.2f)",
                        unit.symbol, unit.qty, unit.stop_px,
                        unit.trail_amount)
        else:
            # 实盘：watcher 盯入场单；TRAIL 子单已在 broker 端待命
            asyncio.create_task(
                self._watch_entry(unit, result["trade"],
                                  result.get("trail_trade")))
        return unit

    async def _watch_entry(self, unit: PositionUnit, trade,
                           trail_trade=None) -> None:
        """实盘模式：盯入场单直到 Filled/Cancelled（v1 轮询，后续改事件）。
        TRAIL 子单已随括号单下发，入场成交即自动生效，无需再挂保护。"""
        while not trade.isDone():
            await asyncio.sleep(1)
        status = trade.orderStatus.status
        if status == "Filled":
            unit.state = UnitState.ACTIVE
            unit.filled_qty = int(trade.orderStatus.filled)
            unit.avg_fill = float(trade.orderStatus.avgFillPrice)
            unit.trail_trade = trail_trade
            self._log("entry_filled", unit, {})
            logger.info("✅ 入场成交 %s x%d @%.2f，TRAIL 跟踪止损已生效",
                        unit.symbol, unit.filled_qty, unit.avg_fill)
        else:
            unit.state = UnitState.FAILED
            unit.notes.append(f"入场单终态 {status}")
            self.units.pop(unit.symbol, None)
            # 入场未成交（DAY 单过期/被撤）：撤掉 TRAIL 子单防残留
            if trail_trade is not None and not trail_trade.isDone():
                try:
                    self._gw.ib.cancelOrder(trail_trade.order)
                except Exception:
                    logger.exception("%s 撤 TRAIL 子单失败", unit.symbol)
            self._log("entry_not_filled", unit, {"status": status})
            logger.warning("%s 入场单未成交 (%s)，单元关闭",
                           unit.symbol, status)

    # ------------------------------------------------------------------
    # 追踪止损（棘轮）
    # ------------------------------------------------------------------

    async def update_trailing_stop(self, symbol: str,
                                   indicator_stop: float) -> bool:
        """
        手动调整 TRAIL 触发价（只向有利方向移动，1 美分防抖）。
        注意：native TRAIL 单由 broker 按 trail_amount 自动跟踪，
        正常无需每轮调用——此方法留给"用指标线主动收紧"的场景。
        """
        unit = self.units.get(symbol)
        if unit is None or unit.state != UnitState.ACTIVE:
            return False
        cur = unit.stop_px
        new = max(cur, indicator_stop) if unit.side == "long" \
            else min(cur, indicator_stop)
        if abs(new - cur) < _RATCHET_EPS:
            return False
        ok = await self._gw.modify_stop(unit, round(new, 2))
        if ok:
            self._log("stop_ratched", unit, {"new_stop": new})
        return ok

    # ------------------------------------------------------------------
    # 平仓
    # ------------------------------------------------------------------

    async def close_position(self, unit: PositionUnit,
                             reason: str = "manual") -> None:
        """市价平仓（marketable LIMIT）。先撤保护挂单防止残留反向裸仓。"""
        # TODO(v2): 撤 OCA 组挂单 -> 反向 marketable 单；v1 dry-run 直接记状态
        unit.state = UnitState.CLOSED
        unit.closed_at = time.time()
        self.units.pop(unit.symbol, None)
        self._log("closed", unit, {"reason": reason})
        logger.info("仓位关闭 %s (%s)", unit.symbol, reason)

    # ------------------------------------------------------------------
    # 对账
    # ------------------------------------------------------------------

    async def reconcile(self) -> List[str]:
        """
        与 IBKR 实际持仓比对，返回异常描述列表。
        护栏（继承 nautilus 经验）：broker 净仓=0 但本地单元 ACTIVE 且
        止损单仍在挂时，疑似 FA 子账户盲区，只告警不自动处置。
        """
        anomalies: List[str] = []
        broker = await self._gw.positions()
        for sym, unit in list(self.units.items()):
            ticker = sym.split(":")[-1]
            net = broker.get(ticker, 0.0)
            if unit.state == UnitState.ACTIVE and net == 0:
                msg = (f"{sym}: broker 净仓=0 但本地 ACTIVE"
                       f"（TRAIL 单 {unit.trail_order_id} 或仍活跃，"
                       "疑似 FA 盲区/已被 TWS 手动平仓）——告警不自动处置")
                anomalies.append(msg)
                logger.warning("对账异常: %s", msg)
        for ticker, net in broker.items():
            sym = next((s for s in self.units
                        if s.split(":")[-1] == ticker), None)
            if sym is None and net != 0:
                msg = f"{ticker}: broker 持仓 {net} 股但本地无单元（外部开仓）"
                anomalies.append(msg)
                logger.warning("对账异常: %s", msg)
        return anomalies

    # ------------------------------------------------------------------
    # 日志
    # ------------------------------------------------------------------

    def _log(self, event: str, obj, extra: dict) -> None:
        try:
            data = asdict(obj) if hasattr(obj, "__dataclass_fields__") \
                else dict(obj)
        except Exception:
            data = {"repr": repr(obj)}
        data.pop("trail_trade", None)
        rec = {"ts": time.time(), "event": event, **data, **extra}
        path = DRYRUN_LOG if not self._gw.enabled else self._log_path
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False, default=str)
                        + "\n")
        except OSError:
            logger.exception("订单日志写入失败")
