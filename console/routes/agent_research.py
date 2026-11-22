"""Local research API. Writes are limited to the existing research notes journal."""
import asyncio
import sqlite3
from urllib.parse import urlsplit
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field, StrictBool, validator
from console.dependencies import get_services
from engine.research_review import ResearchReview
from engine.research_methods import ResearchNoteConflict
from execution.state_store import PersistenceError

router = APIRouter(prefix='/api/research')


def local(request: Request):
    if request.client is None or request.client.host not in ('127.0.0.1','::1'):
        raise HTTPException(403,'研究数据只允许本机读取')


def local_write(request: Request):
    local(request)
    origin = request.headers.get('origin')
    if request.headers.get('sec-fetch-site') == 'cross-site' or (origin and
        (urlsplit(origin).scheme not in ('http','https') or urlsplit(origin).netloc != request.headers.get('host'))):
        raise HTTPException(403, '只能从当前控制台保存研究记录')
    if request.headers.get('content-type', '').split(';')[0].lower() != 'application/json':
        raise HTTPException(415, '研究记录需要JSON请求')


async def run_research(function, *args):
    try:
        return await asyncio.to_thread(function, *args)
    except ResearchNoteConflict as error:
        raise HTTPException(409, str(error)) from error
    except KeyError as error:
        raise HTTPException(404, str(error)) from error
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    except (OSError, sqlite3.Error, PersistenceError) as error:
        raise HTTPException(503, '研究记录暂不可读，请重试或检查研究日志') from error


class ResearchAnswer(BaseModel):
    class Config:
        extra = 'forbid'
    text: str = Field(min_length=1, max_length=20000)
    revision: str = Field(min_length=1, max_length=100)
    confirmed: StrictBool = False

    @validator('text')
    def meaningful_text(cls, value):
        if not value.strip():
            raise ValueError('请填写交易思考')
        return value.strip()


@router.post('/workbench/refresh')
async def refresh_workbench(request: Request, services=Depends(get_services)):
    local_write(request)
    return await run_research(services.agent_research.refresh)


@router.get('/workbench/days')
async def workbench_days(request: Request, services=Depends(get_services)):
    local(request)
    return await run_research(services.agent_research.days)


@router.get('/workbench/day')
async def workbench_day(request: Request, day: str, services=Depends(get_services)):
    local(request)
    return await run_research(services.agent_research.day, day)


@router.post('/workbench/day')
async def prepare_workbench_day(request: Request, day: str, services=Depends(get_services)):
    local_write(request)
    return await run_research(services.agent_research.prepare_day, day)


@router.post('/workbench/trades/{identity}')
async def workbench_trade(request: Request, identity: str, services=Depends(get_services)):
    local_write(request)
    try:
        return await run_research(services.agent_research.detail, identity)
    except HTTPException as error:
        if error.status_code == 400 and error.detail == '交易组或批次不存在':
            raise HTTPException(404, error.detail) from error
        raise


@router.post('/workbench/trades/{identity}/questions/{question_id}')
async def answer_workbench(request: Request, identity: str, question_id: str, answer: ResearchAnswer,
                           services=Depends(get_services)):
    local_write(request)
    return await run_research(services.agent_research.answer, identity, question_id,
                             answer.text, answer.revision, answer.confirmed)


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
