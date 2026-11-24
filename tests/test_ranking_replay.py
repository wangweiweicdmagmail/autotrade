"""Historical candle caching/replay never boots the application or opens a broker."""
import asyncio
from datetime import datetime
import pytest
from engine.ranking_replay import cache_day,cached_input,replay_inputs,rank_at,day_bounds
from engine.research_store import ResearchStore
from engine.research_calendar import ET
from .conftest import run

DAY='2026-10-07';PLAN,START,END=day_bounds(DAY);OPEN=PLAN['regular_open'];CLOSE=PLAN['regular_close'];SYMBOL='NASDAQ:TEST'

def dataset(interval):
    seconds=60 if interval=='1m' else 300
    warm=4 if interval=='1m' else 30
    prev=OPEN-63000
    stamps=[prev-seconds*(warm-i) for i in range(warm)]+list(range(OPEN,CLOSE,seconds))
    return {'metadata':{'timezone':'America/New_York','minmove':1,'pricescale':100,'type':'stock','resolved_symbol':SYMBOL},
        'bars':[{'time':t,'open':100+i*.01,'high':101+i*.01,'low':99+i*.01,'close':100+i*.01,'volume':1000} for i,t in enumerate(stamps)]}


def test_cache_is_database_first_idempotent_and_never_fetches_again_when_complete(tmp_path):
    store=ResearchStore(tmp_path/'research.sqlite3');calls=[]
    async def fetch(series,end,count,credentials):
        calls.append(series['interval']);return dataset(series['interval'])
    first=run(cache_day(store,DAY,[SYMBOL],set(),tmp_path/'unused',fetch=fetch,now=END+1))
    assert first['complete'] and sorted(calls)==['1m','5m']
    for interval,expected in [('1m',390),('5m',78)]:
        meta,bars,state=cached_input(store,SYMBOL,interval,'regular',DAY)
        assert state['complete'] and state['rows']==expected and state['warmup']>0
        assert meta['session']=='regular' and meta['adjustment']=='splits'
        assert all(b['observed_at']==END+1 for b in bars)
    again=run(cache_day(store,DAY,[SYMBOL],set(),tmp_path/'unused',fetch=fetch,now=END+2))
    assert all(r['status']=='cached' for r in again['rows']);assert len(calls)==2
    assert len(store.series(SYMBOL))==2


def test_asof_inputs_use_only_closed_bars_and_keep_whole_day_in_database(tmp_path):
    store=ResearchStore(tmp_path/'research.sqlite3')
    async def fetch(series,*args):return dataset(series['interval'])
    run(cache_day(store,DAY,[SYMBOL],set(),tmp_path/'unused',fetch=fetch,now=END+1))
    at=OPEN+600
    inputs=replay_inputs(store,DAY,at,[SYMBOL],set());data=inputs['datasets'][SYMBOL]
    assert len(data['bars'])==14 and len(data['five_bars'])==32
    assert data['bars'][-1]['time']==OPEN+540 and data['five_bars'][-1]['time']==OPEN+300
    assert len(cached_input(store,SYMBOL,'1m','regular',DAY)[1])==394
    assert inputs['sources'][SYMBOL]['input_digest']
    result=run(rank_at(store,DAY,'09:40',[SYMBOL],set()))
    assert result['as_of']==at and result['scoring']['model']=='opening15_midpoint_v7'
    assert not result['ranks'] and not result['complete']
    assert result['scoring']['pending'][0]['reason']=='opening_range_forming'
    ready=run(rank_at(store,DAY,'09:50',[SYMBOL],set()))
    assert ready['complete'] and ready['ranks'][0]['opening_position']>0
    assert ready['ranks'][0]['range_confirmed_at']==OPEN+900
    assert result['mode']=='latest_research_reconstruction' and result['experiment_id']
    assert result['ranking_source_sha256']
    with store.read() as db:
        assert db.execute("SELECT COUNT(*) FROM ranking_batches").fetchone()[0]==0
        assert db.execute("SELECT COUNT(*) FROM definition_versions WHERE payload LIKE '%ranking_experiment%'").fetchone()[0]==2


def test_missing_minute_is_reported_and_retry_only_refetches_the_incomplete_interval(tmp_path):
    store=ResearchStore(tmp_path/'research.sqlite3');calls=[]
    async def incomplete(series,*args):
        result=dataset(series['interval']);calls.append(series['interval'])
        if series['interval']=='1m':result['bars']=[b for b in result['bars'] if b['time']!=OPEN+60]
        return result
    first=run(cache_day(store,DAY,[SYMBOL],set(),tmp_path/'unused',fetch=incomplete,now=END+1))
    assert not first['complete'];assert next(r for r in first['rows'] if r['interval']=='1m')['missing']==[OPEN+60]
    async def fixed(series,*args):calls.append(series['interval']);return dataset(series['interval'])
    second=run(cache_day(store,DAY,[SYMBOL],set(),tmp_path/'unused',fetch=fixed,now=END+2))
    assert second['complete'];assert calls.count('1m')==2 and calls.count('5m')==1


def test_unavailable_history_is_explicit_and_cannot_become_empty_valid_data(tmp_path):
    store=ResearchStore(tmp_path/'research.sqlite3')
    async def unavailable(*args):raise ValueError('isolated source unavailable')
    result=run(cache_day(store,DAY,[SYMBOL],set(),tmp_path/'unused',fetch=unavailable,now=END+1))
    assert not result['complete'] and all(r['status']=='failed' for r in result['rows'])
    inputs=replay_inputs(store,DAY,OPEN+600,[SYMBOL],set())
    assert not inputs['datasets'] and inputs['missing'][0]['symbol']==SYMBOL


def test_replay_rejects_unclosed_days_and_outside_session(tmp_path):
    store=ResearchStore(tmp_path/'research.sqlite3')
    with pytest.raises(ValueError):run(cache_day(store,DAY,[SYMBOL],set(),tmp_path/'unused',now=OPEN))
    with pytest.raises(ValueError):replay_inputs(store,DAY,OPEN,[SYMBOL],set())
    with pytest.raises(ValueError):day_bounds('2026-10-10')


def test_an_old_available_minute_is_never_labeled_as_the_requested_later_snapshot(tmp_path):
    store=ResearchStore(tmp_path/'research.sqlite3')
    async def fetch(series,*args):
        data=dataset(series['interval'])
        if series['interval']=='1m':data['bars']=[b for b in data['bars'] if b['time']<OPEN+540]
        return data
    run(cache_day(store,DAY,[SYMBOL],set(),tmp_path/'unused',fetch=fetch,now=END+1))
    result=run(rank_at(store,DAY,'09:40',[SYMBOL],set()))
    assert result['as_of']==OPEN+600 and result['source_as_of']==OPEN+540
    assert not result['ranks'] and not result['complete']
    assert any(e['reason']=='requested_minute_unavailable' for e in result['errors'])


def test_new_position_ranking_does_not_require_five_minute_ema_or_even_a_five_minute_series(tmp_path):
    from engine.research_archive import envelope
    store=ResearchStore(tmp_path/'research.sqlite3');data=dataset('1m')
    series={'provider':'tradingview','symbol':SYMBOL,'resolved_symbol':SYMBOL,'interval':'1m','session':'regular','adjustment':'splits',
            'timezone':'America/New_York','minmove':1,'pricescale':100,'type':'stock'}
    rows=[{**b,'close_time':b['time']+60} for b in data['bars']]
    store.apply(envelope('bars',{'series':series,'rows':rows,'received_at':END+1},observed_at=END+1))
    result=run(rank_at(store,DAY,'09:50',[SYMBOL],set()))
    assert result['complete'] and result['ranks'][0]['opening_position']>0
    assert result['sources'][SYMBOL]['five_rows']==0 and len(result['sources'][SYMBOL]['series_ids'])==1
    assert result['rankings']['long'][0]['symbol']==SYMBOL
