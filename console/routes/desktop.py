"""HTTP adapters; application logic lives in services."""

from fastapi import Depends
from console.schemas import TvShowRequest
from console.dependencies import get_services, require_trader
from fastapi import APIRouter

router = APIRouter()


@router.post("/api/tv/show")
async def api_tv_show(
    req: TvShowRequest,
    trader: str = Depends(require_trader),
    *,
    services=Depends(get_services),
) -> dict:
    return await services.desktop.api_tv_show(req=req, trader=trader)


@router.post("/api/tv/view")
async def api_tv_view(
    req: TvShowRequest,
    trader: str = Depends(require_trader),
    *,
    services=Depends(get_services),
) -> dict:
    return await services.desktop.api_tv_view(req=req, trader=trader)
