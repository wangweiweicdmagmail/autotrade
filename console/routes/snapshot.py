"""HTTP adapters; application logic lives in services."""

from fastapi import Depends
from fastapi import WebSocket
from console.dependencies import get_services
from fastapi import APIRouter

router = APIRouter()


@router.get("/api/status")
async def api_status(*, services=Depends(get_services)) -> dict:
    return await services.snapshot.api_status()


@router.websocket("/ws")
async def ws_endpoint(ws: WebSocket, *, services=Depends(get_services)) -> None:
    return await services.snapshot.ws_endpoint(ws=ws)


@router.websocket("/ws/copy")
async def ws_copy_endpoint(ws: WebSocket, *, services=Depends(get_services)) -> None:
    await services.snapshot.ws_copy_endpoint(ws)
