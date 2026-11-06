"""HTTP adapters; application logic lives in services."""

from fastapi import Depends
from console.dependencies import get_services
from fastapi import APIRouter, HTTPException, Query, Request
import asyncio

router = APIRouter()


@router.get("/api/market")
async def api_market(*, services=Depends(get_services)) -> dict:
    return await services.signals.api_market()


@router.get("/api/screen")
async def api_screen(*, services=Depends(get_services)) -> dict:
    return await services.signals.api_screen()

@router.get("/api/market/audit")
async def api_market_audit(
    request: Request,
    symbol: str = Query(min_length=1, max_length=80),
    frame_id: int | None = Query(default=None, ge=1, le=2**63 - 1),
    before: int = Query(default=2**63 - 1, ge=1, le=2**63 - 1),
    limit: int = Query(default=20, ge=1, le=100),
    *,
    services=Depends(get_services),
) -> dict:
    if request.client is None or request.client.host not in ("127.0.0.1", "::1"):
        raise HTTPException(403, "行情审计仅支持本机读取")
    audit = services.market_audit
    rows = await asyncio.to_thread(audit.read, symbol, frame_id=frame_id, before=before, limit=limit)
    return {"symbol": symbol, "audit": audit.status(), "rows": rows}
