"""All views share one accepted minute and VWAP admission; temporary data only."""
from copy import deepcopy
from datetime import datetime
from unittest.mock import AsyncMock
import pytest
from console.schemas import LimitStopOrderRequest
from console.services.rhythm import MarketRhythmReader
from execution.entry_admission import candidate_snapshot,check_candidate
from .test_ranking_snapshot import raw,put
from .test_candidate_entry_admission import NOW,STAMP,SYM,market,frame
from .test_risk_exposure import make_mgr
from .conftest import run


def three_views():
    data=raw();one=deepcopy(data['series'][SYM]);symbols=[f'NASDAQ:L{i:02}' for i in range(12)]+[f'NASDAQ:S{i:02}' for i in range(12)]
    data['series']={s:{**deepcopy(one),'symbol':s} for s in symbols}
    rows=[]
    for i,s in enumerate(symbols):
        sign=1 if i<12 else -1;value=sign*(12-i%12)
        rows.append({'symbol':s,'rank':0,'direction_sign':sign,'opening_position':value,'score':abs(value)})
    rows.sort(key=lambda r:(-r['score'],r['symbol']))
    def view(sign):return [{**r,'rank':i+1} for i,r in enumerate([r for r in rows if not sign or r['direction_sign']==sign][:10])]
    data['rankings']={'mixed':view(0),'long':view(1),'short':view(-1)};data['ranks']=data['rankings']['mixed']
    data['coverage']={'total':24,'aligned':24,'received':24,'missing':[]};data['scoring'].update(ready=24,eligible=24)
    data['beta']=[];return data


def test_side_view_outside_mixed_top10_is_authorized_only_with_its_frozen_source_and_vwap_direction(tmp_path):
    path=tmp_path/'rhythm.json';put(path,three_views());reader=MarketRhythmReader(path);display=reader.read(now=NOW,latest_only=True)
    target=display['rankings']['long'][7]['symbol']
    assert target not in [r['symbol'] for r in display['ranks']]
    prices={target:deepcopy(market(-1)[SYM])}
    mixed=candidate_snapshot(display,NOW,prices)
    assert not check_candidate(mixed,target,'short')['ok']
    long=candidate_snapshot(display,NOW,prices,view='long')
    assert long['ranking_snapshot']==mixed['ranking_snapshot']
    result=check_candidate(long,target,'short')
    assert result['ok'] and result['evidence']['ranking_direction']==1 and result['evidence']['direction']==-1
    assert result['evidence']['ranking_evidence']['ranking_view']=='long'
    assert not check_candidate(long,target,'long')['ok']
    assert not candidate_snapshot(display,NOW,prices,view='unknown')['ready']


def test_partial_minute_retains_all_three_views_and_same_snapshot_id(tmp_path):
    path=tmp_path/'rhythm.json';data=three_views();put(path,data);reader=MarketRhythmReader(path)
    before=reader.read(now=NOW);changed=deepcopy(data);changed['as_of']+=60;changed['coverage']['aligned']-=1;changed['coverage']['missing']=['NASDAQ:L11']
    put(path,changed);after=reader.read(now=NOW+60)
    assert after['ranking_snapshot']==before['ranking_snapshot'] and after['rankings']==before['rankings']
    assert after['batch_progress']['waiting']


@pytest.mark.parametrize('view',['mixed','long','short'])
def test_before_0945_is_a_hard_entry_boundary_even_with_a_claimed_complete_board(view):
    early=datetime.fromisoformat('2026-11-19T09:44:59-05:00').timestamp()
    data=frame();result=check_candidate(candidate_snapshot(data,early,market(now=early),view=view),SYM,'long')
    assert not result['ok'] and '09:45' in result['reason']
    assert candidate_snapshot(data,NOW,market(),view='mixed')['ready']


@pytest.mark.parametrize('manual',[False,True])
def test_order_manager_rechecks_the_first_fifteen_minutes_without_sending_a_parent_or_stop(tmp_path,manual):
    early=datetime.fromisoformat('2026-11-19T09:44:59-05:00').timestamp()
    mgr=make_mgr(tmp_path);mgr._gw.place_market_with_stop=AsyncMock(return_value={})
    mgr.entry_admission=lambda symbol,side,now:check_candidate(candidate_snapshot(frame(),early,market(now=early)),symbol,side)
    lot,reason=run(mgr.place_limit_stop_lot(SYM,'long',1,100,99,entry_order_type='MKT',manual_entry=manual))
    assert lot is None and '09:45' in reason;mgr._gw.place_market_with_stop.assert_not_awaited()


def test_direction_view_is_passed_through_final_preflight_and_cannot_be_attached_to_an_auto_order(tmp_path):
    mgr=make_mgr(tmp_path);mgr._gw.place_market_with_stop=AsyncMock(return_value={});calls=[]
    def admission(symbol,side,now,**kwargs):
        calls.append(kwargs);return {'ok':False,'reason':'isolated source captured'}
    mgr.entry_admission=admission
    lot,reason=run(mgr.place_limit_stop_lot(SYM,'long',1,100,99,entry_order_type='MKT',manual_entry=True,signal_snapshot={'candidate_view':'long'}))
    assert lot is None and calls==[{'view':'long'}]
    calls.clear()
    lot,reason=run(mgr.place_limit_stop_lot(SYM,'long',1,100,99,entry_order_type='MKT',manual_entry=False,signal_snapshot={'candidate_view':'long'}))
    assert lot is None and not calls and '仅用于手动' in reason
    assert LimitStopOrderRequest(symbol=SYM,limit_px=100,stop_px=99,candidate_view='short').candidate_view=='short'


def test_forged_or_inconsistent_direction_views_cannot_be_accepted(tmp_path):
    path=tmp_path/'rhythm.json';data=three_views();data['rankings']['long'][0]['direction_sign']=-1;put(path,data)
    result=MarketRhythmReader(path).read(now=NOW)
    assert result['status']=='error' and not result['ranks']
