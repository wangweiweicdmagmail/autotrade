"""回测触价成交模型；不连接券商。"""

from dataclasses import dataclass
from typing import Dict, List, Optional
from engine.states import ACTIVE, DONE, PENDING


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


class SimBroker:
    """回测网关：LMT 触价成交、STP 触价成交，与 LiveBroker 同接口。"""

    def __init__(self) -> None:
        self._lots: Dict[str, SimLot] = {}
        self._seq = 0

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
        self._seq += 1
        lot = SimLot(
            lot_id=f"sim-{self._seq:04d}",
            symbol=symbol,
            side=side,
            qty=qty,
            entry_px=limit_px,
            stop_px=stop_px,
            init_stop_px=stop_px,
            limit_type=limit_type,
            reason=reason,
        )
        self._lots[lot.lot_id] = lot
        return lot

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
                if (lot.side == "long" and price <= lot.entry_px) or (
                    lot.side == "short" and price >= lot.entry_px
                ):
                    lot.state = ACTIVE
                    lot.avg_fill = lot.entry_px
                    lot.fill_time = ts
            elif lot.state == ACTIVE:
                self._track_mfe(lot, price)
                if (lot.side == "long" and price <= lot.stop_px) or (
                    lot.side == "short" and price >= lot.stop_px
                ):
                    self._settle(lot, lot.stop_px, "stop")
                    lot.exit_time = ts

    def _track_mfe(self, lot: SimLot, price: float) -> None:
        if lot.avg_fill <= 0:
            return
        sign = 1 if lot.side == "long" else -1
        lot.mfe = max(lot.mfe, round((price - lot.avg_fill) * sign * lot.qty, 2))

    def closed_results(self) -> List[SimLot]:
        return [l for l in self._lots.values() if l.state == DONE]
