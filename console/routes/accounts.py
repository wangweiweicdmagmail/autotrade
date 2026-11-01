"""HTTP adapters; application logic lives in services."""

from fastapi import Depends
from console.dependencies import get_services
from fastapi import APIRouter

router = APIRouter()


@router.post("/api/refresh")
async def api_refresh(*, services=Depends(get_services)) -> dict:
    return await services.accounts.api_refresh()
