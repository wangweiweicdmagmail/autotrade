"""OrderManager 的执行适配；风险和成交事实仍由执行层处理。"""

from typing import Optional


class LiveBroker:
    """实盘网关：包装 OrderManager（LMT 父 + STP 子，dry-run 虚拟成交）。"""

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
        lot, err = await self._om.place_limit_stop_lot(
            symbol=symbol,
            side=side,
            qty=qty,
            limit_px=limit_px,
            stop_px=stop_px,
            limit_type=limit_type,
            reason=reason,
            risk_usd=(meta or {}).get("risk_budget", 0),
            signal_snapshot={**(meta or {}), "auto_strategy": True},
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
