"""HTTP adapters; application logic lives in services."""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse

from console.dependencies import get_services

router = APIRouter()


@router.get("/system-docs/{document}", include_in_schema=False)
async def system_documentation(document: str, download: bool = False) -> FileResponse:
    from console.services.documentation import documentation_file

    return documentation_file(document, download=download)


@router.post("/api/system/shutdown")
async def api_system_shutdown(
    request: Request, *, services=Depends(get_services)
) -> dict:
    return await services.system.api_system_shutdown(request=request)


@router.get("/", include_in_schema=False)
async def index(*, services=Depends(get_services)) -> FileResponse:
    return await services.system.index()
