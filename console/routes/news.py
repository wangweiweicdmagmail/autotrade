"""HTTP adapters; application logic lives in services."""

from fastapi import Depends
from console.dependencies import get_services, require_trader
from fastapi import APIRouter

router = APIRouter()


@router.get("/api/news/live")
async def api_live_news(
    ticker: str, minutes: int = 15, *, services=Depends(get_services)
) -> dict:
    return await services.news.api_live_news(ticker=ticker, minutes=minutes)


@router.get("/api/news")
async def api_news(*, services=Depends(get_services)) -> dict:
    return await services.news.api_news()


@router.post("/api/news/refresh")
async def api_news_refresh(
    trader: str = Depends(require_trader), *, services=Depends(get_services)
) -> dict:
    return await services.news.api_news_refresh(trader=trader)
