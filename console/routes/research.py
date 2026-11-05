"""HTTP adapters; application logic lives in services."""

from fastapi import Depends, HTTPException
from console.schemas import BtLoadRequest, BtRunRequest
from console.dependencies import get_services
from fastapi import APIRouter

router = APIRouter()


@router.post("/api/bt/run")
async def api_bt_run(req: BtRunRequest, *, services=Depends(get_services)) -> dict:
    return await services.research.api_bt_run(req=req)


@router.get("/api/shadow")
async def api_shadow(*, services=Depends(get_services)) -> dict:
    return await services.research.api_shadow()


@router.get("/api/shadow/report")
async def api_shadow_report(day: str = "", *, services=Depends(get_services)) -> dict:
    return await services.research.api_shadow_report(day=day)


@router.get("/api/shadow/strategy-report")
async def api_strategy_shadow_report(day: str = "", *, services=Depends(get_services)) -> dict:
    try:
        return services.strategy_shadow.report(day)
    except ValueError as error:
        raise HTTPException(400, "交易日格式应为 YYYY-MM-DD") from error


@router.post("/api/bt/load")
async def api_bt_load(req: BtLoadRequest, *, services=Depends(get_services)) -> dict:
    return await services.research.api_bt_load(req=req)


@router.post("/api/bt/step")
async def api_bt_step(n: int = 1, *, services=Depends(get_services)) -> dict:
    return await services.research.api_bt_step(n=n)


@router.post("/api/bt/run_all")
async def api_bt_run_all(*, services=Depends(get_services)) -> dict:
    return await services.research.api_bt_run_all()


@router.get("/api/bt/state")
async def api_bt_state(*, services=Depends(get_services)) -> dict:
    return await services.research.api_bt_state()


@router.get("/api/backtest")
async def api_backtest(
    symbol: str, day: str = "", bars: int = 500, *, services=Depends(get_services)
) -> dict:
    return await services.research.api_backtest(symbol=symbol, day=day, bars=bars)


@router.get("/api/performance")
async def api_performance(*, services=Depends(get_services)) -> dict:
    return await services.research.api_performance()


@router.get("/api/returns/compare")
async def api_returns_compare(
    start: str = "", end: str = "", *, services=Depends(get_services)
) -> dict:
    return await services.research.api_returns_compare(start=start, end=end)
