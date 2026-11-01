"""策略执行端口。模拟价格推进与交易组存储是独立能力。"""

from typing import Optional, Protocol, runtime_checkable


class LotView(Protocol):
    lot_id: str
    state: str
    entry_px: float
    stop_px: float
    qty: int
    avg_fill: float


@runtime_checkable
class TradeBroker(Protocol):
    def get(self, lot_id: str) -> Optional[LotView]: ...

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
    ) -> LotView: ...

    async def cancel(self, lot_id: str) -> bool: ...
    async def close(
        self, lot_id: str, ref_px: float, reason: str, ts: float = 0.0
    ) -> bool: ...
    async def move_stop(self, lot_id: str, new_stop: float) -> bool: ...


@runtime_checkable
class GroupStateStore(Protocol):
    def pyramid_states(self) -> dict: ...
    def save_pyramid_state(self, group_id: str, state: dict) -> None: ...


@runtime_checkable
class SimulatedPriceFeed(Protocol):
    def on_price(self, symbol: str, price: float, ts: float = 0.0) -> None: ...
