"""HTTP adapters; application logic lives in services."""

from fastapi import Depends
from fastapi.responses import FileResponse
from fastapi import Request
from console.dependencies import get_services
from fastapi import APIRouter

router = APIRouter()


@router.post("/api/system/shutdown")
async def api_system_shutdown(
    request: Request, *, services=Depends(get_services)
) -> dict:
    return await services.system.api_system_shutdown(request=request)


@router.get("/", include_in_schema=False)
async def index(*, services=Depends(get_services)) -> FileResponse:
    return await services.system.index()
