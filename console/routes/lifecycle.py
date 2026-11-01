"""HTTP adapters; application logic lives in services."""

from fastapi import Depends
from console.dependencies import get_services
from fastapi import APIRouter

router = APIRouter()


@router.get("/api/startup", include_in_schema=False)
async def api_startup(*, services=Depends(get_services)) -> dict:
    return await services.lifecycle.api_startup()
