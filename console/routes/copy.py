"""HTTP adapters; application logic lives in services."""

from fastapi import Depends
from console.schemas import (
    CopyCmdRequest,
    CopyServerCreateRequest,
    CopyServerUpdateRequest,
)
from console.dependencies import get_services, require_trader
from fastapi import APIRouter

router = APIRouter()


@router.get("/api/copy/servers")
async def api_copy_servers(
    trader: str = Depends(require_trader), *, services=Depends(get_services)
) -> dict:
    return await services.copy.api_copy_servers(trader=trader)


@router.post("/api/copy/servers")
async def api_copy_server_add(
    req: CopyServerCreateRequest,
    trader: str = Depends(require_trader),
    *,
    services=Depends(get_services),
) -> dict:
    return await services.copy.api_copy_server_add(req=req, trader=trader)


@router.post("/api/copy/servers/{name}/update")
async def api_copy_server_update(
    name: str,
    req: CopyServerUpdateRequest,
    trader: str = Depends(require_trader),
    *,
    services=Depends(get_services),
) -> dict:
    return await services.copy.api_copy_server_update(name=name, req=req, trader=trader)


@router.post("/api/copy/servers/{name}/delete")
async def api_copy_server_delete(
    name: str, trader: str = Depends(require_trader), *, services=Depends(get_services)
) -> dict:
    return await services.copy.api_copy_server_delete(name=name, trader=trader)


@router.post("/api/copy/servers/{name}/enable")
async def api_copy_server_enable(
    name: str, trader: str = Depends(require_trader), *, services=Depends(get_services)
) -> dict:
    return await services.copy.api_copy_server_enable(name=name, trader=trader)


@router.post("/api/copy/servers/{name}/disable")
async def api_copy_server_disable(
    name: str, trader: str = Depends(require_trader), *, services=Depends(get_services)
) -> dict:
    return await services.copy.api_copy_server_disable(name=name, trader=trader)


@router.post("/api/copy/servers/{name}/cmd")
async def api_copy_server_cmd(
    name: str,
    req: CopyCmdRequest,
    trader: str = Depends(require_trader),
    *,
    services=Depends(get_services),
) -> dict:
    return await services.copy.api_copy_server_cmd(name=name, req=req, trader=trader)
