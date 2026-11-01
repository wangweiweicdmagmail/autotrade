"""HTTP adapters; application logic lives in services."""

from fastapi import Depends
from fastapi import Request
from console.schemas import ClientRegisterRequest
from console.dependencies import get_services
from fastapi import APIRouter

router = APIRouter()


@router.get("/api/clients/self")
async def api_clients_self(request: Request, *, services=Depends(get_services)) -> dict:
    return await services.clients.api_clients_self(request=request)


@router.post("/api/clients/register")
async def api_clients_register(
    req: ClientRegisterRequest, request: Request, *, services=Depends(get_services)
) -> dict:
    return await services.clients.api_clients_register(req=req, request=request)


@router.post("/api/clients/{token}/approve")
async def api_clients_approve(
    token: str, request: Request, *, services=Depends(get_services)
) -> dict:
    return await services.clients.api_clients_approve(token=token, request=request)


@router.post("/api/clients/{token}/reject")
async def api_clients_reject(
    token: str, request: Request, *, services=Depends(get_services)
) -> dict:
    return await services.clients.api_clients_reject(token=token, request=request)
