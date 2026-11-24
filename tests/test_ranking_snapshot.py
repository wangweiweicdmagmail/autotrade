"""Accepted-minute invariants. Temporary caches and broker stubs only."""
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
import json
from unittest.mock import AsyncMock

import pytest

from console.services.rhythm import MarketRhythmReader
from execution.entry_admission import candidate_snapshot, check_candidate
from .test_candidate_entry_admission import NOW, STAMP, SYM, market, beta_tracks, BETA_SYMBOLS
from .test_risk_exposure import make_mgr
from .conftest import run

OTHER = 'NASDAQ:OTHER'
UNIVERSE = [SYM,OTHER,*BETA_SYMBOLS]


def raw(at=STAMP, *, ranked=SYM, complete=True, pending=False, with_beta=True):
    def row(symbol, time):
        return {'symbol':symbol, 'last_closed':time-60, 'events':[], 'td':{
            'version':1,'comparison_bars':4,'trend_threshold':3,'counts':[[time-60,1]]},
            'points':[[time-60,100,99,2,100,101,99,100,2,'outside_progress',1]]}
    return {'version':1,'day':'2026-11-19','as_of':at,'generated_at':at+3,
        'columns':['time','ema3','vwap','sigma','open','high','low','close','z','phase','direction'],
        'beta':list(BETA_SYMBOLS) if with_beta else [],'coverage':{'total':4 if with_beta else 2,'received':4 if with_beta else 2,'aligned':(4 if complete else 3) if with_beta else (2 if complete else 1),
                              'missing':[] if complete else [OTHER]},
        'scoring':{'model':'opening15_midpoint_v7','version':7,'stock_session':'regular','pending':[{'symbol':OTHER,'reason':'ATR'}] if pending else [],
                   'ready':1,'eligible':1},
        'ranks':[{'symbol':ranked,'rank':1,'direction_sign':1,'opening_position':2,'score':88,'score_parts':{'vwap':24,'slope':44,'classification':20}}],
        'series':{SYM:row(SYM,at),OTHER:row(OTHER,at if complete else at-60),
                  **({r['symbol']:r for r in beta_tracks(at)} if with_beta else {})}}


def put(path, frame):
    temporary=path.with_suffix('.tmp')
    temporary.write_text(json.dumps(frame))
    temporary.replace(path)


def admission(reader, symbol, now=NOW):
    live=market(now=now)
    return check_candidate(candidate_snapshot(reader.read(now=now,latest_only=True),now,live),symbol,'long')


def test_partial_batches_are_never_the_first_accepted_minute(tmp_path):
    path=tmp_path/'rhythm.json';put(path,raw(complete=False))
    reader=MarketRhythmReader(path,symbols=UNIVERSE)
    assert reader.read(now=NOW)['status']=='waiting'
    assert not admission(reader,SYM)['ok']
    assert not reader.accepted_path.exists()
    # A raw ready flag cannot bypass either the complete batch or snapshot contract.
    frame={'status':'ready','day':'2026-11-19','as_of':STAMP,'coverage':{'total':2,'aligned':1,'missing':[OTHER]}}
    assert not candidate_snapshot(frame,NOW,market())['ready']


def test_api_and_admission_share_old_snapshot_until_next_complete_minute(console_app):
    from starlette.testclient import TestClient
    reader=console_app.research._market_rhythm;reader.expected_symbols=set(UNIVERSE)
    put(reader.path,raw())
    displayed=TestClient(console_app.app).get('/api/market-rhythm').json()
    first_id=displayed['ranking_snapshot']['id']
    console_app.collector._snapshot=market()
    allowed=console_app.research.check_candidate_entry(SYM,'long',NOW)
    assert allowed['ok'] and allowed['evidence']['ranking_evidence']['snapshot_id']==first_id
    put(reader.path,raw(STAMP+60,ranked=OTHER,complete=False))
    retained=run(console_app.research.api_market_rhythm())
    assert retained['ranking_snapshot']==displayed['ranking_snapshot']
    assert retained['ranks']==displayed['ranks'] and retained['batch_progress']['waiting']
    assert retained['batch_progress']['coverage']['aligned']==3
    console_app.collector._snapshot=market(now=NOW+60)
    allowed=console_app.research.check_candidate_entry(SYM,'long',NOW+60)
    assert allowed['ok'] and allowed['evidence']['ranking_evidence']['snapshot_id']==first_id
    assert not console_app.research.check_candidate_entry(OTHER,'long',NOW+60)['ok']
    put(reader.path,raw(STAMP+60,ranked=OTHER))
    current=run(console_app.research.api_market_rhythm())
    assert current['ranking_snapshot']['id']!=first_id and current['ranks'][0]['symbol']==OTHER
    assert not console_app.research.check_candidate_entry(SYM,'long',NOW+60)['ok']


def test_same_minute_revision_freezes_rank_direction_score_td_and_tracks(tmp_path):
    path=tmp_path/'rhythm.json';put(path,raw())
    reader=MarketRhythmReader(path);before=reader.read(now=NOW)
    revised=raw(ranked=OTHER);revised['ranks'][0].update(score=99,direction_sign=-1)
    revised['series'][SYM]['points'][0][7]=999
    revised['series'][SYM]['td']['counts'][0][1]=-7
    put(path,revised);after=reader.read(now=NOW+1)
    for key in ('ranking_snapshot','ranks','tracks','as_of','revision','scoring'):
        assert after[key]==before[key]
    assert admission(reader,SYM)['ok']
    assert MarketRhythmReader(path).read(now=NOW+1)['ranks']==before['ranks']


def test_restart_restores_previous_batch_during_partial_scoring_and_missing_cache(tmp_path):
    path=tmp_path/'rhythm.json';put(path,raw())
    reader=MarketRhythmReader(path);old=reader.read(now=NOW)
    put(path,raw(STAMP+60,ranked=OTHER,pending=True))
    restarted=MarketRhythmReader(path)
    assert restarted.read(now=NOW+60)['ranking_snapshot']==old['ranking_snapshot']
    assert admission(restarted,SYM,NOW+60)['ok']
    assert admission(restarted,SYM,NOW+121)['ok']  # Fresh selected/index facts, unchanged complete membership.
    path.unlink()
    assert MarketRhythmReader(path).read(now=NOW+60)['ranking_snapshot']==old['ranking_snapshot']


def test_previous_day_snapshot_stays_visible_but_never_grants_new_day_entry(tmp_path):
    path=tmp_path/'rhythm.json';put(path,raw())
    reader=MarketRhythmReader(path);before=reader.read(now=NOW)
    later=raw(STAMP+86400,complete=False);later['day']='2026-11-20';put(path,later)
    current=reader.read(now=NOW+86400)
    assert current['ranking_snapshot']==before['ranking_snapshot']
    assert not admission(reader,SYM,NOW+86400)['ok']


@pytest.mark.parametrize('fault',['claimed_complete','universe','nonminute','missing_coverage','boolean'])
def test_batch_metadata_cannot_misrepresent_full_coverage(tmp_path,fault):
    path=tmp_path/'rhythm.json';frame=raw()
    if fault=='claimed_complete':frame['series'][OTHER]['last_closed']-=60
    elif fault=='universe':del frame['series'][OTHER];frame['coverage'].update(total=1,aligned=1)
    elif fault=='nonminute':frame['as_of']+=1
    elif fault=='missing_coverage':frame['coverage']={}
    elif fault=='boolean':frame['coverage']['aligned']=True
    put(path,frame);reader=MarketRhythmReader(path,symbols=UNIVERSE)
    assert reader.read(now=NOW)['status']=='waiting'
    assert not admission(reader,SYM)['ok']


def test_empty_complete_top_ten_can_replace_previous_candidates(tmp_path):
    path=tmp_path/'rhythm.json';put(path,raw())
    reader=MarketRhythmReader(path);reader.read(now=NOW)
    empty=raw(STAMP+60);empty['ranks']=[];put(path,empty)
    assert reader.read(now=NOW+60)['ranks']==[]
    assert not admission(reader,SYM,NOW+60)['ok']


def test_snapshot_write_failure_and_corrupt_restart_do_not_publish_new_selection(tmp_path,monkeypatch):
    path=tmp_path/'rhythm.json';put(path,raw())
    reader=MarketRhythmReader(path);before=reader.read(now=NOW)
    original=reader.accepted_path.read_bytes()
    put(path,raw(STAMP+60,ranked=OTHER))
    import console.services.rhythm as module
    def fail(*args):raise OSError('disk full')
    with monkeypatch.context() as m:
        m.setattr(module,'atomic_write',fail)
        assert reader.read(now=NOW+60)['status']=='error'
        assert reader.accepted_path.read_bytes()==original
        assert reader._accepted_id==before['ranking_snapshot']['id']
    reader.accepted_path.write_text('broken')
    assert MarketRhythmReader(path).read(now=NOW+60)['status']=='error'
    assert reader.accepted_path.read_text()=='broken'


def test_returned_objects_and_parallel_reads_cannot_mutate_accepted_snapshot(tmp_path):
    path=tmp_path/'rhythm.json';put(path,raw())
    reader=MarketRhythmReader(path);old=reader.read(now=NOW)
    old['ranks'][0]['score']=0;old['tracks'][0]['points'][0][7]=999
    with ThreadPoolExecutor(max_workers=4) as pool:
        rows=list(pool.map(lambda _:reader.read(now=NOW,latest_only=True),range(12)))
    assert len({r['ranking_snapshot']['id'] for r in rows})==1
    assert all(r['ranks'][0]['score']==88 and r['tracks'][0]['points'][0][7]==100 for r in rows)


def test_temporary_source_loss_and_backward_frames_keep_the_snapshot_and_recover(tmp_path):
    path=tmp_path/'rhythm.json';put(path,raw())
    reader=MarketRhythmReader(path);first=reader.read(now=NOW)
    parked=tmp_path/'parked.json';path.rename(parked)
    assert reader.read(now=NOW)['batch_progress']['waiting']
    parked.rename(path) # Same mtime and size as before the temporary source loss.
    assert not reader.read(now=NOW)['batch_progress']['waiting']
    put(path,raw(STAMP-60,ranked=OTHER))
    result=reader.read(now=NOW)
    assert result['ranking_snapshot']==first['ranking_snapshot']
    assert result['batch_progress']['waiting']


@pytest.mark.parametrize('stage',['risk_wait','after_intent'])
@pytest.mark.parametrize('complete',[False,True])
def test_broker_preflight_rechecks_only_accepted_batches(tmp_path,stage,complete):
    path=tmp_path/'rhythm.json';put(path,raw())
    reader=MarketRhythmReader(path);first=reader.read(now=NOW)
    mgr=make_mgr(tmp_path/'manager')
    mgr._gw.place_market_with_stop=AsyncMock(return_value={'ok':True,'dry_run':True,'entry_order_id':1,'stop_order_id':2})
    mgr.entry_admission=lambda symbol,side,now:admission(reader,symbol,NOW+60)
    def change():put(path,raw(STAMP+60,ranked=OTHER,complete=complete))
    if stage=='risk_wait':
        async def pnl():change();return 0
        mgr._gw.realized_pnl=pnl
    else:
        save=mgr._save
        def changed():save();change()
        mgr._save=changed
    lot,reason=run(mgr.place_limit_stop_lot(SYM,'long',10,100,99,entry_order_type='MKT'))
    if complete:
        assert lot is None and '前十' in reason
        mgr._gw.place_market_with_stop.assert_not_awaited()
        assert all(not r.entry_submission_pending for r in mgr._lots.values())
    else:
        assert lot is not None,reason
        assert mgr._gw.place_market_with_stop.await_count==1
        assert lot.signal_snapshot['candidate_admission']['ranking_evidence']['snapshot_id']==first['ranking_snapshot']['id']
