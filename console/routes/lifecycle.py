"""HTTP adapters; application logic lives in services."""

from fastapi import Depends
from console.dependencies import get_services
from fastapi import APIRouter, HTTPException, Request

router = APIRouter()


@router.get("/api/startup", include_in_schema=False)
async def api_startup(request: Request, *, services=Depends(get_services)) -> dict:
    status = await services.lifecycle.api_startup()
    status['tv_login']['local_actions'] = services.clients._is_local(request)
    return status


@router.post('/api/startup/tv-login', include_in_schema=False)
async def tv_login(request: Request, *, services=Depends(get_services)) -> dict:
    if (not services.clients._is_local(request)
            or request.headers.get('X-Autotrade-Action') != 'tv-login'):
        raise HTTPException(403, '请在运行 AutoTrade 的电脑上打开专用 Chrome 登录')
    return await services.tv_login.open_login()
