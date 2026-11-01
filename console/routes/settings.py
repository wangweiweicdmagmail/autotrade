"""HTTP adapters; application logic lives in services."""

from fastapi import Depends
from console.schemas import SettingsRequest
from console.dependencies import get_services, require_trader
from fastapi import APIRouter

router = APIRouter()


@router.get("/api/settings")
async def api_get_settings(*, services=Depends(get_services)) -> dict:
    return await services.settings.api_get_settings()


@router.post("/api/settings")
async def api_update_settings(
    req: SettingsRequest,
    trader: str = Depends(require_trader),
    *,
    services=Depends(get_services),
) -> dict:
    return await services.settings.api_update_settings(req=req, trader=trader)
