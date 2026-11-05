"""Isolated, simulated-only night test endpoints."""

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from console.dependencies import get_services, require_trader

Services = Annotated[Any, Depends(get_services)]
Trader = Annotated[str, Depends(require_trader)]

router = APIRouter(prefix="/api/night-test")


class ConnectRequest(BaseModel):
    symbol: str = Field(min_length=3, max_length=64)
    strategy: str = Field(default="ema9_pyramid", max_length=64)
    session: Literal["regular", "extended", "24h"] = "24h"


class StartRequest(BaseModel):
    risk_budget: float = Field(default=100, gt=0, le=100000, allow_inf_nan=False)
    window_start: str = Field(default="20:00", pattern=r"^\d{2}:\d{2}$")
    window_end: str = Field(default="04:00", pattern=r"^\d{2}:\d{2}$")


@router.get("/status")
async def status(services: Services):
    return services.night_test.status()


@router.post("/connect")
async def connect(req: ConnectRequest, trader: Trader, services: Services):
    try:
        return await services.night_test.connect(req.symbol, req.strategy, req.session)
    except ValueError as error:
        raise HTTPException(409, str(error)) from error


@router.post("/start")
async def start(req: StartRequest, trader: Trader, services: Services):
    try:
        return await services.night_test.start_run(
            req.risk_budget, req.window_start, req.window_end
        )
    except ValueError as error:
        raise HTTPException(409, str(error)) from error


@router.post("/stop")
async def stop(trader: Trader, services: Services):
    return await services.night_test.stop_run()


@router.post("/disconnect")
async def disconnect(trader: Trader, services: Services):
    return await services.night_test.disconnect()


@router.get("/report")
async def report(services: Services):
    return JSONResponse(
        services.night_test.report(),
        headers={
            "Content-Disposition": 'attachment; filename="night-paper-test.json"',
            "Cache-Control": "no-store",
        },
    )
