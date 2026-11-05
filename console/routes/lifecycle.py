"""HTTP adapters; application logic lives in services."""

from fastapi import Depends
from console.dependencies import get_services
from fastapi import APIRouter, HTTPException, Request

router = APIRouter()


@router.get("/api/startup", include_in_schema=False)
async def api_startup(*, services=Depends(get_services)) -> dict:
    return await services.lifecycle.api_startup()


@router.post("/api/startup/repair-pine-cache", include_in_schema=False)
async def repair_pine_cache(request: Request, *, services=Depends(get_services)) -> dict:
    if (not services.clients._is_local(request)
            or request.headers.get("X-Autotrade-Action") != "repair-pine-cache"):
        raise HTTPException(403, "请在本机控制台修复指标缓存")
    return await services.lifecycle.repair_pine_cache()
