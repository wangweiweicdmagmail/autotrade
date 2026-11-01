"""HTTP adapters; application logic lives in services."""

from fastapi import Depends
from console.dependencies import get_services
from fastapi import APIRouter

router = APIRouter()


@router.get("/api/precheck")
async def api_precheck(*, services=Depends(get_services)) -> dict:
    return await services.precheck.api_precheck()


@router.get("/api/precheck/last")
async def api_precheck_last(*, services=Depends(get_services)) -> dict:
    return await services.precheck.api_precheck_last()
