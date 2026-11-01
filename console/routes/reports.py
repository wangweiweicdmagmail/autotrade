"""HTTP adapters; application logic lives in services."""

from fastapi import Depends
from console.schemas import ReportSourceAddRequest
from console.dependencies import get_services, require_trader
from fastapi import APIRouter

router = APIRouter()


@router.get("/api/reports")
async def api_reports_list(*, services=Depends(get_services)) -> dict:
    return await services.reports.api_reports_list()


@router.get("/api/reports/status")
async def api_reports_status(*, services=Depends(get_services)) -> dict:
    return await services.reports.api_reports_status()


@router.get("/api/reports/{date}")
async def api_reports_get(date: str, *, services=Depends(get_services)) -> dict:
    return await services.reports.api_reports_get(date=date)


@router.post("/api/reports/generate")
async def api_reports_generate(
    trader: str = Depends(require_trader), *, services=Depends(get_services)
) -> dict:
    return await services.reports.api_reports_generate(trader=trader)


@router.get("/api/report/sources")
async def api_report_sources(*, services=Depends(get_services)) -> dict:
    return await services.reports.api_report_sources()


@router.post("/api/report/sources")
async def api_report_source_add(
    req: ReportSourceAddRequest,
    trader: str = Depends(require_trader),
    *,
    services=Depends(get_services),
) -> dict:
    return await services.reports.api_report_source_add(req=req, trader=trader)


@router.post("/api/report/sources/{video_id}/toggle")
async def api_report_source_toggle(
    video_id: str,
    trader: str = Depends(require_trader),
    *,
    services=Depends(get_services),
) -> dict:
    return await services.reports.api_report_source_toggle(
        video_id=video_id, trader=trader
    )


@router.post("/api/report/sources/{video_id}/delete")
async def api_report_source_delete(
    video_id: str,
    trader: str = Depends(require_trader),
    *,
    services=Depends(get_services),
) -> dict:
    return await services.reports.api_report_source_delete(
        video_id=video_id, trader=trader
    )
