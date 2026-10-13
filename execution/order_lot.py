# -*- coding: utf-8 -*-
"""
分批建仓：订单批次管理器 (OrderLot / OrderManager)
==================================================

核心语义（2026-10-04 定稿，配合 Web 下单控制台）：

- 一次「下单」= 一个 **OrderLot（批次）** = 一个 LMT 入场单 + 一个 TRAIL 跟踪
  止损单（括号单，复用 IBKRGateway.place_bracket）。
- 同一只股票可以连续下多个 lot，每个 lot 拥有**独立的**跟踪止损触发价、
  跟踪幅度、订单号与盈亏（PnL）。股价往前走时，各自的 TRAIL 各自跟着 K 线走。
- 对外「聚合」成仓位视图：同一 symbol 的多个 lot 合并显示净股数与合计 PnL，
  但底层每个 lot 独立管理、独立改 TRAIL、独立撤单。

与旧的 execution/position.py（symbol -> 单 PositionUnit）不同：本模块是
**lot 粒度**（symbol -> 多个 OrderLot），专门服务「分批建仓 + 逐单管理」。
旧的 PositionManager 仍被 main.py 使用，两者并存、互不影响。

风控：开仓前仍过 RiskGate.check_new_order（止损合法性 / 持仓数按 distinct
symbol 计 / 日亏损熔断 / qty 上限），「持仓数上限」按 symbol 聚合而非 lot 数。
"""

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
STATE_PATH = PROJECT_ROOT / "logs" / "lots_state.json"

# lot 状态机
PENDING = "PENDING"     # 入场单已提交、未成交
ACTIVE = "ACTIVE"       # 已成交、TRAIL 在 broker 端跟踪
CANCELLED = "CANCELLED" # 已撤单
FAILED = "FAILED"       # 下单失败
CLOSED = "CLOSED"       # 已平仓（预留）

_OPEN_STATES = {PENDING, ACTIVE}


@dataclass
class OrderLot:
    """一个独立的下单批次：LMT 入场 + TRAIL 跟踪止损（括号单）。"""

    lot_id: str
    symbol: str
    side: str                       # 'long' | 'short'
    qty: int                        # 目标股数
    entry_px: float                 # LMT 限价（入场参考价）
    stop_px: float                  # TRAIL 初始触发价（独立跟踪止损线）
    trail_amount: float             # TRAIL 跟踪幅度（美元/股）
    state: str = PENDING
    entry_order_id: Optional[int] = None
    trail_order_id: Optional[int] = None
    filled_qty: int = 0
    avg_fill: float = 0.0           # 成交均价
    last_px: float = 0.0            # mark 价（PnL 计算用；dry-run = avg_fill）
    pnl: float = 0.0                # 未实现盈亏（美元）
    reason: str = ""                # 下单理由/备注
    note: str = ""
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    # ---- 复盘/绩效字段（平仓闭环）----
    limit_type: str = ""            # 信号类型：ema / channel（ST回踩）
    entry_time: float = 0.0         # 实际成交时间戳（无成交则 0）
    exit_px: float = 0.0            # 平仓成交价
    closed_at: float = 0.0          # 平仓时间戳
    realized_pnl: float = 0.0       # 实现盈亏（美元，平仓后落定）
    exit_reason: str = ""           # 平仓原因：manual / stop / eod
    signal_snapshot: dict = field(default_factory=dict)  # 下单时指标快照

    # ---- 风险回报复盘指标 ----
    take_profit_px: float = 0.0     # 目标止盈价
    target_rr: float = 0.0          # 目标盈亏比（设计 RR = 止盈距离/止损距离）
    actual_rr: float = 0.0          # 真实盈亏比（实现盈亏 / 计划风险金额）
    mae: float = 0.0                # 最大浮亏 MAE（最不利偏移，≤0，美元）
    mfe: float = 0.0                # 最大浮盈 MFE（最有利偏移，≥0，美元）

    # ib_async Trade 句柄，仅内存持有，不序列化
    entry_trade: object = field(default=None, repr=False, compare=False)
    trail_trade: object = field(default=None, repr=False, compare=False)

    def recalc_pnl(self) -> None:
        """按 mark 价重算未实现盈亏（PENDING 未成交则为 0）。"""
        if self.state != ACTIVE or self.filled_qty <= 0 or self.last_px <= 0:
            self.pnl = 0.0
            return
        if self.side == "long":
            self.pnl = round((self.last_px - self.avg_fill) * self.filled_qty, 2)
        else:
            self.pnl = round((self.avg_fill - self.last_px) * self.filled_qty, 2)

    def to_dict(self) -> dict:
        d = asdict(self)
        d.pop("entry_trade", None)
        d.pop("trail_trade", None)
        return d

    @staticmethod
    def from_dict(d: dict) -> "OrderLot":
        d = {k: v for k, v in d.items() if k not in ("entry_trade", "trail_trade")}
        return OrderLot(**d)


class OrderManager:
    """分批建仓的批次管理器：下单 / 聚合 / 改 TRAIL / 撤单 / 持久化。"""

    def __init__(self, gateway, risk_gate, state_path: Path = STATE_PATH) -> None:
        self._gw = gateway
        self._risk = risk_gate
        self._state_path = Path(state_path)
        self._lots: Dict[str, OrderLot] = {}
        self._recent_places: Dict[tuple, float] = {}   # 下单幂等：近期相同单时间戳
        self._load()

    # ------------------------------------------------------------------
    # 下单（分批建仓：每次调用 = 一个独立 lot）
    # ------------------------------------------------------------------

    async def place_lot(self, symbol: str, side: str, qty: int,
                        entry_px: float, stop_px: float, trail_amount: float,
                        reason: str = "") -> Tuple[Optional[OrderLot], str]:
        """手动下单一个批次。返回 (lot, 错误信息)。"""
        symbol = symbol.strip().upper()
        side = side.strip().lower()
        if side not in ("long", "short"):
            return None, f"方向非法: {side}（应为 long / short）"
        if qty <= 0:
            return None, "qty 必须为正整数"
        if entry_px <= 0 or stop_px <= 0:
            return None, "入场价/止损价必须为正"
        if trail_amount <= 0:
            return None, "TRAIL 跟踪幅度必须为正（美元/股）"

        # 风控：持仓数按 distinct symbol 聚合（不是 lot 数）
        open_positions = len({l.symbol for l in self._lots.values()
                              if l.state in _OPEN_STATES})
        realized = await self._gw.realized_pnl()  # 熔断基准：今日已实现盈亏
        ok, why = self._risk.check_new_order(
            symbol, side, entry_px, stop_px, qty,
            open_positions=open_positions, daily_pnl=realized)
        if not ok:
            logger.warning("🚫 风控拦截 %s %s: %s", symbol, side, why)
            return None, why

        lot = OrderLot(
            lot_id=f"lot-{uuid.uuid4().hex[:8]}",
            symbol=symbol, side=side, qty=int(qty),
            entry_px=float(entry_px), stop_px=float(stop_px),
            trail_amount=float(trail_amount), reason=reason,
        )
        result = await self._gw.place_bracket(lot)
        lot.entry_order_id = result.get("entry_order_id")
        lot.trail_order_id = result.get("trail_order_id")
        if not result.get("ok"):
            lot.state = FAILED
            lot.note = f"括号单失败: {result.get('error')}"
            self._lots[lot.lot_id] = lot
            self._save()
            return None, lot.note

        if result.get("dry_run"):
            # dry-run：立即虚拟成交，TRAIL 视为已托管
            lot.state = ACTIVE
            lot.filled_qty = lot.qty
            lot.avg_fill = lot.entry_px
            lot.last_px = lot.entry_px
            lot.recalc_pnl()
            logger.info("[DRY-RUN] lot %s %s %s x%d 虚拟成交，TRAIL 触发 %.2f "
                        "幅度 %.2f", lot.lot_id, symbol, side, lot.qty,
                        lot.stop_px, lot.trail_amount)
        else:
            lot.entry_trade = result.get("trade")
            lot.trail_trade = result.get("trail_trade")
            asyncio.create_task(self._watch_entry(lot))

        self._lots[lot.lot_id] = lot
        self._save()
        return lot, ""

    async def place_limit_stop_lot(self, symbol: str, side: str, qty: int,
                                   limit_px: float, stop_px: float,
                                   limit_type: str = "",
                                   reason: str = "",
                                   risk_usd: float = 0,
                                   signal_snapshot: Optional[dict] = None
                                   ) -> Tuple[Optional[OrderLot], str]:
        """限价单（父）+ 固定 STOP 止损（子，parentId 关联）下单一个批次。"""
        symbol = symbol.strip().upper()
        side = side.strip().lower()
        if side not in ("long", "short"):
            return None, f"方向非法: {side}（应为 long / short）"
        if qty <= 0:
            return None, "qty 必须为正整数"
        if limit_px <= 0 or stop_px <= 0:
            return None, "限价/止损价必须为正"

        # 幂等：2 秒内相同 symbol+side+限价+止损 视为重复提交（防双击/重试）
        key = (symbol, side, round(limit_px, 2), round(stop_px, 2))
        now = time.time()
        last = self._recent_places.get(key)
        if last is not None and now - last < 2.0:
            logger.warning("重复下单拦截 %s %s @%.2f/%.2f", symbol, side,
                           limit_px, stop_px)
            return None, "重复下单（2 秒内相同订单已提交，请勿重复点击）"
        self._recent_places[key] = now

        open_positions = len({l.symbol for l in self._lots.values()
                              if l.state in _OPEN_STATES})
        realized = await self._gw.realized_pnl()  # 熔断基准：今日已实现盈亏
        ok, why = self._risk.check_new_order(
            symbol, side, limit_px, stop_px, qty,
            open_positions=open_positions, daily_pnl=realized, risk_usd=risk_usd)
        if not ok:
            logger.warning("🚫 风控拦截 %s %s: %s", symbol, side, why)
            return None, why

        lot = OrderLot(
            lot_id=f"lot-{uuid.uuid4().hex[:8]}",
            symbol=symbol, side=side, qty=int(qty),
            entry_px=float(limit_px), stop_px=float(stop_px),
            trail_amount=0.0,
            reason=reason or f"{limit_type} 限价单",
            note=f"limit+stop ({limit_type})",
            limit_type=limit_type or "",
            signal_snapshot=signal_snapshot or {},
        )
        result = await self._gw.place_limit_with_stop(
            symbol, side, qty, limit_px, stop_px)
        lot.entry_order_id = result.get("entry_order_id")
        lot.trail_order_id = result.get("stop_order_id")  # 复用字段存 STOP 单 id
        if not result.get("ok"):
            lot.state = FAILED
            lot.note = f"限价+STOP 单失败: {result.get('error')}"
            self._lots[lot.lot_id] = lot
            self._save()
            return None, lot.note

        if result.get("dry_run"):
            lot.state = ACTIVE
            lot.filled_qty = lot.qty
            lot.avg_fill = lot.entry_px
            lot.last_px = lot.entry_px
            lot.entry_time = time.time()   # dry-run 成交时间
            self._set_targets(lot)
            lot.recalc_pnl()
            logger.info("[DRY-RUN] lot %s %s %s x%d 限价 %.2f STOP %.2f 虚拟成交",
                        lot.lot_id, symbol, side, lot.qty, limit_px, stop_px)
        else:
            lot.entry_trade = result.get("trade")
            lot.trail_trade = result.get("stop_trade")
            asyncio.create_task(self._watch_entry(lot))

        self._lots[lot.lot_id] = lot
        self._save()
        return lot, ""

    async def close_lot(self, lot_id: str,
                        ref_px: Optional[float] = None,
                        exit_reason: str = "manual") -> Tuple[bool, str]:
        """平仓一个 lot：撤 STOP 保护单 + 反向 marketable 平仓单（实盘）；dry-run 直接置 CLOSED。"""
        lot = self._lots.get(lot_id)
        if lot is None:
            return False, f"lot 不存在: {lot_id}"
        if lot.state != ACTIVE:
            return False, f"lot 状态 {lot.state} 不可平仓（仅 ACTIVE 可平）"

        is_live = bool(self._gw.enabled and self._gw._connected)

        if not is_live:
            # dry-run：虚拟单无真实持仓，直接置 CLOSED
            self._finalize_close(lot, ref_px or lot.last_px or lot.avg_fill,
                                 exit_reason)
            lot.note = f"已平仓（dry-run/{exit_reason}）"
            self._save()
            return True, ""

        # 实盘：先撤 STOP 保护单（防平仓后残留成反向裸仓）
        if lot.trail_trade is not None and not lot.trail_trade.isDone():
            self._gw.cancel_order(lot.trail_trade)

        # 平仓参考价：显式传入 > 最新 mark 价 > 成交均价
        px = ref_px or lot.last_px or lot.avg_fill
        if px <= 0:
            px = await self._gw.last_price(lot.symbol) or lot.avg_fill
        if px <= 0:
            return False, "无法确定平仓参考价"

        result = await self._gw.place_close(lot.symbol, lot.side, lot.filled_qty, px)
        if not result.get("ok"):
            lot.note = f"平仓失败: {result.get('error')}"
            self._save()
            return False, lot.note

        self._finalize_close(lot, px, exit_reason)
        lot.note = f"已平仓（{exit_reason}）"
        self._save()
        return True, ""

    def _set_targets(self, lot: "OrderLot") -> None:
        """按 2R 目标设置止盈价和目标盈亏比（入场后调用）。"""
        risk_dist = abs(lot.entry_px - lot.stop_px)
        if risk_dist <= 0:
            return
        if lot.side == "long":
            lot.take_profit_px = round(lot.entry_px + 2 * risk_dist, 4)
        else:
            lot.take_profit_px = round(lot.entry_px - 2 * risk_dist, 4)
        lot.target_rr = round(2.0, 2)

    def _finalize_close(self, lot: "OrderLot", exit_px: float,
                        exit_reason: str) -> None:
        """平仓落定：记录平仓价/时间/实现盈亏/真实RR，并追加到历史归档。"""
        lot.state = CLOSED
        lot.exit_px = round(float(exit_px or 0.0), 4)
        lot.closed_at = time.time()
        lot.exit_reason = exit_reason or "manual"
        if lot.avg_fill > 0 and lot.filled_qty > 0:
            if lot.side == "long":
                lot.realized_pnl = round((lot.exit_px - lot.avg_fill) * lot.filled_qty, 2)
            else:
                lot.realized_pnl = round((lot.avg_fill - lot.exit_px) * lot.filled_qty, 2)
        # 真实盈亏比 = 实现盈亏 / 计划风险金额（股数 × 止损距离）
        risk_amt = abs(lot.entry_px - lot.stop_px) * lot.filled_qty
        if risk_amt > 0:
            lot.actual_rr = round(lot.realized_pnl / risk_amt, 3)
        lot.updated_at = time.time()
        self._archive(lot)

    async def _watch_entry(self, lot: OrderLot) -> None:
        """实盘：盯入场单到 Filled/Cancelled，成交后启用 PnL 跟踪。"""
        trade = lot.entry_trade
        if trade is None:
            return
        while not trade.isDone():
            await asyncio.sleep(1)
        status = trade.orderStatus.status
        lot.updated_at = time.time()
        if status == "Filled":
            lot.state = ACTIVE
            lot.filled_qty = int(trade.orderStatus.filled)
            lot.avg_fill = float(trade.orderStatus.avgFillPrice)
            lot.last_px = lot.avg_fill
            lot.entry_time = time.time()
            self._set_targets(lot)
            lot.recalc_pnl()
            logger.info("✅ lot %s %s 成交 x%d @%.2f，TRAIL 已生效",
                        lot.lot_id, lot.symbol, lot.filled_qty, lot.avg_fill)
        else:
            lot.state = FAILED
            lot.note = f"入场单终态 {status}"
            # 入场未成交：撤 TRAIL 子单防残留
            if lot.trail_trade is not None and not lot.trail_trade.isDone():
                self._gw.cancel_order(lot.trail_trade)
            logger.warning("lot %s %s 入场未成交 (%s)", lot.lot_id,
                           lot.symbol, status)
        self._save()

    # ------------------------------------------------------------------
    # 查询与聚合
    # ------------------------------------------------------------------

    def list_lots(self, active_only: bool = False,
                  symbol: Optional[str] = None) -> List[dict]:
        lots = list(self._lots.values())
        if symbol:
            t = symbol.split(":")[-1].upper()
            lots = [l for l in lots if l.symbol.split(":")[-1].upper() == t]
        if active_only:
            lots = [l for l in lots if l.state in _OPEN_STATES]
        lots.sort(key=lambda l: l.created_at)
        return [l.to_dict() for l in lots]

    def aggregate(self) -> List[dict]:
        """按 symbol 聚合：总览是一个仓位，底层多个 lot 各自跟踪。"""
        groups: Dict[str, Dict] = {}
        for lot in self._lots.values():
            if lot.state not in _OPEN_STATES:
                continue
            key = lot.symbol.split(":")[-1].upper()
            g = groups.setdefault(key, {
                "symbol": key, "side": lot.side,
                "net_qty": 0, "avg_entry": 0.0, "pnl": 0.0,
                "lots": [],
            })
            # 多/空混合时 net_qty 相抵；avg_entry 用加权（简化按股数加权）
            signed = lot.filled_qty if lot.side == "long" else -lot.filled_qty
            g["net_qty"] += signed
            g["pnl"] = round(g["pnl"] + lot.pnl, 2)
            g["lots"].append(lot.to_dict())
        result = []
        for g in groups.values():
            # 加权成本：用各 lot 的成交价按股数加权（不分方向，近似）
            weight = 0.0
            total = 0.0
            for d in g["lots"]:
                if d["filled_qty"] > 0 and d["avg_fill"] > 0:
                    weight += d["avg_fill"] * d["filled_qty"]
                    total += d["filled_qty"]
            g["avg_entry"] = round(weight / total, 2) if total else 0.0
            g["lots"].sort(key=lambda x: x["created_at"])
            result.append(g)
        result.sort(key=lambda g: abs(g["net_qty"]), reverse=True)
        return result

    # ------------------------------------------------------------------
    # 逐单管理：改 TRAIL / 撤单
    # ------------------------------------------------------------------

    def get(self, lot_id: str) -> Optional[OrderLot]:
        return self._lots.get(lot_id)

    async def update_trail(self, lot_id: str,
                           new_stop: float) -> Tuple[bool, str]:
        """修改某个 lot 的 TRAIL 触发价（只向有利方向，1 美分防抖）。"""
        lot = self._lots.get(lot_id)
        if lot is None:
            return False, f"lot 不存在: {lot_id}"
        if lot.state not in _OPEN_STATES:
            return False, f"lot 已非活跃状态 ({lot.state})"
        if new_stop <= 0:
            return False, "触发价必须为正"
        cur = lot.stop_px
        new = max(cur, new_stop) if lot.side == "long" else min(cur, new_stop)
        if abs(new - cur) < 0.01:
            return False, f"触发价未变（{cur}）"
        ok = await self._gw.modify_stop(lot, round(new, 2))
        if ok:
            lot.stop_px = round(new, 2)
            lot.updated_at = time.time()
            self._save()
        return ok, ("" if ok else "修改失败")

    async def cancel_lot(self, lot_id: str) -> Tuple[bool, str]:
        """撤掉一个 lot：PENDING 撤入场+STOP；ACTIVE 实盘拒绝（持仓需平仓）、dry-run 虚拟单可撤。"""
        lot = self._lots.get(lot_id)
        if lot is None:
            return False, f"lot 不存在: {lot_id}"
        if lot.state not in (PENDING, ACTIVE):
            return False, f"lot 状态 {lot.state} 不可撤单"

        is_live = bool(self._gw.enabled and self._gw._connected)
        if lot.state == ACTIVE and is_live:
            return False, ("已成交持仓不能撤单（STOP 是保护单，撤掉会变裸仓）。"
                           "请使用平仓操作。")

        if not is_live:
            # dry-run：虚拟单无真实持仓，直接置 CANCELLED
            lot.state = CANCELLED
            lot.note = "已撤单（dry-run）"
            lot.updated_at = time.time()
            self._save()
            return True, ""

        cancelled = []
        for trade in (lot.trail_trade, lot.entry_trade):
            if trade is not None and not trade.isDone():
                cancelled.append(self._gw.cancel_order(trade))
        lot.state = CANCELLED if all(cancelled) else lot.state
        if lot.state == CANCELLED:
            lot.note = "已撤单"
        lot.updated_at = time.time()
        self._save()
        return lot.state == CANCELLED, ("已撤单" if lot.state == CANCELLED
                                        else "部分撤单失败")

    # ------------------------------------------------------------------
    # mark 价刷新（实盘从 IBKR snapshot；dry-run 用 avg_fill）
    # ------------------------------------------------------------------

    async def refresh_marks(self) -> None:
        """刷新所有活跃 lot 的 mark 价并重算 PnL。"""
        symbols = {l.symbol for l in self._lots.values()
                   if l.state == ACTIVE}
        prices: Dict[str, Optional[float]] = {}
        for sym in symbols:
            prices[sym] = await self._gw.last_price(sym)
        changed = False
        for lot in self._lots.values():
            if lot.state != ACTIVE:
                continue
            px = prices.get(lot.symbol)
            if px and px > 0:
                if abs(px - lot.last_px) >= 0.0001:
                    lot.last_px = px
                    changed = True
            elif lot.last_px <= 0:
                lot.last_px = lot.avg_fill
            lot.recalc_pnl()
            # 更新 MAE/MFE（最大浮亏/最大浮盈）
            if lot.filled_qty > 0 and lot.avg_fill > 0 and lot.last_px > 0:
                if lot.side == "long":
                    drift = (lot.last_px - lot.avg_fill) * lot.filled_qty
                else:
                    drift = (lot.avg_fill - lot.last_px) * lot.filled_qty
                lot.mfe = round(max(lot.mfe, drift), 2)
                lot.mae = round(min(lot.mae, drift), 2)
            lot.updated_at = time.time()
        if changed:
            self._save()

    # ------------------------------------------------------------------
    # 持久化
    # ------------------------------------------------------------------

    def _archive(self, lot: "OrderLot") -> None:
        """平仓后追加到当日历史归档（append，永不覆盖）。"""
        try:
            day = time.strftime("%Y%m%d")
            path = self._state_path.parent / f"trades-{day}.json"
            records = []
            if path.exists():
                try:
                    records = json.loads(path.read_text(encoding="utf-8"))
                except Exception:
                    records = []
            records.append(lot.to_dict())
            path.write_text(json.dumps(records, ensure_ascii=False, indent=2),
                            encoding="utf-8")
        except Exception as e:
            logger.warning("归档平仓记录失败: %s", e)

    def _save(self) -> None:
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            payload = {"lots": [l.to_dict() for l in self._lots.values()],
                       "saved_at": time.time()}
            self._state_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8")
        except OSError:
            logger.exception("lot 状态落盘失败")

    def _load(self) -> None:
        if not self._state_path.exists():
            return
        try:
            payload = json.loads(self._state_path.read_text(encoding="utf-8"))
            for d in payload.get("lots", []):
                lot = OrderLot.from_dict(d)
                self._lots[lot.lot_id] = lot
            logger.info("已恢复 %d 个 lot 批次（%s）", len(self._lots),
                        self._state_path.name)
        except (OSError, ValueError, TypeError) as e:
            logger.warning("lot 状态加载失败: %s", e)
