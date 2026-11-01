"""HTTP adapters; application logic lives in services."""

from fastapi import Depends
from console.dependencies import get_services
from fastapi import APIRouter

router = APIRouter()


@router.get("/api/market")
async def api_market(*, services=Depends(get_services)) -> dict:
    return await services.signals.api_market()


@router.get("/api/screen")
async def api_screen(*, services=Depends(get_services)) -> dict:
    return await services.signals.api_screen()
