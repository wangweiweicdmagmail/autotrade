"""Local read-only research API. Answers are appended with the offline CLI."""
import asyncio
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from console.dependencies import get_services
from engine.research_review import ResearchReview

router = APIRouter(prefix='/api/research')


def local(request: Request):
    if request.client is None or request.client.host not in ('127.0.0.1','::1'):
        raise HTTPException(403,'研究数据只允许本机读取')


@router.get('/data-status')
async def data_status(request: Request, day: str, services=Depends(get_services)):
    local(request)
    store = services.research_archive.store
    if not store.path.exists():
        return {'available':False,'complete':False}
    return await asyncio.to_thread(store.manifest,day)


@router.get('/rankings')
async def rankings(request: Request, day: str, at: int | None = None, services=Depends(get_services)):
    local(request)
    return await asyncio.to_thread(services.research_archive.store.rankings,day,at=at)


@router.get('/bars')
async def bars(request: Request, series_id: str, start: int, end: int, as_of: float | None=None,
               limit: int=Query(2000,ge=1,le=10000), services=Depends(get_services)):
    local(request)
    return await asyncio.to_thread(services.research_archive.store.bars,series_id,start,end,as_of=as_of,limit=limit)


@router.get('/trades/{identity}')
async def trade(request: Request, identity: str, services=Depends(get_services)):
    local(request)
    review = ResearchReview(services.paths.data_dir)
    await asyncio.to_thread(review.sync)
    try:
        return await asyncio.to_thread(review.trade,identity)
    except ValueError as error:
        raise HTTPException(404,str(error)) from error


@router.get('/questions')
async def questions(request: Request, status: str='pending', services=Depends(get_services)):
    local(request)
    review = ResearchReview(services.paths.data_dir)
    return await asyncio.to_thread(review.notes.questions,None if status=='all' else status)
