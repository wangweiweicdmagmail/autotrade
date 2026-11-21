"""HTTP adapters; application logic lives in services."""

from fastapi import Depends
from typing import Optional
from console.schemas import AutoAdoptRequest, AutoRejectRequest, AutoStartRequest, AutoInstanceBreakevenRequest, GoldenEntryRequest
from console.dependencies import get_services, require_trader
from fastapi import APIRouter

router = APIRouter()


@router.get("/api/auto/models")
async def api_auto_models(*, services=Depends(get_services)) -> dict:
    return await services.automatic.api_auto_models()


@router.post("/api/auto/start/preview")
async def api_auto_start_preview(
    req: AutoStartRequest,
    trader: str = Depends(require_trader),
    *,
    services=Depends(get_services),
) -> dict:
    return await services.automatic.api_auto_start(req=req, trader=trader, preview=True)


@router.post("/api/auto/start")
async def api_auto_start(
    req: AutoStartRequest,
    trader: str = Depends(require_trader),
    *,
    services=Depends(get_services),
) -> dict:
    return await services.automatic.api_auto_start(req=req, trader=trader)


@router.post("/api/auto/golden-entry")
async def api_golden_entry(
    req: GoldenEntryRequest, trader: str = Depends(require_trader), *, services=Depends(get_services),
) -> dict:
    return await services.automatic.api_golden_entry(req, trader)


@router.post("/api/auto/stop")
async def api_auto_stop(
    close_positions: bool = False,
    trader: str = Depends(require_trader),
    *,
    services=Depends(get_services),
) -> dict:
    return await services.automatic.api_auto_stop(
        close_positions=close_positions, trader=trader
    )


@router.get("/api/auto/status")
async def api_auto_status(*, services=Depends(get_services)) -> dict:
    return await services.automatic.api_auto_status()


@router.post("/api/auto/adopt")
async def api_auto_adopt(
    req: AutoAdoptRequest,
    trader: str = Depends(require_trader),
    *,
    services=Depends(get_services),
) -> dict:
    return await services.automatic.api_auto_adopt(req=req, trader=trader)


@router.post("/api/auto/instances/{symbol}/stop")
async def api_auto_instance_stop(
    symbol: str,
    close_positions: bool = False,
    trader: str = Depends(require_trader),
    strategy: str = "ema9_pyramid",
    instance_id: str = "default",
    *,
    services=Depends(get_services),
) -> dict:
    return await services.automatic.api_auto_instance_stop(
        symbol=symbol,
        close_positions=close_positions,
        trader=trader,
        strategy=strategy,
        instance_id=instance_id,
    )


@router.post("/api/auto/instances/{symbol}/breakeven")
async def api_auto_instance_breakeven(
    symbol: str, req: AutoInstanceBreakevenRequest,
    trader: str = Depends(require_trader), strategy: str = "ema9_pyramid",
    instance_id: str = "default", *, services=Depends(get_services),
) -> dict:
    return await services.automatic.api_auto_instance_breakeven(
        symbol, req, trader, strategy, instance_id)


@router.post("/api/auto/proposals/{proposal_id}/approve")
async def api_auto_proposal_approve(
    proposal_id: str,
    trader: str = Depends(require_trader),
    *,
    services=Depends(get_services),
) -> dict:
    return await services.automatic.api_auto_proposal_approve(
        proposal_id=proposal_id, trader=trader
    )


@router.post("/api/auto/proposals/{proposal_id}/reject")
async def api_auto_proposal_reject(
    proposal_id: str,
    req: Optional[AutoRejectRequest] = None,
    trader: str = Depends(require_trader),
    *,
    services=Depends(get_services),
) -> dict:
    return await services.automatic.api_auto_proposal_reject(
        proposal_id=proposal_id, req=req, trader=trader
    )
