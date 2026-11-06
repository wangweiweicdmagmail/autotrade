"""OrderManager 的执行适配；风险和成交事实仍由执行层处理。"""

from typing import Optional
from domain.contracts import OrderPlan
from engine.live_audit import decision_market_frame


class LiveBroker:
    """实盘适配：显式选择 MKT/LMT 入场，始终保留 STP 保护子单。"""

    def __init__(self, order_mgr) -> None:
        self._om = order_mgr

    def get(self, lot_id: str):
        return self._om.get(lot_id)

    def pyramid_states(self) -> dict:
        return self._om.pyramid_states()

    def save_pyramid_state(self, group_id: str, state: dict) -> None:
        self._om.save_pyramid_state(group_id, state)

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
    ):
        plan = OrderPlan(symbol, side, qty, limit_px, stop_px, (meta or {}).get("risk_budget", 0),
                         (meta or {}).get("risk_reference_price", 0),
                         entry_order_type=(meta or {}).get("entry_order_type", "LMT"))
        signal_snapshot = {**(meta or {}), "auto_strategy": True}
        frame = decision_market_frame.get()
        if frame is not None:
            signal_snapshot["decision_market_frame"] = frame
        lot, err = await self._om.place_limit_stop_lot(
            symbol=plan.symbol,
            side=plan.side,
            qty=plan.qty,
            limit_px=plan.entry_price,
            stop_px=plan.stop_price,
            limit_type=limit_type,
            reason=reason,
            risk_usd=plan.risk_budget,
            risk_ref_px=plan.reference_price,
            entry_order_type=plan.entry_order_type,
            signal_snapshot=signal_snapshot,
            trader="auto",
        )  # 策略引擎下单归属标记（与人工单区分）
        if lot is None:
            raise RuntimeError(err)
        return lot

    async def mark_seed(
        self, lot_id: str, risk_dist: float, initial_risk_usd: float
    ) -> None:
        self._om.mark_pyramid_seed(lot_id, risk_dist, initial_risk_usd)

    async def mark_pyramid_group(self, lot_id: str, meta: dict) -> None:
        self._om.mark_pyramid_group(lot_id, meta)

    async def cancel(self, lot_id: str) -> bool:
        ok, _ = await self._om.cancel_lot(lot_id)
        return ok

    async def close(
        self, lot_id: str, ref_px: float, reason: str, ts: float = 0.0
    ) -> bool:
        ok, _ = await self._om.close_lot(lot_id, ref_px=ref_px, exit_reason=reason)
        return ok

    async def move_stop(self, lot_id: str, new_stop: float) -> bool:
        ok, _ = await self._om.update_trail(lot_id, new_stop)
        return ok
