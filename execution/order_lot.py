# -*- coding: utf-8 -*-
"""
分批建仓：订单批次管理器 (OrderLot / OrderManager)
==================================================

核心语义（2026-10-17 v3，配合 Web 下单控制台）：

- 一次「下单」= 一个 **OrderLot（批次）** = 一个 LMT 入场单 + 一个 STP
  固定止损子单（parentId 括号链，IBKRGateway.place_limit_with_stop）。
  止损单只有 STP 一种（2026-10-17 起，TRAIL 路径已删除）。
- 同一只股票可以连续下多个 lot，每个 lot 拥有**独立的**止损触发价、
  订单号与盈亏（PnL）。
- 对外「聚合」成仓位视图：同一 symbol 的多个 lot 合并显示净股数与合计 PnL，
  但底层每个 lot 独立管理、独立改止损、独立撤单。

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

import config

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
STATE_PATH = PROJECT_ROOT / "logs" / "lots_state.json"

# lot 状态机
PENDING = "PENDING"     # 入场单已提交、未成交
ACTIVE = "ACTIVE"       # 已成交、STOP 在 broker 端保护
CLOSING = "CLOSING"     # 平仓单已提交，等待全部实际成交
CANCELLED = "CANCELLED" # 已撤单
FAILED = "FAILED"       # 下单失败
CLOSED = "CLOSED"       # 已平仓

_OPEN_STATES = {PENDING, ACTIVE, CLOSING}
_CANCEL_WAIT_SECONDS = 8.0
_WATCH_POLL_SECONDS = 1.0


@dataclass
class OrderLot:
    """一个独立的下单批次：LMT 入场 + STP 固定止损子单（括号单）。"""

    lot_id: str
    symbol: str
    side: str                       # 'long' | 'short'
    qty: int                        # 目标股数
    entry_px: float                 # LMT 限价（入场参考价）
    stop_px: float                  # STP 止损触发价
    trail_amount: float             # 遗留字段（历史 TRAIL lot），新单恒为 0
    init_entry_px: float = 0.0      # 下单时计划限价（R 风险基准，改价/成交后不变化）
    init_stop_px: float = 0.0       # 下单时计划止损（R 风险基准，跟踪后不变化）
    init_risk_usd: float = 0.0      # 下单时计划风险金额（qty × 每股初始风险）
    max_position_pct: float = 0.0   # 下单时仓位上限%（0=不封顶；回踩改价股数封顶用）
    atr_mult: float = 1.0           # 下单时全局止损 ATR 倍数（突破成交补挂止损锚定价 ∓ atr_mult×ATR；默认 1.0 兼容旧状态文件）
    state: str = PENDING
    entry_order_id: Optional[int] = None
    trail_order_id: Optional[int] = None
    filled_qty: int = 0             # ACTIVE/CLOSING 时为实际余仓
    avg_fill: float = 0.0           # 入场成交均价
    last_px: float = 0.0            # mark 价（PnL 计算用；dry-run = avg_fill）
    pnl: float = 0.0                # 未实现盈亏（美元）
    reason: str = ""                # 下单理由/备注
    note: str = ""
    trader: str = ""                # 下单交易员（master=本机，其余为 LAN 客户端名）
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    # ---- 复盘/绩效字段（平仓闭环）----
    limit_type: str = ""            # ema / channel / midpoint / retest / auto_* / adopted
    reprice_bar_time: float = 0.0   # midpoint/retest 最近一次改价尝试的确认 bar 时间
    breakout_escalated: bool = False  # 中点单突破上根K线后已升级市价（一次性，不可逆）
    # ---- 中点 OCA 双腿（LMT 中点 + STP 突破，一成全撤）----
    oca_group: str = ""             # OCA 组名（空 = 单腿中点单）
    breakout_order_id: Optional[int] = None  # STP 突破入场腿
    breakout_px: float = 0.0        # 当前突破触发价
    breakout_filled: bool = False   # 实际由突破腿成交（止损已改为独立补挂）
    entry_time: float = 0.0         # 实际成交时间戳（无成交则 0）
    exit_px: float = 0.0            # 平仓实际成交均价
    closed_at: float = 0.0          # 平仓时间戳
    realized_pnl: float = 0.0       # 实现盈亏（美元，按实际平仓成交计算）
    exit_reason: str = ""           # 平仓原因：manual / stop / eod
    signal_snapshot: dict = field(default_factory=dict)  # 下单时指标快照

    # ---- 平仓/保护单生命周期（全部持久化，Trade 句柄除外）----
    close_order_id: Optional[int] = None
    close_permId: Optional[int] = None
    close_requested_qty: int = 0    # 当前平仓单委托数量
    close_order_filled_qty: int = 0 # 当前平仓单已入账成交数量
    close_order_avg_fill: float = 0.0
    close_filled_qty: int = 0       # 本 lot 跨平仓/止损单累计实际平仓数量
    close_avg_fill: float = 0.0     # 上述累计成交的加权均价
    close_reason: str = ""
    trail_order_filled_qty: int = 0 # 当前保护单已入账成交数量
    trail_order_avg_fill: float = 0.0
    protection_restore_needed: bool = False
    trail_modify_blocked_until: float = 0.0  # IBKR 320/断连后的修改冷却时间
    protection_find_blocked_until: float = 0.0  # 保护单快照认回失败冷却（防每秒打全量订单快照）
    owned_order_ids: List[int] = field(default_factory=list)  # 含替换单/旧平仓单/permId

    # ---- 风险回报复盘指标 ----
    take_profit_px: float = 0.0     # 目标止盈价
    target_rr: float = 0.0          # 目标盈亏比（设计 RR = 止盈距离/止损距离）
    actual_rr: float = 0.0          # 真实盈亏比（实现盈亏 / 计划风险金额）
    mae: float = 0.0                # 最大浮亏 MAE（最不利偏移，≤0，美元）
    mfe: float = 0.0                # 最大浮盈 MFE（最有利偏移，≥0，美元）

    # ---- 时间维度复盘字段 ----
    breakeven_reached: bool = False  # 是否触发过保本（止损移到成本）
    breakeven_time: float = 0.0      # 保本触发时间戳
    breakeven_seconds: float = 0.0   # 开仓→保本 的秒数
    stop_time: float = 0.0           # 止损触发时间戳
    stop_seconds: float = 0.0        # 开仓→止损 的秒数
    exit_seconds: float = 0.0        # 开仓→平仓 的总秒数

    # ib_async Trade 句柄，仅内存持有，不序列化
    entry_trade: object = field(default=None, repr=False, compare=False)
    trail_trade: object = field(default=None, repr=False, compare=False)
    close_trade: object = field(default=None, repr=False, compare=False)
    breakout_trade: object = field(default=None, repr=False, compare=False)

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
        d.pop("close_trade", None)
        d.pop("breakout_trade", None)
        return d

    @staticmethod
    def from_dict(d: dict) -> "OrderLot":
        d = {k: v for k, v in d.items()
             if k not in ("entry_trade", "trail_trade", "close_trade",
                          "breakout_trade")}
        return OrderLot(**d)


class OrderManager:
    """分批建仓的批次管理器：下单 / 聚合 / 改 STOP / 撤平 / 持久化。"""

    def __init__(self, gateway, risk_gate, state_path: Path = STATE_PATH) -> None:
        self._gw = gateway
        self._risk = risk_gate
        self._state_path = Path(state_path)
        self._lots: Dict[str, OrderLot] = {}
        self._pyramid_groups: dict = {}
        self._locks: Dict[str, asyncio.Lock] = {}       # 每 lot 串行化撤改平与 watcher
        self._recent_places: Dict[tuple, float] = {}   # 下单幂等：近期相同单时间戳
        self._watchers: Dict[tuple, asyncio.Task] = {}  # (lot_id, kind) -> watcher 任务
        self._revive_done: set = set()      # 已复活过的 lot（每 lot 只复活一次）
        self.on_lot_closed = None      # 平仓回调 fn(lot_dict)，由上层（console）注入
        self.on_lot_revived = None     # 对账复活回调 fn(lot_dict)，由上层（console）注入
        # 价格提供者（console 注入 TV 现价；None 时回退 gateway.last_price）
        self.price_provider = None
        # 行情健康提供者（console 注入；回测/离线测试可为 None）
        self.market_health = None
        self._load()

    async def _ref_px(self, symbol: str) -> Optional[float]:
        """平仓参考价：优先 price_provider（TV），回退 IBKR snapshot。
        全系统以 TV 为唯一价格源，不依赖 TWS 行情权限。"""
        if self.price_provider is not None:
            try:
                px = self.price_provider(symbol)
                if px and px > 0:
                    return px
            except Exception:
                pass
        return await self._gw.last_price(symbol)

    def _lot_lock(self, lot: OrderLot) -> asyncio.Lock:
        lock = self._locks.get(lot.lot_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[lot.lot_id] = lock
        return lock

    @staticmethod
    def _trade_order_id(trade) -> Optional[int]:
        try:
            return int(trade.order.orderId)
        except (AttributeError, TypeError, ValueError):
            return None

    @staticmethod
    def _trade_perm_id(trade) -> Optional[int]:
        try:
            value = int(trade.order.permId)
            return value if value > 0 else None
        except (AttributeError, TypeError, ValueError):
            return None

    def _remember_order(self, lot: OrderLot, *order_ids) -> None:
        for order_id in order_ids:
            try:
                value = int(order_id)
            except (TypeError, ValueError):
                continue
            if value > 0 and value not in lot.owned_order_ids:
                lot.owned_order_ids.append(value)

    def legacy_order_ids(self, lot: OrderLot) -> List[int]:
        """返回本 lot 拥有过的订单号/permId，供成交回报做所有权白名单。"""
        ids = list(lot.owned_order_ids)
        ids.extend((lot.entry_order_id, lot.trail_order_id,
                    lot.close_order_id, lot.close_permId))
        for trade in (lot.entry_trade, lot.trail_trade, lot.close_trade):
            ids.extend((self._trade_order_id(trade), self._trade_perm_id(trade)))
        out: List[int] = []
        for order_id in ids:
            try:
                value = int(order_id)
            except (TypeError, ValueError):
                continue
            if value > 0 and value not in out:
                out.append(value)
        return out

    async def _wait_trade_done(self, trade, timeout: float = _CANCEL_WAIT_SECONDS) -> bool:
        if trade is None:
            return False
        deadline = time.monotonic() + timeout
        while not trade.isDone():
            if time.monotonic() >= deadline:
                return False
            await asyncio.sleep(0.05)
        return True

    async def _cancel_trade_and_wait(self, trade) -> bool:
        """只有券商确认终态才返回 True；缺句柄/撤单异常/超时均为失败。"""
        if trade is None:
            return False
        if trade.isDone():
            return True
        if not self._gw.cancel_order(trade):
            return False
        return await self._wait_trade_done(trade)

    # ------------------------------------------------------------------
    # 下单（分批建仓：每次调用 = 一个独立 lot，LMT 父 + STP 子）
    # ------------------------------------------------------------------

    async def place_limit_stop_lot(self, symbol: str, side: str, qty: int,
                                   limit_px: float, stop_px: float,
                                   limit_type: str = "",
                                   reason: str = "",
                                   risk_usd: float = 0,
                                   signal_snapshot: Optional[dict] = None,
                                   reprice_bar_time: float = 0.0,
                                   breakout_px: float = 0.0,
                                   max_position_pct: float = 0.0,
                                   atr_mult: float = 1.0,
                                   trader: str = "",
                                   risk_ref_px: float = 0.0
                                   ) -> Tuple[Optional[OrderLot], str]:
        """限价单（父）+ 固定 STOP 止损（子，parentId 关联）下单一个批次。

        breakout_px > 0 时（中点单）改用 OCA 双腿：LMT 中点 + STP 突破
        入场一成全撤，止损子单挂 LMT 腿。"""
        symbol = symbol.strip().upper()
        side = side.strip().lower()
        if side not in ("long", "short"):
            return None, f"方向非法: {side}（应为 long / short）"
        if self.market_health is not None:
            try:
                health = self.market_health()
            except Exception as e:
                logger.error("行情健康检查失败，拒绝开仓 %s %s: %s", symbol, side, e)
                return None, "行情健康检查失败，禁止新开仓；人工平仓/撤单不受影响"
            if not health.get("trade_allowed", False):
                reason = health.get("reason") or "unknown"
                logger.warning("🚫 行情陈旧，拒绝开仓 %s %s: %s", symbol, side, reason)
                return None, f"行情数据陈旧（{reason}），禁止新开仓；请等待数据恢复"
        # 延迟行情标的黑名单：无 IBKR 实时行情订阅（10089），拒绝交易
        ticker = symbol.split(":")[-1].upper()
        if ticker in config.TRADE_BLOCKLIST:
            logger.warning("🚫 拒绝交易延迟行情标的 %s（无实时行情订阅）", ticker)
            return None, f"{ticker} 为延迟行情标的（无实时行情订阅），拒绝交易"
        if qty <= 0:
            return None, "qty 必须为正整数"
        if limit_px <= 0 or stop_px <= 0:
            return None, "限价/止损价必须为正"
        if breakout_px > 0:
            if side == "long" and breakout_px <= limit_px:
                return None, "多仓突破触发价必须高于中点限价"
            if side == "short" and breakout_px >= limit_px:
                return None, "空仓突破触发价必须低于中点限价"

        # 幂等：2 秒内相同 symbol+side+限价+止损 视为重复提交（防双击/重试）
        key = (symbol, side, round(limit_px, 2), round(stop_px, 2))
        ownership = signal_snapshot or {}
        if ownership.get("strategy_id"):
            key += (ownership["strategy_id"], ownership.get("strategy_instance_id", "default"),
                    ownership.get("group_id"), ownership.get("entry_index"))
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
            open_positions=open_positions, daily_pnl=realized, risk_usd=risk_usd,
            open_risk_usd=self.risk_exposure()["total"],
            risk_ref_px=risk_ref_px)
        if not ok:
            logger.warning("🚫 风控拦截 %s %s: %s", symbol, side, why)
            return None, why

        # R 风险基准锚定预期成交价（market/chase 单 = 现价；其余 = 限价），
        # 溢价只做券商成交保护不进风险/RR 口径
        anchor_entry = float(risk_ref_px) if risk_ref_px > 0 else float(limit_px)
        lot = OrderLot(
            lot_id=f"lot-{uuid.uuid4().hex[:8]}",
            symbol=symbol, side=side, qty=int(qty),
            entry_px=float(limit_px), stop_px=float(stop_px),
            init_entry_px=anchor_entry, init_stop_px=float(stop_px),
            init_risk_usd=round(abs(anchor_entry - float(stop_px)) * int(qty), 2),
            max_position_pct=float(max_position_pct or 0.0),
            atr_mult=float(atr_mult or 1.0),
            trail_amount=0.0,
            reason=reason or f"{limit_type} 限价单",
            note=f"limit+stop ({limit_type})",
            limit_type=limit_type or "",
            reprice_bar_time=float(reprice_bar_time or 0.0),
            signal_snapshot=signal_snapshot or {},
            trader=trader or "",
        )
        if breakout_px > 0:
            lot.oca_group = f"OCA-{lot.lot_id}"
            lot.breakout_px = round(float(breakout_px), 2)
            result = await self._gw.place_midpoint_oca(
                symbol, side, qty, limit_px, breakout_px, stop_px,
                lot.oca_group)
            lot.breakout_order_id = result.get("breakout_order_id")
            lot.breakout_trade = result.get("breakout_trade")
        else:
            if (config.MARKET_ORDER_STRICT
                    and limit_type in ("market", "chase")):
                # 严格市价成交（只做实时行情标的）：MKT 父 + STP 子括号单
                result = await self._gw.place_market_with_stop(
                    symbol, side, qty, stop_px)
                # MKT 无挂单价：entry 记录预期成交价（现价），成交后 avg_fill 为准
                if risk_ref_px > 0:
                    lot.entry_px = float(risk_ref_px)
            else:
                result = await self._gw.place_limit_with_stop(
                    symbol, side, qty, limit_px, stop_px)
        lot.entry_order_id = result.get("entry_order_id")
        lot.trail_order_id = result.get("stop_order_id")  # 复用字段存 STOP 单 id
        lot.entry_trade = result.get("trade")
        lot.trail_trade = result.get("stop_trade")
        self._remember_order(
            lot, lot.entry_order_id, lot.trail_order_id,
            lot.breakout_order_id,
            self._trade_order_id(lot.entry_trade),
            self._trade_perm_id(lot.entry_trade),
            self._trade_order_id(lot.trail_trade),
            self._trade_perm_id(lot.trail_trade),
            self._trade_order_id(lot.breakout_trade),
            self._trade_perm_id(lot.breakout_trade))
        if not result.get("ok"):
            # 有 orderId 就说明订单可能已到券商；即使返回失败，也必须等对账
            # 排除实际成交，不能直接制造 FAILED 孤儿。
            lot.state = PENDING if lot.entry_order_id is not None else FAILED
            lot.note = f"限价+STOP 单失败: {result.get('error')}"
            if lot.state == PENDING:
                lot.note += "；订单号已分配，保留 PENDING 等对账确认实际成交"
            self._lots[lot.lot_id] = lot
            self._save()
            if lot.state == PENDING and lot.entry_trade is not None:
                self._start_watch(lot, "entry", self._watch_entry(lot))
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

        self._lots[lot.lot_id] = lot
        self._save()
        if not result.get("dry_run"):
            self._start_watch(lot, "entry", self._watch_entry(lot))
        return lot, ""

    async def close_lot(self, lot_id: str,
                        ref_px: Optional[float] = None,
                        exit_reason: str = "manual") -> Tuple[bool, str]:
        """提交平仓并进入 CLOSING；只有 watcher 确认实际成交后才 CLOSED。"""
        lot = self._lots.get(lot_id)
        if lot is None:
            return False, f"lot 不存在: {lot_id}"
        async with self._lot_lock(lot):
            if lot.state != ACTIVE:
                return False, f"lot 状态 {lot.state} 不可平仓（仅 ACTIVE 可平）"
            if lot.filled_qty <= 0:
                return False, "lot 没有可平仓的实际余仓"

            is_live = bool(self._gw.enabled and self._gw._connected)
            px = ref_px or lot.last_px or lot.avg_fill
            if not is_live:
                # dry-run 没有券商异步成交，允许立即按虚拟成交价闭环。
                if px <= 0:
                    return False, "无法确定平仓参考价"
                closed_before = lot.close_filled_qty
                amount = lot.close_avg_fill * closed_before + px * lot.filled_qty
                lot.close_filled_qty = closed_before + lot.filled_qty
                lot.close_avg_fill = amount / lot.close_filled_qty
                lot.close_reason = exit_reason
                lot.filled_qty = 0
                self._finalize_close(lot, lot.close_avg_fill, exit_reason)
                lot.note = f"已平仓（dry-run/{exit_reason}）"
                self._save()
                self._emit_closed(lot)
                return True, ""

            # 尚未入账的保护单成交须先处理，否则可能过量平仓；已入账部分
            # 已从 filled_qty 扣减，剩余数量可以安全重试平仓。
            trail = lot.trail_trade
            if trail is not None and trail.isDone() \
                    and int(trail.orderStatus.filled or 0) > lot.trail_order_filled_qty:
                self._start_exit_watch(lot)
                return False, "保护单已发生成交，等待其平仓回报处理"

            if px <= 0:
                px = await self._ref_px(lot.symbol) or lot.avg_fill
            if px <= 0:
                return False, "无法确定平仓参考价"
            if trail is None and lot.trail_order_id is not None:
                return False, "缺保护单句柄，无法确认撤单终态；请先对账恢复句柄"

            # 先持久化 CLOSING，再撤保护单；崩溃后也不会被当作普通 ACTIVE 重发。
            # 旧 ID 只留在所有权白名单，当前字段必须清空，避免重启把旧终态当新单。
            self._remember_order(lot, lot.close_order_id, lot.close_permId,
                                 self._trade_perm_id(lot.close_trade))
            lot.close_order_id = None
            lot.close_permId = None
            lot.close_trade = None
            lot.state = CLOSING
            lot.close_reason = exit_reason or "manual"
            lot.close_requested_qty = 0
            lot.close_order_filled_qty = 0
            lot.close_order_avg_fill = 0.0
            lot.updated_at = time.time()
            self._save()

            if trail is not None and not trail.isDone():
                if not await self._cancel_trade_and_wait(trail):
                    lot.state = ACTIVE
                    lot.note = "平仓未提交：保护单撤单未获券商终态确认"
                    lot.updated_at = time.time()
                    self._save()
                    return False, lot.note
            if trail is not None and int(trail.orderStatus.filled or 0) \
                    > lot.trail_order_filled_qty:
                # 撤单等待期间 STOP 可能成交；交回退出 watcher，禁止再发反向单。
                lot.state = ACTIVE
                lot.note = "保护单在平仓前发生成交，未提交新的平仓单"
                self._save()
                self._start_exit_watch(lot)
                return False, lot.note

            lot.close_requested_qty = lot.filled_qty
            lot.close_order_filled_qty = 0
            lot.close_order_avg_fill = 0.0
            result = await self._gw.place_close(
                lot.symbol, lot.side, lot.close_requested_qty, px)
            lot.close_order_id = result.get("order_id")
            lot.close_trade = result.get("trade")
            lot.close_permId = (result.get("permId")
                                or self._trade_perm_id(lot.close_trade))
            self._remember_order(
                lot, lot.close_order_id, lot.close_permId,
                self._trade_order_id(lot.close_trade),
                self._trade_perm_id(lot.close_trade))

            if not result.get("ok"):
                if lot.close_trade is not None:
                    self._record_exit_fill(lot, "close")
                if lot.filled_qty > 0:
                    lot.state = ACTIVE
                    protected = await self._restore_protection(lot)
                    lot.note = f"平仓失败: {result.get('error')}"
                    if not protected:
                        lot.note += "；实际余仓保护单恢复失败，需立即核查"
                    lot.updated_at = time.time()
                    self._save()
                    if protected:
                        self._start_exit_watch(lot)
                    return False, lot.note
                # 即使网关报错，只要已有成交就由 watcher 按实际数量闭环，绝不猜价。
                lot.note = "平仓返回失败但已有成交，等待实际成交回报核对"
                self._save()
                self._start_close_watch(lot)
                return False, lot.note

            lot.note = (f"平仓单已提交（{lot.close_reason}），"
                        f"orderId={lot.close_order_id}，等待实际成交")
            lot.updated_at = time.time()
            self._save()
            self._start_close_watch(lot)
            return True, ""

    def _apply_partial_fill(self, lot: "OrderLot", close_qty: int,
                            px: float, exit_reason: str) -> None:
        """部分平仓记账：累计已平数量 + 加权均价 + 减余仓 + 部分实现盈亏。lot 保持 ACTIVE。"""
        before = lot.close_filled_qty
        total = before + close_qty
        if before > 0 and lot.close_avg_fill > 0:
            lot.close_avg_fill = round(
                (lot.close_avg_fill * before + px * close_qty) / total, 4)
        else:
            lot.close_avg_fill = round(px, 4)
        lot.close_filled_qty = total
        lot.filled_qty -= close_qty
        lot.qty = lot.filled_qty
        lot.exit_px = round(px, 4)
        lot.close_reason = exit_reason
        self._recompute_realized(lot)

    async def close_half(self, lot_id: str,
                         ref_px: Optional[float] = None,
                         exit_reason: str = "partial") -> Tuple[bool, str]:
        """平仓一半：先收缩 STOP 保护单到剩余数量，再 marketable 平一半；lot 保持 ACTIVE。

        关键顺序（防裸仓）：先减保护单数量 → 再平一半。平掉后 STOP 只保护剩余余仓。
        """
        lot = self._lots.get(lot_id)
        if lot is None:
            return False, f"lot 不存在: {lot_id}"
        async with self._lot_lock(lot):
            if lot.state != ACTIVE:
                return False, f"lot 状态 {lot.state} 不可平仓一半（仅 ACTIVE）"
            if lot.filled_qty <= 1:
                return False, "余仓 ≤1 股，无法平仓一半"
            close_qty = lot.filled_qty // 2        # 平掉一半（向下取整）
            remain_qty = lot.filled_qty - close_qty

            is_live = bool(self._gw.enabled and self._gw._connected)
            px = ref_px or lot.last_px or lot.avg_fill

            if not is_live:
                if px <= 0:
                    return False, "无法确定平仓参考价"
                self._apply_partial_fill(lot, close_qty, px, exit_reason)
                lot.note = f"平仓一半（dry-run）：平 {close_qty}，余 {remain_qty}"
                lot.updated_at = time.time()
                self._save()
                self._emit_closed(lot)
                return True, ""

            # 1. 先收缩 STOP 保护单数量到剩余（防裸仓）
            trail = lot.trail_trade
            if trail is None and lot.trail_order_id is not None:
                # 缺句柄则券商侧 STOP 数量未知，平一半后会留下超额 STOP
                # （触发即变反向仓）；与 close_lot 同款守卫
                return False, "缺保护单句柄，无法安全收缩保护数量；请先对账恢复句柄"
            if trail is not None and not trail.isDone():
                if not await self._gw.modify_stop_qty(trail, remain_qty):
                    return False, "收缩止损单数量失败，中止平仓一半"

            if px <= 0:
                px = await self._ref_px(lot.symbol) or lot.avg_fill
            if px <= 0:
                if trail is not None and not trail.isDone():
                    await self._gw.modify_stop_qty(trail, lot.filled_qty)
                return False, "无法确定平仓参考价"

            # 2. 平一半（marketable LIMIT，place_close 内部 2.5s 核对）
            result = await self._gw.place_close(lot.symbol, lot.side, close_qty, px)
            lot.close_order_id = result.get("order_id")
            lot.close_trade = result.get("trade")
            lot.close_permId = (result.get("permId")
                                or self._trade_perm_id(lot.close_trade))
            self._remember_order(lot, lot.close_order_id, lot.close_permId,
                                 self._trade_order_id(lot.close_trade),
                                 self._trade_perm_id(lot.close_trade))
            if not result.get("ok"):
                if trail is not None and not trail.isDone():
                    await self._gw.modify_stop_qty(trail, lot.filled_qty)
                return False, result.get("error") or "平仓一半失败"

            # 3. 统一交 partial watcher 记账（按实际成交回报，避免重复记账）
            lot.note = f"平仓一半已提交：平 {close_qty}，余 {remain_qty}"
            lot.updated_at = time.time()
            self._save()
            self._start_partial_watch(lot, close_qty)
            return True, ""

    async def _watch_partial(self, lot: "OrderLot", close_qty: int) -> None:
        """监控平一半单成交：按实际成交记账（partial），lot 保持 ACTIVE。"""
        while True:
            async with self._lot_lock(lot):
                trade = lot.close_trade
                if trade is None:
                    return
                if trade.isDone():
                    filled = int(trade.orderStatus.filled or 0)
                    status = trade.orderStatus.status
                    if filled > 0:
                        avg = float(trade.orderStatus.avgFillPrice or 0.0)
                        if avg <= 0:
                            avg = lot.last_px or lot.avg_fill
                        actual = min(filled, close_qty)
                        self._apply_partial_fill(lot, actual, avg, "partial")
                        lot.note = (f"平仓一半成交：平 {actual}，余 {lot.filled_qty}"
                                    f"（{status}）")
                    else:
                        lot.note = f"平一半单终态 {status} 无成交，保护单已收缩，可重试"
                    lot.updated_at = time.time()
                    self._save()
                    return
            await asyncio.sleep(_WATCH_POLL_SECONDS)

    def _start_partial_watch(self, lot: "OrderLot", close_qty: int) -> None:
        self._start_watch(lot, f"partial-{close_qty}", self._watch_partial(lot, close_qty))

    def _set_targets(self, lot: "OrderLot") -> None:
        """按下单初始风险设置 2R 目标；改价/滑点/跟踪不改写基准。"""
        init_entry = lot.init_entry_px or lot.entry_px
        init_stop = lot.init_stop_px or lot.stop_px
        risk_dist = abs(init_entry - init_stop)
        if risk_dist <= 0:
            return
        if lot.side == "long":
            lot.take_profit_px = round(init_entry + 2 * risk_dist, 4)
        else:
            lot.take_profit_px = round(init_entry - 2 * risk_dist, 4)
        lot.target_rr = round(2.0, 2)

    def _recompute_realized(self, lot: OrderLot) -> None:
        if lot.close_avg_fill <= 0 or lot.close_filled_qty <= 0:
            return
        if lot.side == "long":
            lot.realized_pnl = round(
                (lot.close_avg_fill - lot.avg_fill) * lot.close_filled_qty, 2)
        else:
            lot.realized_pnl = round(
                (lot.avg_fill - lot.close_avg_fill) * lot.close_filled_qty, 2)
        lot.exit_px = round(lot.close_avg_fill, 4)

    def _record_exit_fill(self, lot: OrderLot, source: str) -> bool:
        """按订单累计成交快照入账增量；filled_qty 始终同步为实际余仓。"""
        trade = lot.close_trade if source == "close" else lot.trail_trade
        if trade is None:
            return False
        ownership_changed = False
        if source == "close":
            order_id = self._trade_order_id(trade)
            perm_id = self._trade_perm_id(trade)
            if order_id is not None and lot.close_order_id != order_id:
                lot.close_order_id = order_id
                ownership_changed = True
            if perm_id is not None and lot.close_permId != perm_id:
                lot.close_permId = perm_id
                ownership_changed = True
            if ownership_changed:
                self._remember_order(lot, order_id, perm_id)
        try:
            filled = int(trade.orderStatus.filled or 0)
            avg = float(trade.orderStatus.avgFillPrice or 0.0)
        except (AttributeError, TypeError, ValueError):
            return ownership_changed
        if source == "close":
            previous = lot.close_order_filled_qty
            previous_avg = lot.close_order_avg_fill
        else:
            previous = lot.trail_order_filled_qty
            previous_avg = lot.trail_order_avg_fill
        if filled < previous:
            logger.warning("lot %s %s 成交数量回退 %d -> %d，忽略异常快照",
                           lot.lot_id, source, previous, filled)
            return ownership_changed
        delta = filled - previous
        if delta == 0 and (avg <= 0 or abs(avg - previous_avg) < 0.000001):
            return ownership_changed

        old_closed = lot.close_filled_qty
        old_amount = lot.close_avg_fill * old_closed
        new_closed = old_closed + delta
        if avg > 0 and not (old_closed > 0 and lot.close_avg_fill <= 0):
            # orderStatus.avgFillPrice 是当前订单全部成交的均价；减去该订单
            # 上次已入账金额，避免部分成交轮询重复或错误加权。
            amount = old_amount + avg * filled - previous_avg * previous
            lot.close_avg_fill = (round(amount / new_closed, 4)
                                  if new_closed > 0 else 0.0)
        elif delta > 0:
            # 数量仍须立即扣减以防重复平仓；价格未知时禁止 CLOSED。
            lot.close_avg_fill = 0.0

        lot.close_filled_qty = new_closed
        lot.filled_qty = max(0, lot.filled_qty - delta)
        if source == "close":
            lot.close_order_filled_qty = filled
            lot.close_order_avg_fill = avg
        else:
            lot.trail_order_filled_qty = filled
            lot.trail_order_avg_fill = avg
        lot.updated_at = time.time()
        self._recompute_realized(lot)
        if delta > 0:
            logger.info("lot %s %s 实际平仓成交 +%d，累计 %d，余仓 %d @%s",
                        lot.lot_id, source, delta, lot.close_filled_qty,
                        lot.filled_qty, lot.close_avg_fill or "价格未知")
        return True

    async def _actual_exit_px(self, lot: OrderLot) -> float:
        if lot.close_avg_fill > 0:
            return lot.close_avg_fill
        if lot.close_filled_qty <= 0:
            return 0.0
        fill = await self._gw.closing_fill_avg(
            lot.symbol, lot.side, lot.entry_time,
            legacy_order_ids=self.legacy_order_ids(lot))
        # 网关会纳入本工具 88 号段成交；数量不一致说明可能混入其他 lot，
        # 此时宁可等待人工核查，也不能用该均价记盈亏。
        if fill and int(fill[1]) == lot.close_filled_qty and fill[0] > 0:
            lot.close_avg_fill = float(fill[0])
            self._recompute_realized(lot)
            return lot.close_avg_fill
        return 0.0

    async def _recover_missing_close(self, lot: OrderLot) -> str:
        """平仓句柄重启后不可见时，仅用数量完全匹配的自有成交回报恢复。"""
        if (lot.close_order_id is None and lot.close_permId is None) \
                or lot.close_requested_qty <= 0:
            return "waiting"
        original_qty = lot.close_filled_qty + lot.filled_qty
        if original_qty <= 0:
            return "waiting"
        fill = await self._gw.closing_fill_avg(
            lot.symbol, lot.side, lot.entry_time,
            legacy_order_ids=self.legacy_order_ids(lot))
        if not fill or fill[0] <= 0 or int(fill[1]) != original_qty:
            logger.warning("lot %s 平仓句柄缺失且成交回报无法唯一归因（%s），"
                           "保持 CLOSING，禁止重发", lot.lot_id, fill)
            return "waiting"
        lot.close_filled_qty = original_qty
        lot.filled_qty = 0
        lot.close_avg_fill = float(fill[0])
        lot.close_order_filled_qty = lot.close_requested_qty
        self._recompute_realized(lot)
        if self._finalize_close(lot, lot.close_avg_fill,
                                lot.close_reason or "manual"):
            lot.note = "重启后按本 lot 实际成交回报补登平仓"
            return "closed"
        return "waiting"

    async def _has_broker_position(self, lot: OrderLot) -> Tuple[bool, str]:
        """新建保护单前的幻影守卫：券商侧必须有足额同向持仓。
        （TWS 手动平仓后残留的 ACTIVE lot 若直接挂 STP，触发后会变成真实反向仓。）
        dry-run 跳过；持仓查询失败同样拒绝（不拿未知当存在）。"""
        if not self._gw.enabled:
            return True, ""
        pos = await self._gw.positions()
        ticker = lot.symbol.split(":")[-1]
        if pos is None:
            return False, f"券商持仓查询失败，禁止为 {ticker} 新建保护单"
        have = pos.get(ticker, 0.0)
        if lot.side == "long" and have < lot.filled_qty:
            return False, (f"券商侧 {ticker} 持仓 {have} < lot {lot.filled_qty}，"
                           f"疑似幻影 lot，禁止新建保护单，请人工核查/核销")
        if lot.side == "short" and have > -lot.filled_qty:
            return False, (f"券商侧 {ticker} 持仓 {have} > -{lot.filled_qty}，"
                           f"疑似幻影 lot，禁止新建保护单，请人工核查/核销")
        return True, ""

    async def _restore_protection(self, lot: OrderLot) -> bool:
        """优先认回原括号子单并调整数量；确认缺失后才恢复独立 STOP。"""
        if lot.filled_qty <= 0:
            lot.protection_restore_needed = False
            return True
        trail = lot.trail_trade
        if trail is None or trail.isDone():
            # 断线/重启可能造成句柄过期；先用最新快照认回括号子单，避免重复挂保护单
            if lot.protection_find_blocked_until > time.time():
                logger.warning("lot %s 保护单快照认回冷却中，暂缓恢复", lot.lot_id)
                return False
            found, snapshot_complete = await self._find_active_protection(lot)
            if found is not None:
                trail = lot.trail_trade
            elif not snapshot_complete:
                lot.protection_find_blocked_until = time.time() + 30
                self._save()
                logger.warning("lot %s 恢复保护单前订单快照不完整，禁止新建 STOP（冷却 30s）",
                               lot.lot_id)
                return False
        if trail is not None and not trail.isDone():
            try:
                current_qty = int(trail.order.totalQuantity or 0)
            except (AttributeError, TypeError, ValueError):
                current_qty = 0
            if current_qty == lot.filled_qty:
                lot.protection_restore_needed = False
                return True
            ok = await self._gw.modify_stop_qty(trail, lot.filled_qty)
            lot.protection_restore_needed = not ok
            return ok

        lot.protection_restore_needed = True
        self._save()
        ok_pos, pos_err = await self._has_broker_position(lot)
        if not ok_pos:
            logger.error("lot %s 恢复保护单被幻影守卫拦截: %s", lot.lot_id, pos_err)
            return False
        result = await self._gw.place_stop_only(
            lot.symbol, lot.side, lot.filled_qty, lot.stop_px)
        if not result.get("ok"):
            logger.error("lot %s %s 实际余仓 %d 恢复保护单失败: %s",
                         lot.lot_id, lot.symbol, lot.filled_qty,
                         result.get("error"))
            return False
        lot.trail_order_id = result.get("stop_order_id") or result.get("order_id")
        lot.trail_trade = result.get("trade")
        lot.trail_order_filled_qty = 0
        lot.trail_order_avg_fill = 0.0
        lot.protection_restore_needed = False
        self._remember_order(
            lot, lot.trail_order_id,
            self._trade_order_id(lot.trail_trade),
            self._trade_perm_id(lot.trail_trade))
        logger.info("lot %s 已按实际余仓 %d 恢复 STOP orderId=%s",
                    lot.lot_id, lot.filled_qty, lot.trail_order_id)
        return True

    def _finalize_close(self, lot: "OrderLot", exit_px: float,
                        exit_reason: str) -> bool:
        """实际平仓全部成交后落定；价格或数量未知时拒绝伪造闭环。"""
        if lot.state == CLOSED:
            return False
        closed_qty = lot.close_filled_qty or lot.filled_qty
        if closed_qty <= 0 or float(exit_px or 0.0) <= 0:
            logger.warning("lot %s 缺实际平仓数量/价格，拒绝落定 CLOSED", lot.lot_id)
            return False
        if lot.close_filled_qty <= 0:
            lot.close_filled_qty = closed_qty
            lot.close_avg_fill = float(exit_px)
        lot.state = CLOSED
        lot.exit_px = round(float(exit_px), 4)
        lot.close_avg_fill = lot.exit_px
        lot.closed_at = time.time()
        lot.exit_reason = exit_reason or "manual"
        # 时间维度复盘：持仓总时长；止损单触发时记录止损时长
        if lot.entry_time > 0:
            lot.exit_seconds = round(lot.closed_at - lot.entry_time, 1)
            if exit_reason == "stop":
                lot.stop_time = lot.closed_at
                lot.stop_seconds = lot.exit_seconds
        lot.protection_restore_needed = False
        if lot.avg_fill > 0:
            if lot.side == "long":
                lot.realized_pnl = round(
                    (lot.exit_px - lot.avg_fill) * closed_qty, 2)
            else:
                lot.realized_pnl = round(
                    (lot.avg_fill - lot.exit_px) * closed_qty, 2)
        init_entry = lot.init_entry_px or lot.entry_px
        init_stop = lot.init_stop_px or lot.stop_px
        risk_amt = lot.init_risk_usd or abs(init_entry - init_stop) * lot.qty
        if risk_amt > 0:
            lot.actual_rr = round(lot.realized_pnl / risk_amt, 3)
        # CLOSED 历史记录的 filled_qty 表示实际平仓总量；开放状态才表示余仓。
        lot.filled_qty = closed_qty
        lot.pnl = 0.0
        lot.updated_at = time.time()
        self._archive(lot)
        return True

    def _record_entry_fill(self, lot: OrderLot) -> bool:
        trade = lot.entry_trade
        if trade is None:
            return False
        try:
            filled = int(trade.orderStatus.filled or 0)
            avg = float(trade.orderStatus.avgFillPrice or 0.0)
        except (AttributeError, TypeError, ValueError):
            return False
        if filled < lot.filled_qty:
            logger.warning("lot %s 入场成交数量回退 %d -> %d，忽略异常快照",
                           lot.lot_id, lot.filled_qty, filled)
            return False
        changed = filled != lot.filled_qty
        if avg > 0 and abs(avg - lot.avg_fill) >= 0.000001:
            changed = True
        if not changed:
            return False
        lot.filled_qty = filled
        if avg > 0:
            lot.avg_fill = avg
            lot.last_px = avg
        if filled > 0 and lot.entry_time <= 0:
            lot.entry_time = time.time()
        lot.updated_at = time.time()
        return True

    async def _activate_filled_entry(self, lot: OrderLot, status: str) -> bool:
        """入场有实际成交即转 ACTIVE，并把保护数量缩到实际成交股数。"""
        lot.state = ACTIVE
        if lot.entry_time <= 0:
            lot.entry_time = time.time()
        lot.last_px = lot.avg_fill or lot.entry_px
        self._set_targets(lot)
        lot.recalc_pnl()
        lot.note = (f"入场成交 {lot.filled_qty}/{lot.qty} @ {lot.avg_fill}"
                    if status != "Filled" else
                    f"入场全部成交 {lot.filled_qty} @ {lot.avg_fill}")

        trail = lot.trail_trade
        if lot.breakout_filled:
            # OCA 双腿均成交（overfill）：多余仓位无止损保护，立即平仓
            brk = lot.breakout_trade
            extra = 0
            if brk is not None:
                try:
                    extra = int(brk.orderStatus.filled or 0)
                except (AttributeError, TypeError, ValueError):
                    extra = 0
            if brk is not None and not brk.isDone():
                await self._cancel_trade_and_wait(brk)
            if extra > 0:
                result = await self._gw.place_close(
                    lot.symbol, lot.side, extra,
                    lot.avg_fill or lot.last_px or lot.entry_px,
                    order_ref=f"oca-overfill-{lot.lot_id}")
                if result.get("ok"):
                    self._remember_order(
                        lot, result.get("order_id"),
                        self._trade_order_id(result.get("trade")),
                        self._trade_perm_id(result.get("trade")))
                    lot.note += f"；OCA 双腿均成交，多余 {extra} 股已自动平仓"
                    logger.critical("lot %s OCA overfill 多余 %d 股已自动平仓",
                                    lot.lot_id, extra)
                else:
                    lot.note += (f"；OCA 双腿均成交，多余 {extra} 股平仓失败"
                                 "（无止损保护），需立即人工处理")
                    logger.critical("lot %s OCA overfill 多余 %d 股平仓失败: %s",
                                    lot.lot_id, extra, result.get("error"))
            lot.breakout_trade = None
            trail = lot.trail_trade
            if trail is None or trail.isDone():
                # 突破腿成交：原止损子单随 LMT 腿被 OCA 撤销，补挂独立 STP。
                # 止损锚定实际成交价（而非突破触发价）：成交价 ∓ lot.atr_mult×ATR
                # （用下单时固化的倍数，挂单存续期内口径一致）。
                atr = 0.0
                try:
                    atr = float((lot.signal_snapshot.get("ts_c")
                                 or lot.signal_snapshot.get("ts")
                                 or {}).get("atr") or 0.0)
                except (TypeError, ValueError):
                    atr = 0.0
                fill = lot.avg_fill or lot.entry_px
                if atr > 0 and fill > 0:
                    new_stop = round(fill + (-1 if lot.side == "long" else 1)
                                     * atr * lot.atr_mult, 2)
                    if ((lot.side == "long" and new_stop < fill)
                            or (lot.side == "short" and new_stop > fill)):
                        lot.stop_px = new_stop
                result = await self._gw.place_stop_only(
                    lot.symbol, lot.side, lot.filled_qty, lot.stop_px)
                if result.get("ok"):
                    lot.trail_order_id = (result.get("stop_order_id")
                                          or result.get("order_id"))
                    lot.trail_trade = result.get("trade")
                    lot.trail_order_filled_qty = 0
                    lot.trail_order_avg_fill = 0.0
                    lot.protection_restore_needed = False
                    self._remember_order(
                        lot, lot.trail_order_id,
                        self._trade_order_id(lot.trail_trade),
                        self._trade_perm_id(lot.trail_trade))
                    lot.note += f"；突破成交后已补挂独立 STOP @{lot.stop_px}"
                    self._start_exit_watch(lot)
                    self._save()
                    return True
                lot.protection_restore_needed = True
                lot.note += "；突破成交后补挂独立止损失败，裸仓风险，需立即核查"
                logger.error("lot %s 突破成交后补挂独立 STOP 失败: %s",
                             lot.lot_id, result.get("error"))
                self._save()
                return False
        if not lot.breakout_filled:
            # OCA 撤单滞后时突破腿可能仍活跃；入场腿已成交，防御性撤掉，
            # 否则其后续成交将无止损保护且无 watcher 跟踪
            brk = lot.breakout_trade
            if brk is not None and not brk.isDone():
                await self._cancel_trade_and_wait(brk)
        if trail is not None and not trail.isDone():
            try:
                protective_qty = int(trail.order.totalQuantity or 0)
            except (AttributeError, TypeError, ValueError):
                protective_qty = 0
            ok = True
            if protective_qty != lot.filled_qty:
                ok = await self._gw.modify_stop_qty(trail, lot.filled_qty)
                if not ok:
                    lot.note += ("；STOP 数量未调整到实际成交，禁止盲目重试平仓，"
                                 "需立即核查")
                    logger.error("lot %s 入场部分成交后 STOP 数量修改失败: %s -> %s",
                                 lot.lot_id, protective_qty, lot.filled_qty)
            lot.protection_restore_needed = not ok
            self._start_exit_watch(lot)
            return ok
        if trail is not None and int(trail.orderStatus.filled or 0) > 0:
            self._start_exit_watch(lot)
            return True

        lot.note += "；保护单句柄缺失/已终态，实际持仓可能裸仓，需立即核查"
        logger.error("lot %s 入场已成交但无法确认有效 STOP 保护", lot.lot_id)
        return False

    async def _handle_entry_terminal(self, lot: OrderLot) -> bool:
        trade = lot.entry_trade
        if trade is None or not trade.isDone():
            return False
        self._record_entry_fill(lot)
        status = trade.orderStatus.status
        if lot.filled_qty > 0:
            # 包括 Submitted 部分成交后被 Cancelled：成交部分绝不是孤儿。
            return await self._activate_filled_entry(lot, status)

        # 本地无成交记录 ≠ 无成交：OCA 竞态下突破腿成交与 LMT 撤单可同帧，
        # 缓存 orderStatus 会漏报。终态判定前查成交真相（executions）；
        # 查询失败保持 PENDING 交给对账重试，绝不误判 CANCELLED。
        truth = await self._ground_truth_entry_fills(lot)
        if truth is None:
            lot.note = f"入场单终态 {status}，成交真相查询失败，保持 PENDING 待对账"
            logger.warning("lot %s %s", lot.lot_id, lot.note)
            return False
        if await self._activate_from_ground_truth(lot, truth, "入场终态"):
            return True

        trail = lot.trail_trade
        if trail is not None and not trail.isDone():
            if not await self._cancel_trade_and_wait(trail):
                lot.note = f"入场已终态 {status}，但 STOP 撤单未确认，保留待对账"
                logger.warning("lot %s %s", lot.lot_id, lot.note)
                return False
        if trail is None and lot.trail_order_id is not None:
            lot.note = f"入场已终态 {status}，缺 STOP 句柄，保留待对账"
            return False
        lot.state = CANCELLED if status in ("Cancelled", "ApiCancelled") else FAILED
        lot.note = f"入场单终态 {status}，无实际成交"
        lot.updated_at = time.time()
        return True

    async def _maybe_switch_to_breakout(self, lot: OrderLot) -> bool:
        """突破腿已成交而 LMT 腿未成交：把突破腿切换为入场腿（须持锁）。

        OCA 会自动撤销 LMT 腿（连带其子止损单），这里做防御性补撤，
        并标记 breakout_filled，供 _activate_filled_entry 补挂独立止损。
        """
        brk = lot.breakout_trade
        if brk is None or lot.breakout_filled:
            return False
        try:
            brk_filled = int(brk.orderStatus.filled or 0)
        except (AttributeError, TypeError, ValueError):
            return False
        if brk_filled <= 0:
            return False
        entry = lot.entry_trade
        entry_filled = 0
        if entry is not None:
            try:
                entry_filled = int(entry.orderStatus.filled or 0)
            except (AttributeError, TypeError, ValueError):
                entry_filled = 0
        if entry_filled > 0:
            # 双腿均成交（overfill）：不切换，保留 LMT 为入场腿，
            # 多余仓位由 _activate_filled_entry 自动平掉。
            lot.breakout_filled = True
            logger.critical("lot %s OCA 双腿均成交（overfill）: LMT %d / 突破 %d",
                            lot.lot_id, entry_filled, brk_filled)
            return False
        lot.breakout_filled = True
        lot.entry_trade = brk
        lot.entry_order_id = lot.breakout_order_id
        lot.breakout_trade = None
        lot.breakout_order_id = None
        lot.note = "突破腿成交（OCA 撤中点腿），转为入场腿"
        if entry is not None and not entry.isDone():
            await self._cancel_trade_and_wait(entry)
        trail = lot.trail_trade
        if trail is not None and not trail.isDone():
            await self._cancel_trade_and_wait(trail)
        lot.trail_trade = None
        lot.trail_order_id = None
        lot.updated_at = time.time()
        self._save()
        logger.warning("lot %s 突破腿成交 x%d @%.4f，LMT 腿及子止损已撤销",
                       lot.lot_id, brk_filled,
                       float(brk.orderStatus.avgFillPrice or 0.0))
        return True

    async def _ground_truth_entry_fills(self, lot: OrderLot):
        """OCA 双腿的成交真相（executions）：{'entry': (qty, avg), 'breakout': (qty, avg)}。

        突破腿成交与 LMT 腿被 OCA 撤单可在同一帧发生，缓存的 orderStatus
        会漏报（QCOM/SMCI/BABA/SNOW 四次实锤）；executions 是唯一可靠来源。
        None = 查询失败（调用方不得把 None 当作无成交）。
        """
        if not (self._gw.enabled and self._gw._connected):
            return None
        ids = [i for i in (lot.entry_order_id, lot.breakout_order_id) if i]
        if not ids:
            return {}
        fills = await self._gw.entry_fills_by_order(lot.symbol, ids)
        if fills is None:
            return None
        out = {}
        if lot.entry_order_id in fills:
            out["entry"] = fills[lot.entry_order_id]
        if lot.breakout_order_id in fills:
            out["breakout"] = fills[lot.breakout_order_id]
        return out

    async def _activate_from_ground_truth(self, lot: OrderLot, truth: dict,
                                          source: str,
                                          cap_qty: Optional[int] = None) -> bool:
        """按成交真相激活/复活 lot（OCA 竞态漏报修复）。truth 无成交返回 False。

        cap_qty：对账复活时传券商实际持仓——余仓少于成交数说明已被人工减仓，
        账本对齐余仓且不做 overfill 平仓（多余部分已被人工处理）。
        """
        entry_qty, entry_avg = truth.get("entry", (0, 0.0))
        brk_qty, brk_avg = truth.get("breakout", (0, 0.0))
        total = int(entry_qty + brk_qty)
        if total <= 0:
            return False
        if brk_qty > 0:
            lot.breakout_filled = True
            if entry_qty <= 0:
                # 纯突破腿成交：OCA 已撤 LMT 腿（连带子止损），突破腿转为入场腿
                if lot.breakout_trade is not None:
                    lot.entry_trade = lot.breakout_trade
                if lot.breakout_order_id is not None:
                    lot.entry_order_id = lot.breakout_order_id
                lot.breakout_trade = None
                lot.breakout_order_id = None
                lot.note = "突破腿成交（OCA 撤中点腿），按成交真相转为入场腿"
        if cap_qty is not None and total > cap_qty:
            total = cap_qty
        lot.filled_qty = total
        lot.avg_fill = round((entry_qty * entry_avg + brk_qty * brk_avg)
                             / (entry_qty + brk_qty), 4)
        lot.last_px = lot.avg_fill
        if lot.entry_time <= 0:
            lot.entry_time = time.time()
        lot.state = ACTIVE
        self._set_targets(lot)
        lot.recalc_pnl()
        # 双腿双成（overfill）：超计划部分无止损保护，立即平仓
        extra = total - lot.qty if cap_qty is None and total > lot.qty else 0
        if extra > 0:
            result = await self._gw.place_close(
                lot.symbol, lot.side, extra,
                lot.avg_fill or lot.entry_px,
                order_ref=f"oca-overfill-{lot.lot_id}")
            if result.get("ok"):
                self._remember_order(
                    lot, result.get("order_id"),
                    self._trade_order_id(result.get("trade")),
                    self._trade_perm_id(result.get("trade")))
                lot.filled_qty = lot.qty
                lot.note += f"；OCA 双腿均成交，多余 {extra} 股已自动平仓"
                logger.critical("lot %s OCA overfill 多余 %d 股已自动平仓",
                                lot.lot_id, extra)
            else:
                lot.note += (f"；OCA 双腿均成交，多余 {extra} 股平仓失败"
                             "（无止损保护），需立即人工处理")
                logger.critical("lot %s OCA overfill 多余 %d 股平仓失败: %s",
                                lot.lot_id, extra, result.get("error"))
        # 突破腿仍活跃（OCA 撤单滞后）时防御性撤掉：其后续成交会变成
        # 无止损、无 watcher 跟踪的隐形仓位
        brk = lot.breakout_trade
        if brk is not None and not brk.isDone():
            await self._cancel_trade_and_wait(brk)
        # 残留保护单防御性撤掉，统一由 _restore_protection 重建（含幻影守卫）
        trail = lot.trail_trade
        if trail is not None and not trail.isDone():
            await self._cancel_trade_and_wait(trail)
        lot.trail_trade = None
        lot.trail_order_id = None
        lot.protection_restore_needed = True
        protected = await self._restore_protection(lot)
        lot.note += (f"（{source}按成交真相激活 {lot.filled_qty} 股"
                     + ("，已恢复 STOP 保护" if protected
                        else "，STOP 恢复失败需立即核查") + "）")
        lot.updated_at = time.time()
        self._save()
        if protected:
            self._start_exit_watch(lot)
        logger.warning("lot %s %s %s按成交真相激活：%d 股 @%.4f，保护单%s",
                       lot.lot_id, lot.symbol, source, lot.filled_qty,
                       lot.avg_fill, "已恢复" if protected else "恢复失败")
        return True

    async def _watch_entry(self, lot: OrderLot) -> None:
        """持续记录入场成交；部分成交后撤剩余时，成交部分转 ACTIVE。"""
        while True:
            async with self._lot_lock(lot):
                if lot.state != PENDING:
                    return
                await self._maybe_switch_to_breakout(lot)
                trade = lot.entry_trade
                if trade is None:
                    return
                changed = self._record_entry_fill(lot)
                if trade.isDone():
                    await self._handle_entry_terminal(lot)
                    self._save()
                    return
                if changed:
                    self._save()
            await asyncio.sleep(_WATCH_POLL_SECONDS)

    # ------------------------------------------------------------------
    # 退出 watcher：保护单（TRAIL/STOP）在 broker 端触发 = 自动平仓，
    # 必须落完整平仓记录（exit_px / 实现盈亏 / 真实RR / 归档 / 事件）
    # ------------------------------------------------------------------

    def _start_watch(self, lot: "OrderLot", kind: str, coro) -> None:
        """挂 watcher 任务（幂等）。watcher 每轮重读 lot 上的订单句柄。"""
        if not (self._gw.enabled and self._gw._connected):
            coro.close()
            return
        key = (lot.lot_id, kind)
        task = self._watchers.get(key)
        if task is not None and not task.done():
            coro.close()
            return
        self._watchers[key] = asyncio.create_task(coro)

    def _start_exit_watch(self, lot: "OrderLot") -> None:
        self._start_watch(lot, "exit", self._watch_exit(lot))

    def _start_close_watch(self, lot: "OrderLot") -> None:
        self._start_watch(lot, "close", self._watch_close(lot))

    async def _handle_close_terminal(self, lot: OrderLot) -> str:
        trade = lot.close_trade
        if trade is None or not trade.isDone():
            return "pending"
        self._record_exit_fill(lot, "close")
        status = trade.orderStatus.status
        if lot.filled_qty > 0:
            if status == "Filled":
                # Filled 却仍有本地余仓说明账本不一致，禁止自动重试造成超卖。
                lot.note = (f"平仓单 Filled 但本地仍有 {lot.filled_qty} 股余仓，"
                            "数量归因不一致，保持 CLOSING 待人工核查")
                logger.error("lot %s %s", lot.lot_id, lot.note)
                return "waiting"
            lot.state = ACTIVE
            protected = await self._restore_protection(lot)
            lot.note = (f"平仓单终态 {status}，实际余仓 {lot.filled_qty}，"
                        + ("已恢复 STOP 保护，可安全重试" if protected else
                           "STOP 恢复失败，需立即核查"))
            lot.updated_at = time.time()
            return "restored" if protected else "unprotected"

        if lot.close_filled_qty <= 0:
            lot.note = f"平仓单终态 {status} 但无本 lot 实际成交，保持 CLOSING 核查"
            return "waiting"
        exit_px = await self._actual_exit_px(lot)
        if exit_px <= 0:
            lot.note = "平仓数量已成交但实际均价未知，保持 CLOSING 等待成交回报"
            logger.warning("lot %s %s", lot.lot_id, lot.note)
            return "waiting"
        if self._finalize_close(lot, exit_px, lot.close_reason or "manual"):
            lot.note = f"实际平仓全部成交 @ {lot.exit_px}（{lot.exit_reason}）"
            return "closed"
        return "waiting"

    async def _watch_close(self, lot: OrderLot) -> None:
        """Submitted/部分成交保持 CLOSING；失败按实际余仓恢复 ACTIVE+STOP。"""
        while True:
            outcome = "pending"
            async with self._lot_lock(lot):
                if lot.state != CLOSING:
                    return
                trade = lot.close_trade
                if trade is None:
                    lot.note = "CLOSING 缺平仓单句柄，等待对账恢复，禁止重复提交"
                    self._save()
                    return
                changed = self._record_exit_fill(lot, "close")
                if not trade.isDone():
                    if changed:
                        self._save()
                else:
                    outcome = await self._handle_close_terminal(lot)
                    self._save()
                    if outcome == "restored":
                        self._start_exit_watch(lot)
            if outcome == "closed":
                logger.info("✅ lot %s 实际平仓完成 x%d @%.2f，实现盈亏 %.2f",
                            lot.lot_id, lot.filled_qty, lot.exit_px,
                            lot.realized_pnl)
                self._emit_closed(lot)
                return
            if outcome != "pending":
                return
            await asyncio.sleep(_WATCH_POLL_SECONDS)

    async def _handle_trail_terminal(self, lot: OrderLot) -> str:
        trade = lot.trail_trade
        if trade is None or not trade.isDone():
            return "pending"
        self._record_exit_fill(lot, "trail")
        status = trade.orderStatus.status
        reason = "trail_stop" if lot.trail_amount > 0 else "stop"
        if lot.trail_order_filled_qty > 0:
            if lot.filled_qty > 0:
                protected = await self._restore_protection(lot)
                lot.note = (f"保护单 {status}，实际平仓后余仓 {lot.filled_qty}，"
                            + ("已补独立 STOP" if protected else
                               "补 STOP 失败，需立即核查"))
                return "restored" if protected else "unprotected"
            exit_px = await self._actual_exit_px(lot)
            if exit_px <= 0:
                lot.note = "保护单已全部成交但实际均价未知，等待成交回报"
                return "waiting"
            if self._finalize_close(lot, exit_px, reason):
                lot.note = f"保护单触发实际平仓 @ {lot.exit_px}"
                return "closed"
            return "waiting"

        lot.note = f"保护单终态 {status}（无成交），实际持仓可能裸仓，需立即核查"
        # 标记保护缺失：对账（reconcile）据此自动调用 _restore_protection
        # （含幻影守卫），否则首次发现保护单终态时裸仓只告警不恢复
        lot.protection_restore_needed = True
        logger.warning("lot %s %s", lot.lot_id, lot.note)
        return "unprotected"

    async def _watch_exit(self, lot: OrderLot) -> None:
        """按保护单实际成交入账；部分平仓不 CLOSED，并为余仓补 STOP。"""
        while True:
            outcome = "pending"
            async with self._lot_lock(lot):
                if lot.state != ACTIVE:
                    return
                trade = lot.trail_trade
                if trade is None:
                    return
                changed = self._record_exit_fill(lot, "trail")
                if not trade.isDone():
                    if changed:
                        self._save()
                else:
                    outcome = await self._handle_trail_terminal(lot)
                    self._save()
            if outcome == "closed":
                logger.info("🛑 lot %s 保护单实际平仓 x%d @%.2f，盈亏 %.2f",
                            lot.lot_id, lot.filled_qty, lot.exit_px,
                            lot.realized_pnl)
                self._emit_closed(lot)
                return
            if outcome == "restored":
                continue
            if outcome != "pending":
                return
            await asyncio.sleep(_WATCH_POLL_SECONDS)

    @staticmethod
    def _protective_price(trade) -> Optional[float]:
        order = trade.order
        value = order.auxPrice if order.orderType == "STP" \
            else getattr(order, "trailStopPrice", None)
        try:
            value = float(value)
        except (TypeError, ValueError):
            return None
        return value if value > 0 else None

    def _protective_candidates(self, lot: OrderLot, trades: dict) -> list:
        ids = set(self.legacy_order_ids(lot))
        ticker = lot.symbol.split(":")[-1].upper()
        want_action = "SELL" if lot.side == "long" else "BUY"
        candidates = []
        for trade in trades.values():
            oid = self._trade_order_id(trade)
            perm_id = self._trade_perm_id(trade)
            if oid not in ids and perm_id not in ids:
                continue
            order = trade.order
            if order.orderType not in ("STP", "TRAIL") or trade.isDone():
                continue
            if trade.contract.symbol.upper() != ticker or order.action != want_action:
                continue
            price = self._protective_price(trade)
            if price is not None:
                candidates.append((price, trade))
        return candidates

    @staticmethod
    def _protection_rank(lot: OrderLot, price: float, trade):
        oid = OrderManager._trade_order_id(trade)
        is_child = lot.entry_order_id is not None \
            and getattr(trade.order, "parentId", None) == lot.entry_order_id
        is_current = oid == lot.trail_order_id
        price_rank = -price if lot.side == "long" else price
        return (not is_child, not is_current, price_rank)

    async def _find_active_protection(self, lot: OrderLot):
        """用最新订单快照认回本 lot 的保护单；快照不完整时返回 unknown。"""
        trades = await self._gw.trade_map()
        if trades is None:
            return None, False
        candidates = self._protective_candidates(lot, trades)
        if not candidates:
            return None, True
        candidates.sort(key=lambda x: self._protection_rank(lot, *x))
        price, trade = candidates[0]
        lot.trail_trade = trade
        lot.trail_order_id = self._trade_order_id(trade)
        lot.stop_px = price
        lot.protection_restore_needed = False
        if "裸仓" in lot.note or "保护单终态" in lot.note:
            lot.note = f"保护单已认回（orderId={lot.trail_order_id}）"
        self._remember_order(lot, lot.trail_order_id, self._trade_perm_id(trade))
        lot.updated_at = time.time()
        return trade, True

    async def _dedupe_protective_orders(self, lot: OrderLot, trades: dict) -> None:
        """同一 lot 若断线后出现多张保护单，优先保留括号子订单并撤掉其余。"""
        candidates = self._protective_candidates(lot, trades)
        if not candidates:
            return
        candidates.sort(key=lambda x: self._protection_rank(lot, *x))
        keep_price, keep_trade = candidates[0]
        keep_id = self._trade_order_id(keep_trade)
        lot.trail_trade = keep_trade
        lot.trail_order_id = keep_id
        lot.stop_px = keep_price
        lot.protection_restore_needed = False
        if "裸仓" in lot.note or "保护单终态" in lot.note:
            lot.note = f"保护单已认回（orderId={keep_id}）"
        self._remember_order(lot, keep_id, self._trade_perm_id(keep_trade))
        for _, trade in candidates[1:]:
            oid = self._trade_order_id(trade)
            price = self._protective_price(trade)
            logger.warning("lot %s 发现重复保护单 orderId=%s @%.2f，保留 %s @%.2f 并撤销重复单",
                           lot.lot_id, oid, price, keep_id, keep_price)
            if not await self._cancel_trade_and_wait(trade):
                logger.error("lot %s 重复保护单 %s 撤单未确认，需人工核查",
                             lot.lot_id, oid)
        lot.updated_at = time.time()

    def _emit_closed(self, lot: "OrderLot") -> None:
        """平仓事件上抛（console 注入 on_lot_closed：事件流 + 通知）。"""
        cb = self.on_lot_closed
        if cb is None:
            return
        try:
            cb(lot.to_dict())
        except Exception:
            logger.exception("on_lot_closed 回调异常")

    # ------------------------------------------------------------------
    # 对账（启动后 + 周期性）：重挂 watcher / 收敛本 lot 自有订单
    # ------------------------------------------------------------------

    async def reconcile(self) -> None:
        """按本 lot 自有订单对账；快照未知或无法归因时只保留并告警。"""
        if not (self._gw.enabled and self._gw._connected):
            return
        open_lots = [l for l in self._lots.values() if l.state in _OPEN_STATES]
        # 复活候选：已终结但本地零成交的 lot（OCA 竞态漏报的受害者）
        revive_candidates = [
            l for l in self._lots.values()
            if l.state in (CANCELLED, FAILED)
            and l.filled_qty <= 0 and l.close_filled_qty <= 0
            and l.entry_order_id is not None
            and l.lot_id not in self._revive_done]
        if not open_lots and not revive_candidates:
            return
        try:
            trades = await self._gw.trade_map()
            broker_pos = await self._gw.positions()
        except Exception as exc:
            logger.warning("对账快照查询异常，禁止本轮撤改平: %s", exc)
            return
        if trades is None or broker_pos is None:
            logger.warning("对账快照不可用（trade_map=%s positions=%s），"
                           "禁止本轮任何撤改平", type(trades).__name__,
                           type(broker_pos).__name__)
            return

        # 复活通道：券商仍有同向持仓 + 本 lot 订单确有成交（OCA 竞态漏报）
        # → 复活为 ACTIVE 并恢复保护。每 lot 只复活一次（_revive_done）。
        for lot in revive_candidates:
            ticker = lot.symbol.split(":")[-1].upper()
            held = broker_pos.get(ticker, 0)
            if not held or (held > 0) != (lot.side == "long"):
                continue
            async with self._lot_lock(lot):
                if lot.state not in (CANCELLED, FAILED):
                    continue
                truth = await self._ground_truth_entry_fills(lot)
                if not truth:
                    continue
                self._revive_done.add(lot.lot_id)
                if await self._activate_from_ground_truth(
                        lot, truth, "对账复活", cap_qty=abs(int(held))):
                    logger.warning("lot %s %s 对账复活（券商持仓 %s，成交真相 %s）",
                                   lot.lot_id, lot.symbol, held, truth)
                    cb = self.on_lot_revived
                    if cb is not None:
                        try:
                            cb(lot.to_dict())
                        except Exception:
                            logger.exception("on_lot_revived 回调异常")

        for lot in open_lots:
            async with self._lot_lock(lot):
                if lot.state not in _OPEN_STATES:
                    continue
                try:
                    ticker = lot.symbol.split(":")[-1].upper()
                    held = broker_pos.get(ticker, 0)
                    await self._dedupe_protective_orders(lot, trades)
                    if lot.state == PENDING:
                        trade = trades.get(lot.entry_order_id)
                        if trade is None:
                            # 订单不在开放快照（上一进程会话的已终结订单不会回来）：
                            # 查成交真相（executions 跨会话有效）再收敛——
                            # 有成交按真相激活；无成交且订单不可见 = 已终结无成交，
                            # 收敛 CANCELLED，绝不永远挂 PENDING。
                            truth = await self._ground_truth_entry_fills(lot)
                            if truth is None:
                                logger.warning(
                                    "对账: lot %s 入场单(orderId=%s)未找到且"
                                    "成交真相查询失败，保持 PENDING",
                                    lot.lot_id, lot.entry_order_id)
                                continue
                            if await self._activate_from_ground_truth(
                                    lot, truth, "对账"):
                                continue
                            lot.state = CANCELLED
                            lot.note = ("入场单不在开放订单快照且成交真相无成交，"
                                        "按 CANCELLED 收敛")
                            lot.updated_at = time.time()
                            self._save()
                            logger.info("对账: lot %s 收敛 CANCELLED"
                                        "（订单不可见且无成交）", lot.lot_id)
                            continue
                        lot.entry_trade = trade
                        self._remember_order(lot, self._trade_perm_id(trade))
                        trail = trades.get(lot.trail_order_id)
                        if trail is not None:
                            lot.trail_trade = trail
                            self._remember_order(lot, self._trade_perm_id(trail))
                        if lot.breakout_order_id is not None:
                            brk = trades.get(lot.breakout_order_id)
                            if brk is not None:
                                lot.breakout_trade = brk
                                self._remember_order(
                                    lot, self._trade_perm_id(brk))
                        self._record_entry_fill(lot)
                        if trade.isDone():
                            await self._handle_entry_terminal(lot)
                        else:
                            self._start_watch(lot, "entry", self._watch_entry(lot))
                        continue

                    if lot.state == CLOSING:
                        trade = trades.get(lot.close_order_id)
                        if trade is None and lot.close_permId is not None:
                            trade = next(
                                (t for t in trades.values()
                                 if self._trade_perm_id(t) == lot.close_permId), None)
                        if trade is None:
                            outcome = await self._recover_missing_close(lot)
                            if outcome == "closed":
                                self._emit_closed(lot)
                            else:
                                logger.warning("对账: lot %s CLOSING 平仓单(orderId=%s/"
                                               "permId=%s)不可见，保持 CLOSING，禁止重发",
                                               lot.lot_id, lot.close_order_id,
                                               lot.close_permId)
                            continue
                        lot.close_trade = trade
                        self._remember_order(
                            lot, self._trade_order_id(trade),
                            self._trade_perm_id(trade))
                        self._record_exit_fill(lot, "close")
                        if trade.isDone():
                            outcome = await self._handle_close_terminal(lot)
                            if outcome == "restored":
                                self._start_exit_watch(lot)
                            elif outcome == "closed":
                                self._emit_closed(lot)
                        else:
                            self._start_close_watch(lot)
                        continue

                    # ACTIVE：只处理本 lot 的保护单/平仓成交，不用 symbol 净持仓
                    # 给单 lot 推断余仓或补登 external_close。
                    trail = trades.get(lot.trail_order_id)
                    if trail is not None:
                        lot.trail_trade = trail
                        self._remember_order(lot, self._trade_perm_id(trail))
                    trail = lot.trail_trade
                    if trail is not None:
                        self._record_exit_fill(lot, "trail")
                        if trail.isDone():
                            outcome = await self._handle_trail_terminal(lot)
                            if outcome == "unprotected" \
                                    and lot.protection_restore_needed \
                                    and lot.filled_qty > 0:
                                outcome = ("restored" if await self._restore_protection(lot)
                                           else "unprotected")
                            if outcome == "restored":
                                self._start_exit_watch(lot)
                            elif outcome == "closed":
                                self._emit_closed(lot)
                            continue
                        if lot.protection_restore_needed and lot.filled_qty > 0:
                            # 入场部分成交时 modify_stop_qty 的瞬时失败可在此安全重试。
                            await self._restore_protection(lot)
                        self._start_exit_watch(lot)
                        continue

                    if lot.protection_restore_needed and lot.filled_qty > 0:
                        if await self._restore_protection(lot):
                            self._start_exit_watch(lot)
                        continue
                    logger.warning("对账: lot %s 缺少可归因保护单；symbol 净持仓=%s，"
                                   "但净持仓不能归因到单 lot，保持 ACTIVE，不撤单、"
                                   "不推断数量、不补登 external_close",
                                   lot.lot_id, held)
                except Exception:
                    logger.exception("对账处理 lot %s 失败，保留原状态待下轮", lot.lot_id)
        self._save()

    # ------------------------------------------------------------------
    # 查询与聚合
    # ------------------------------------------------------------------

    def risk_exposure(self) -> dict:
        """总风险敞口：全部开放 lot 的剩余风险合计（四舍五入 2 位）。

        口径（定稿）：
        - PENDING（未成交）：qty × |entry_px − stop_px|（全额计划风险）；
        - ACTIVE/CLOSING：filled_qty × 当前不利距离——多仓
          max(0, avg_fill − stop_px)，空仓 max(0, stop_px − avg_fill)；
          avg_fill 缺失回退 entry_px。
        保本（止损已到成本或更有利）按上式自然为 0，不看 breakeven_reached。
        """
        lots: List[dict] = []
        total = 0.0
        for lot in self._lots.values():
            if lot.state not in _OPEN_STATES:
                continue
            if lot.state == PENDING:
                risk = lot.qty * abs(lot.entry_px - lot.stop_px)
            else:
                cost = lot.avg_fill if lot.avg_fill > 0 else lot.entry_px
                if lot.side == "long":
                    dist = max(0.0, cost - lot.stop_px)
                else:
                    dist = max(0.0, lot.stop_px - cost)
                risk = lot.filled_qty * dist
            risk = round(risk, 2)
            total += risk
            lots.append({"lot_id": lot.lot_id, "symbol": lot.symbol,
                         "side": lot.side, "risk_usd": risk})
        return {"total": round(total, 2), "lots": lots}

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

    def pyramid_states(self) -> dict:
        import copy
        return copy.deepcopy(self._pyramid_groups)

    def save_pyramid_state(self, group_id: str, state: dict) -> None:
        if self._pyramid_groups.get(group_id) == state:
            return
        import copy
        self._pyramid_groups[group_id] = copy.deepcopy(state)
        self._save()

    def mark_pyramid_seed(self, lot_id: str, risk_dist: float,
                          initial_risk_usd: float) -> None:
        lot = self._lots.get(lot_id)
        if lot is None:
            return
        lot.signal_snapshot = {**lot.signal_snapshot,
                               "pyramid_seed": True,
                               "entry_risk_dist": float(risk_dist),
                               "initial_risk_usd": float(initial_risk_usd),
                               "entry_index": 1}
        self._save()

    def mark_pyramid_group(self, lot_id: str, meta: dict) -> None:
        lot = self._lots.get(lot_id)
        if lot is not None:
            lot.signal_snapshot = {**lot.signal_snapshot, **meta}
            self._save()

    async def reprice_midpoint_lot(self, lot_id: str, new_limit: float,
                                   new_stop: float, bar_time: float,
                                   new_breakout: float = 0.0) -> Tuple[bool, str]:
        """PENDING 中点单按新 bar 改价；OCA 双腿时 LMT/突破/止损三腿同步，
        股数不变，每 bar 只尝试一次。"""
        lot = self._lots.get(lot_id)
        if lot is None:
            return False, f"lot 不存在: {lot_id}"
        async with self._lot_lock(lot):
            if lot.limit_type != "midpoint":
                return False, "仅中点限价单支持逐 bar 改价"
            if lot.state != PENDING:
                return False, f"lot 状态 {lot.state} 不可改价"
            if bar_time and lot.reprice_bar_time >= bar_time:
                return False, "该 bar 已尝试过改价"
            if lot.breakout_escalated:
                return False, "已升级市价单，不再逐 bar 改价"
            new_limit = round(new_limit, 2)
            new_stop = round(new_stop, 2)
            new_breakout = round(new_breakout, 2)
            if new_limit <= 0 or new_stop <= 0:
                return False, "限价/止损必须为正"
            if lot.side == "long" and new_stop >= new_limit:
                return False, "多仓止损必须低于限价"
            if lot.side == "short" and new_stop <= new_limit:
                return False, "空仓止损必须高于限价"
            has_oca = lot.breakout_order_id is not None
            if has_oca:
                if new_breakout <= 0:
                    return False, "OCA 中点单缺少突破触发价"
                if lot.side == "long" and new_breakout <= new_limit:
                    return False, "多仓突破触发价必须高于限价"
                if lot.side == "short" and new_breakout >= new_limit:
                    return False, "空仓突破触发价必须低于限价"
            trade = lot.entry_trade
            if trade is not None and trade.isDone():
                return False, "入场单已终态，等待成交状态同步"
            if has_oca and lot.breakout_trade is not None \
                    and lot.breakout_trade.isDone():
                return False, "突破腿已终态，等待成交状态同步"
            if has_oca:
                ok = await self._gw.modify_midpoint_oca(
                    lot, new_limit, new_breakout, new_stop)
            else:
                ok = await self._gw.modify_limit_stop(lot, new_limit, new_stop)
            if ok:
                lot.entry_px = new_limit
                lot.stop_px = new_stop
                if has_oca:
                    lot.breakout_px = new_breakout
            lot.reprice_bar_time = bar_time or lot.reprice_bar_time
            lot.updated_at = time.time()
            self._save()
            return ok, ("" if ok else "中点父子单改价失败，保留原挂单")

    async def reprice_retest_lot(self, lot_id: str, new_limit: float,
                                 new_stop: float, bar_time: float,
                                 new_qty: int = 0) -> Tuple[bool, str]:
        """PENDING 回踩单按新 bar_c2 改价 + 以损定量改股数。

        new_qty 为按固定风险预算（init_risk_usd）与新止损距离算出的目标
        股数（调用方已封顶 RISK_MAX_ORDER_QTY）：仅 filled_qty == 0 时生效，
        与 lot.qty 不同才走改价改量路径（modify_limit_stop_qty），相同则
        走纯价格修改（modify_limit_stop）；< 1 拒绝本次改价，记录 bar_time
        下根重试。已有部分成交（filled_qty > 0）时忽略 new_qty 只改价。
        每 bar 只尝试一次（reprice_bar_time 幂等）；init_* R 基准字段不动。
        返回 (True, "unchanged") 表示价格股数均未变化、未发起修改。"""
        lot = self._lots.get(lot_id)
        if lot is None:
            return False, f"lot 不存在: {lot_id}"
        async with self._lot_lock(lot):
            if lot.limit_type != "retest":
                return False, "仅回踩限价单支持逐 bar 改价"
            if lot.state != PENDING:
                return False, f"lot 状态 {lot.state} 不可改价"
            if bar_time and lot.reprice_bar_time >= bar_time:
                return False, "该 bar 已尝试过改价"
            new_limit = round(new_limit, 2)
            new_stop = round(new_stop, 2)
            new_qty = int(new_qty or 0)
            if new_limit <= 0 or new_stop <= 0:
                return False, "限价/止损必须为正"
            if lot.side == "long" and new_stop >= new_limit:
                return False, "多仓止损必须低于限价"
            if lot.side == "short" and new_stop <= new_limit:
                return False, "空仓止损必须高于限价"
            if lot.filled_qty > 0:
                # 已有部分成交：只改价不改股数（防止改到比已成交还少）
                new_qty = 0
            elif new_qty < 1:
                # 固定风险预算按新止损距离算不出 1 股：拒绝本次改价，
                # 记录 bar_time，保留原单下根 bar 重试
                lot.reprice_bar_time = bar_time or lot.reprice_bar_time
                lot.updated_at = time.time()
                self._save()
                return False, "新止损距离过大，固定风险预算不足 1 股，保留原挂单"
            trade = lot.entry_trade
            if trade is not None and trade.isDone():
                return False, "入场单已终态，等待成交状态同步"
            qty_change = new_qty if new_qty != lot.qty else 0
            price_same = (new_limit == round(lot.entry_px, 2)
                          and new_stop == round(lot.stop_px, 2))
            if price_same and qty_change == 0:
                # 防抖：股数与价格（1 股/1 美分级）均未变，不发起修改
                lot.reprice_bar_time = bar_time or lot.reprice_bar_time
                lot.updated_at = time.time()
                self._save()
                return True, "unchanged"
            if qty_change:
                ok = await self._gw.modify_limit_stop_qty(
                    lot, new_limit, new_stop, qty_change)
            else:
                ok = await self._gw.modify_limit_stop(lot, new_limit, new_stop)
            if ok:
                lot.entry_px = new_limit
                lot.stop_px = new_stop
                if qty_change:
                    lot.qty = qty_change   # init_* 为 R 基准，不动
            lot.reprice_bar_time = bar_time or lot.reprice_bar_time
            lot.updated_at = time.time()
            self._save()
            return ok, ("" if ok else "回踩父子单改价失败，保留原挂单")

    async def escalate_midpoint_to_market(self, lot_id: str) -> Tuple[bool, str]:
        """PENDING 中点单突破升级：LMT 父单原地改 MKT（同 orderId），STP 子单不动。

        触发条件由上层判断（最新成交价突破上根已收盘 bar 的高/低点）；
        股数与止损价不变，初始 R 风险记录（init_*）不受影响。一次性操作。
        """
        lot = self._lots.get(lot_id)
        if lot is None:
            return False, f"lot 不存在: {lot_id}"
        async with self._lot_lock(lot):
            if lot.limit_type != "midpoint":
                return False, "仅中点限价单支持突破升级"
            if lot.state != PENDING:
                return False, f"lot 状态 {lot.state} 不可升级"
            if lot.breakout_escalated:
                return False, "已升级为市价单"
            if lot.breakout_order_id is not None:
                return False, "OCA 双腿中点单由突破腿原生处理，不做市价升级"
            trade = lot.entry_trade
            if trade is not None and trade.isDone():
                return False, "入场单已终态，等待成交状态同步"
            ok = await self._gw.convert_entry_to_market(lot)
            if ok:
                lot.breakout_escalated = True
                lot.note = "突破上根K线高/低点，中点限价单已升级为市价单"
            lot.updated_at = time.time()
            self._save()
            return ok, ("" if ok else "市价升级失败，保留原中点限价挂单")

    async def _update_trail_unlocked(self, lot: OrderLot,
                                     new_stop: float) -> Tuple[bool, str]:
        """不加锁的止损修改核心；调用方必须已持有对应 lot 锁。"""
        if lot.state not in (PENDING, ACTIVE):
            return False, f"lot 已非活跃状态 ({lot.state})"
        if new_stop <= 0:
            return False, "触发价必须为正"
        now = time.time()
        if lot.trail_modify_blocked_until > now:
            remain = round(lot.trail_modify_blocked_until - now)
            return False, f"IBKR 止损修改冷却中，剩余 {remain}s，请检查 TWS 连接"
        cur = lot.stop_px
        new = max(cur, new_stop) if lot.side == "long" else min(cur, new_stop)
        if abs(new - cur) < 0.01:
            return False, f"触发价未变（{cur}）"
        ok = await self._gw.modify_stop(lot, round(new, 2))
        if ok:
            lot.stop_px = round(new, 2)
            # 止损处于成本有利侧（含越过成本）即视为已保本，不要求精确等于成本
            cost = lot.avg_fill if lot.avg_fill > 0 else lot.entry_px
            favorable = ((lot.side == "long" and new >= cost - 0.01)
                         or (lot.side == "short" and new <= cost + 0.01))
            if cost > 0 and favorable and not lot.breakeven_reached:
                lot.breakeven_reached = True
                lot.breakeven_time = time.time()
                if lot.entry_time > 0:
                    lot.breakeven_seconds = round(
                        lot.breakeven_time - lot.entry_time, 1)
            lot.updated_at = time.time()
            self._save()
        else:
            lot.updated_at = time.time()
            self._save()   # 持久化冷却时间/失败备注，防止每根 bar 连续打崩 TWS
        return ok, ("" if ok else "修改失败")

    async def update_trail(self, lot_id: str,
                           new_stop: float) -> Tuple[bool, str]:
        """修改某个 lot 的保护止损触发价（STP/TRAIL 通用，只向有利方向，1 美分防抖）。"""
        lot = self._lots.get(lot_id)
        if lot is None:
            return False, f"lot 不存在: {lot_id}"
        async with self._lot_lock(lot):
            return await self._update_trail_unlocked(lot, new_stop)

    async def breakeven(self, lot_id: str) -> Tuple[bool, str]:
        """保本：优先认回并修改原括号子单；确认缺失后才恢复独立 STOP。"""
        lot = self._lots.get(lot_id)
        if lot is None:
            return False, f"lot 不存在: {lot_id}"
        async with self._lot_lock(lot):
            if lot.state != ACTIVE:
                return False, f"lot 状态 {lot.state} 不可保本（仅 ACTIVE）"
            if lot.filled_qty <= 0:
                return False, "lot 无实际余仓"
            cost = lot.avg_fill if lot.avg_fill > 0 else lot.entry_px
            if cost <= 0:
                return False, "无入场成本"
            already = ((lot.side == "long" and lot.stop_px >= cost - 0.01)
                       or (lot.side == "short" and lot.stop_px <= cost + 0.01))
            if already:
                if not lot.breakeven_reached:
                    lot.breakeven_reached = True
                    lot.breakeven_time = time.time()
                    if lot.entry_time > 0:
                        lot.breakeven_seconds = round(
                            lot.breakeven_time - lot.entry_time, 1)
                    lot.updated_at = time.time()
                    self._save()
                return True, "已处于保本状态"
            trail = lot.trail_trade
            if trail is not None and trail.isDone() \
                    and int(trail.orderStatus.filled or 0) > 0:
                return False, "保护单已发生成交，等待平仓回报处理"
            # 保护单终态/缺失时，先用最新订单快照认回括号子单；确认不存在才恢复独立 STOP
            if trail is None or trail.isDone():
                if lot.protection_find_blocked_until > time.time():
                    remain = round(lot.protection_find_blocked_until - time.time())
                    return False, f"保护单快照认回冷却中（{remain}s），稍后会自动重试"
                found, snapshot_complete = await self._find_active_protection(lot)
                if found is not None:
                    trail = lot.trail_trade
                elif not snapshot_complete:
                    lot.protection_find_blocked_until = time.time() + 30
                    self._save()
                    return False, "订单快照不完整，禁止新建保护单（30s 后自动重试）"
            if trail is None or trail.isDone():
                ok_pos, pos_err = await self._has_broker_position(lot)
                if not ok_pos:
                    return False, pos_err
                result = await self._gw.place_stop_only(
                    lot.symbol, lot.side, lot.filled_qty, cost)
                if not result.get("ok"):
                    return False, result.get("error") or "恢复保护单失败"
                lot.trail_order_id = (result.get("stop_order_id")
                                      or result.get("order_id"))
                lot.trail_trade = result.get("trade")
                lot.trail_order_filled_qty = 0
                lot.trail_order_avg_fill = 0.0
                lot.stop_px = round(cost, 2)
                lot.breakeven_reached = True
                lot.breakeven_time = time.time()
                if lot.entry_time > 0:
                    lot.breakeven_seconds = round(
                        lot.breakeven_time - lot.entry_time, 1)
                lot.protection_restore_needed = False
                self._remember_order(
                    lot, lot.trail_order_id,
                    self._trade_order_id(lot.trail_trade),
                    self._trade_perm_id(lot.trail_trade))
                lot.updated_at = time.time()
                self._save()
                logger.info("lot %s 保本：恢复独立 STOP @成本 %.2f（原保护单终态）",
                            lot.lot_id, cost)
                return True, "保本（恢复保护单挂成本）"
            # 保护单活跃 → 同一把锁内直接修改止损到成本
            return await self._update_trail_unlocked(lot, round(cost, 2))

    async def cancel_lot(self, lot_id: str) -> Tuple[bool, str]:
        """PENDING 撤入场并等待终态；部分成交转 ACTIVE，绝不撤成裸仓。"""
        lot = self._lots.get(lot_id)
        if lot is None:
            return False, f"lot 不存在: {lot_id}"
        async with self._lot_lock(lot):
            if lot.state not in (PENDING, ACTIVE):
                return False, f"lot 状态 {lot.state} 不可撤单"

            is_live = bool(self._gw.enabled and self._gw._connected)
            if lot.state == ACTIVE and is_live:
                return False, ("已成交持仓不能撤单（STOP 是保护单，撤掉会变裸仓）。"
                               "请使用平仓操作。")

            if not is_live:
                lot.state = CANCELLED
                lot.note = "已撤单（dry-run）"
                lot.updated_at = time.time()
                self._save()
                return True, ""

            entry = lot.entry_trade
            if entry is None:
                return False, "缺入场单句柄，撤单未提交；请先对账恢复句柄"
            if lot.trail_trade is None and lot.trail_order_id is not None:
                return False, "缺保护单句柄，无法安全撤单；请先对账恢复句柄"

            self._record_entry_fill(lot)
            brk = lot.breakout_trade
            if brk is not None and not brk.isDone():
                if not await self._cancel_trade_and_wait(brk):
                    lot.note = "突破腿撤单未获券商终态确认，保持 PENDING"
                    lot.updated_at = time.time()
                    self._save()
                    return False, lot.note
            if not await self._cancel_trade_and_wait(entry):
                lot.note = "入场撤单未获券商终态确认，保持 PENDING，禁止伪装成功"
                lot.updated_at = time.time()
                self._save()
                return False, lot.note
            self._record_entry_fill(lot)
            status = entry.orderStatus.status
            ok = await self._handle_entry_terminal(lot)
            lot.updated_at = time.time()
            self._save()

            if lot.state == ACTIVE:
                if status == "Filled":
                    return False, "入场已全部成交，无法撤单，已转 ACTIVE"
                message = f"剩余入场已撤，部分成交 {lot.filled_qty} 股转 ACTIVE"
                if not ok:
                    message += "；但 STOP 数量/句柄异常，需立即核查"
                return ok, message
            if lot.state == CANCELLED:
                lot.note = "入场及无持仓 STOP 均已确认终态"
                self._save()
                return True, "已撤单"
            return False, lot.note or f"撤单后状态未收敛: {status}"

    # ------------------------------------------------------------------
    # mark 价刷新（实盘从 IBKR snapshot；dry-run 用 avg_fill）
    # ------------------------------------------------------------------

    def apply_marks(self, prices: Dict[str, Optional[float]], save: bool = True) -> None:
        """按给定价格表刷新所有 ACTIVE lot 的 mark 价、PnL 与 MAE/MFE。"""
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
        if changed and save:
            self._save()

    async def refresh_marks(self) -> None:
        """刷新所有活跃 lot 的 mark 价并重算 PnL（实盘从 IBKR snapshot）。"""
        symbols = {l.symbol for l in self._lots.values()
                   if l.state == ACTIVE}
        prices: Dict[str, Optional[float]] = {}
        for sym in symbols:
            prices[sym] = await self._gw.last_price(sym)
        self.apply_marks(prices)

    # ------------------------------------------------------------------
    # 持久化
    # ------------------------------------------------------------------

    def _archive(self, lot: "OrderLot") -> None:
        """平仓后追加到当日历史归档（append，永不覆盖）。"""
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
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
                       "pyramid_groups": self._pyramid_groups,
                       "saved_at": time.time()}
            temporary = self._state_path.with_suffix(self._state_path.suffix + ".tmp")
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8")
            temporary.replace(self._state_path)
        except OSError:
            logger.exception("lot 状态落盘失败")

    def _load(self) -> None:
        if not self._state_path.exists():
            return
        try:
            payload = json.loads(self._state_path.read_text(encoding="utf-8"))
            self._pyramid_groups = payload.get("pyramid_groups") or {}
            for d in payload.get("lots", []):
                lot = OrderLot.from_dict(d)
                self._lots[lot.lot_id] = lot
            logger.info("已恢复 %d 个 lot 批次（%s）", len(self._lots),
                        self._state_path.name)
        except (OSError, ValueError, TypeError) as e:
            logger.warning("lot 状态加载失败: %s", e)
