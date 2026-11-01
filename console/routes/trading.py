"""HTTP adapters; application logic lives in services."""

from fastapi import Depends
from typing import List
from typing import Optional
from console.schemas import LimitStopOrderRequest, UpdateTrailRequest
from console.dependencies import get_services, require_trader
from fastapi import APIRouter

router = APIRouter()


@router.get("/api/lots")
async def api_lots(
    active_only: bool = False,
    symbol: Optional[str] = None,
    *,
    services=Depends(get_services),
) -> List[dict]:
    return await services.trading.api_lots(active_only=active_only, symbol=symbol)


@router.get("/api/positions")
async def api_positions(*, services=Depends(get_services)) -> List[dict]:
    return await services.trading.api_positions()


@router.post("/api/lots/limit-stop")
async def api_place_limit_stop(
    req: LimitStopOrderRequest,
    trader: str = Depends(require_trader),
    *,
    services=Depends(get_services),
) -> dict:
    return await services.trading.api_place_limit_stop(req=req, trader=trader)


@router.post("/api/lots/{lot_id}/trail")
async def api_update_trail(
    lot_id: str,
    req: UpdateTrailRequest,
    trader: str = Depends(require_trader),
    *,
    services=Depends(get_services),
) -> dict:
    return await services.trading.api_update_trail(
        lot_id=lot_id, req=req, trader=trader
    )


@router.post("/api/lots/{lot_id}/cancel")
async def api_cancel_lot(
    lot_id: str,
    trader: str = Depends(require_trader),
    *,
    services=Depends(get_services),
) -> dict:
    return await services.trading.api_cancel_lot(lot_id=lot_id, trader=trader)


@router.post("/api/lots/{lot_id}/close")
async def api_close_lot(
    lot_id: str,
    trader: str = Depends(require_trader),
    *,
    services=Depends(get_services),
) -> dict:
    return await services.trading.api_close_lot(lot_id=lot_id, trader=trader)


@router.post("/api/lots/{lot_id}/breakeven")
async def api_breakeven_lot(
    lot_id: str,
    trader: str = Depends(require_trader),
    *,
    services=Depends(get_services),
) -> dict:
    return await services.trading.api_breakeven_lot(lot_id=lot_id, trader=trader)


@router.post("/api/lots/{lot_id}/close-half")
async def api_close_half(
    lot_id: str,
    trader: str = Depends(require_trader),
    *,
    services=Depends(get_services),
) -> dict:
    return await services.trading.api_close_half(lot_id=lot_id, trader=trader)


@router.post("/api/positions/{symbol}/close")
async def api_close_position(
    symbol: str,
    trader: str = Depends(require_trader),
    *,
    services=Depends(get_services),
) -> dict:
    return await services.trading.api_close_position(symbol=symbol, trader=trader)
