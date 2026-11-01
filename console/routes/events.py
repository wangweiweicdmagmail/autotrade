"""HTTP adapters; application logic lives in services."""

from fastapi import Depends
from typing import Optional
from console.dependencies import get_services
from fastapi import APIRouter

router = APIRouter()


@router.get("/api/logs")
async def api_logs(limit: int = 200, *, services=Depends(get_services)) -> list:
    return await services.events.api_logs(limit=limit)


@router.get("/api/events")
async def api_events(
    event_type: Optional[str] = None,
    limit: int = 500,
    *,
    services=Depends(get_services),
) -> list:
    return await services.events.api_events(event_type=event_type, limit=limit)
