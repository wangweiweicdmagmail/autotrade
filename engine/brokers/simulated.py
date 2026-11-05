"""模拟 MKT 当前观察价成交、LMT 触价成交及 STOP；不连接券商。"""

from dataclasses import dataclass
from typing import Dict, List, Optional
import math
from engine.states import ACTIVE, DONE, PENDING
from domain.contracts import FillEvent, OrderPlan, StopAmendment, OrderState


@dataclass
class SimLot:
    lot_id: str
    symbol: str
    side: str
    qty: int
    entry_px: float
    stop_px: float
    init_stop_px: float = 0.0  # 初始止损（跟踪移动后不变，RR/MFE_R 的风险基准）
    state: str = PENDING
    avg_fill: float = 0.0
    exit_px: float = 0.0
    exit_reason: str = ""
    realized_pnl: float = 0.0
    mfe: float = 0.0  # 最大浮盈（美元）
    fill_time: float = 0.0  # 成交 bar 时间
    exit_time: float = 0.0  # 了结 bar 时间
    limit_type: str = ""
    reason: str = ""
    entry_order_type: str = "LMT"
    risk_reference_price: float = 0.0

    @property
    def lifecycle(self) -> OrderState:
        return OrderState.from_legacy(self.state)


class SimBroker:
    """模拟适配：Pine MKT 不等待回踩参考价；手工 LMT 保留触价行为。"""

    def __init__(self) -> None:
        self._lots: Dict[str, SimLot] = {}
        self._seq = 0
        self.fill_events: List[FillEvent] = []
        self._prices = {}

    def get(self, lot_id: str) -> Optional[SimLot]:
        return self._lots.get(lot_id)

    async def place_limit_stop(
        self,
        symbol: str,
        side: str,
        qty: int,
        limit_px: float,
        stop_px: float,
        limit_type: str = "",
        reason: str = "",
        meta: Optional[dict] = None,
    ) -> SimLot:
        meta = meta or {}
        order_type = meta.get("entry_order_type", "LMT")
        if order_type not in ("MKT", "LMT"):
            raise ValueError("入场订单类型无效")
        plan = OrderPlan(symbol, side, qty, limit_px, stop_px, meta.get("risk_budget", 0),
                         meta.get("risk_reference_price", 0), entry_order_type=order_type)
        self._seq += 1
        lot = SimLot(
            lot_id=f"sim-{self._seq:04d}",
            symbol=symbol,
            side=side,
            qty=plan.qty,
            entry_px=plan.entry_price,
            stop_px=plan.stop_price,
            init_stop_px=plan.stop_price,
            limit_type=limit_type,
            reason=reason,
            entry_order_type=order_type,
            risk_reference_price=plan.reference_price,
        )
        self._lots[lot.lot_id] = lot
        if order_type == "MKT":
            quote, quote_time = self._prices.get(symbol, (0, 0))
            price = meta.get("risk_reference_price") or quote
            if price:
                self._fill(lot, price, meta.get("execution_observed_at") or quote_time)
        return lot

    def _fill(self, lot: SimLot, price: float, ts: float) -> None:
        lot.state = ACTIVE
        lot.avg_fill = round(price, 4)
        lot.fill_time = ts
        self.fill_events.append(FillEvent(lot.lot_id, "entry", lot.qty, lot.avg_fill, ts))

    async def cancel(self, lot_id: str) -> bool:
        lot = self._lots.get(lot_id)
        if lot and lot.state == PENDING:
            lot.state = "CANCELLED"
            return True
        return False

    async def close(
        self, lot_id: str, ref_px: float, reason: str, ts: float = 0.0
    ) -> bool:
        lot = self._lots.get(lot_id)
        if lot is None or lot.state != ACTIVE:
            return False
        self._settle(lot, ref_px, reason, ts=ts)
        lot.exit_time = ts
        return True

    async def move_stop(self, lot_id: str, new_stop: float) -> bool:
        lot = self._lots.get(lot_id)
        if lot is None or lot.state not in (PENDING, ACTIVE):
            return False
        try:
            tightened = StopAmendment(lot.side, lot.stop_px, new_stop).tightened_price()
        except ValueError:
            return False
        if abs(tightened - lot.stop_px) < 0.01:
            return False
        lot.stop_px = tightened
        return True

    def _settle(self, lot: SimLot, px: float, reason: str, *, ts=0.0) -> None:
        lot.state = DONE
        lot.exit_px = round(px, 4)
        lot.exit_reason = reason
        sign = 1 if lot.side == "long" else -1
        lot.realized_pnl = round((lot.exit_px - lot.avg_fill) * sign * lot.qty, 2)
        self.fill_events.append(FillEvent(lot.lot_id, "protective" if reason == "stop" else "close",
                                          lot.qty, lot.exit_px, ts))

    # ---- 价格驱动（回测 feed 调用）----

    def on_price(self, symbol: str, price: float, ts: float = 0.0) -> None:
        """MKT 在可用观察价成交；LMT 触价成交；ACTIVE 触及止损价成交。"""
        if not math.isfinite(price) or price <= 0:
            return
        self._prices[symbol] = (price, ts)
        for lot in self._lots.values():
            if lot.symbol != symbol:
                continue
            if lot.state == PENDING:
                if lot.entry_order_type == "MKT" or (lot.side == "long" and price <= lot.entry_px) or (
                    lot.side == "short" and price >= lot.entry_px
                ):
                    self._fill(lot, price if lot.entry_order_type == "MKT" else lot.entry_px, ts)
            elif lot.state == ACTIVE:
                self._track_mfe(lot, price)
                if (lot.side == "long" and price <= lot.stop_px) or (
                    lot.side == "short" and price >= lot.stop_px
                ):
                    self._settle(lot, lot.stop_px, "stop", ts=ts)
                    lot.exit_time = ts

    def _track_mfe(self, lot: SimLot, price: float) -> None:
        if lot.avg_fill <= 0:
            return
        sign = 1 if lot.side == "long" else -1
        lot.mfe = max(lot.mfe, round((price - lot.avg_fill) * sign * lot.qty, 2))

    def closed_results(self) -> List[SimLot]:
        return [l for l in self._lots.values() if l.state == DONE]
