"""Confirmed ranking admission and all-entry enforcement, using temporary state."""
import json
from datetime import datetime
from unittest.mock import AsyncMock
import pytest
from execution.entry_admission import candidate_snapshot as build_candidate_snapshot, check_candidate, ema9_entry_state
from console.runtime.bootstrap import build_services, RuntimePaths
from .test_risk_exposure import make_mgr
from .test_pine_execution import fixture_model, fixture_output, SYM
from .test_pyramid_safety import StateBroker
from .conftest import run

NOW=datetime.fromisoformat('2026-11-19T15:15:05+00:00').timestamp()
STAMP=NOW-5


def frame():
    return {'status':'ready','day':'2026-11-19','as_of':STAMP,
            'ranks':[{'symbol':SYM,'rank':1,'direction_sign':1}],
            'tracks':[{'symbol':SYM,'aligned':True,'td':{'version':1,'comparison_bars':4,'trend_threshold':3,'counts':[[STAMP-60,1]]},'points':[[STAMP-60,100,99,2,100,101,99,100,2,'outside_progress',1]]}]}


def market(direction=1):
    return {SYM:{'bar':{'time':STAMP,'open':100,'high':101,'low':99,'close':100},
                 'ts':{'time':STAMP,'ema_9':99 if direction==1 else 101},
                 'e9':{'ema_9':200},'price_observed_at':NOW}}


def candidate_snapshot(data, now, market_data=None):
    direction=(data.get('ranks') or [{}])[0].get('direction_sign',1)
    return build_candidate_snapshot(data,now,market(direction) if market_data is None else market_data)


@pytest.mark.parametrize('fault',['stale','future','day','missing','gap','alignment','direction','point','rank'])
def test_missing_or_mismatched_confirmed_ranking_never_grants_entry(fault):
    data=frame()
    if fault=='stale':data['as_of']-=121
    elif fault=='future':data['as_of']=NOW+1
    elif fault=='day':data['day']='2026-11-18'
    elif fault=='missing':data['status']='waiting'
    elif fault=='gap':data['tracks'][0]['issue']='minute_gap'
    elif fault=='alignment':data['tracks'][0]['aligned']=False
    elif fault=='direction':data['ranks'][0]['direction_sign']=-1
    elif fault=='point':data['tracks'][0]['points'][0][0]-=60
    elif fault=='rank':data['ranks'][0]['rank']=11
    assert not check_candidate(candidate_snapshot(data,NOW),SYM,'long')['ok']


def test_only_top_ten_and_matching_trend_are_allowed():
    snapshot=candidate_snapshot(frame(),NOW)
    assert check_candidate(snapshot,SYM,'long')['ok']
    assert check_candidate(snapshot,'NVDA','long')['ok']
    assert not check_candidate(snapshot,SYM,'short')['ok']
    assert not check_candidate(snapshot,'NASDAQ:MSFT','long')['ok']


def test_production_runtime_always_injects_the_shared_candidate_policy(tmp_path):
    app=build_services(RuntimePaths(tmp_path))
    try:
        assert app.order_mgr.entry_admission.__self__ is app.research
        assert not app.order_mgr.entry_admission(SYM,'long',NOW)['ok']
    finally:app.close_log_handler()


def test_production_policy_reads_the_confirmed_cache_and_expires_without_rewriting_it(tmp_path):
    app=build_services(RuntimePaths(tmp_path))
    try:
        app.collector._snapshot=market()
        data=frame();row={**data['tracks'][0],'last_closed':STAMP-60}
        raw={'version':1,'as_of':STAMP,'day':data['day'],'columns':['time'],'beta':[],
             'scoring':{'model':'ema9_15m_v2','version':2,'pending':[],'ready':1,'eligible':1},
             'ranks':data['ranks'],'series':{SYM:row},'coverage':{}}
        raw['ranks'][0].update(score=88,score_parts={'vwap':28,'slope':40,'classification':20},
                               classification_rate=1,classification_samples=15,ema9_slope_atr=.5)
        path=tmp_path/'market_rhythm.json';path.write_text(json.dumps(raw));saved=path.read_bytes()
        allowed=app.order_mgr.entry_admission(SYM,'long',NOW)
        assert allowed['ok'] and allowed['evidence']['sigma']==2
        evidence=allowed['evidence']['ranking_evidence']
        assert evidence['model']=='ema9_15m_v2' and evidence['score']==88
        assert evidence['classification_samples']==15 and evidence['score_parts']['classification']==20
        assert not app.order_mgr.entry_admission(SYM,'short',NOW)['ok']
        assert not app.order_mgr.entry_admission(SYM,'long',NOW+121)['ok']
        assert path.read_bytes()==saved
    finally:app.close_log_handler()


def test_pending_scoring_batch_cannot_grant_new_entry_from_a_partial_top_ten(tmp_path):
    app=build_services(RuntimePaths(tmp_path))
    try:
        data=frame();row={**data['tracks'][0],'last_closed':STAMP-60}
        raw={'version':1,'as_of':STAMP,'day':data['day'],'columns':['time'],'beta':[],
             'scoring':{'model':'ema9_15m_v2','version':2,'pending':[{'symbol':'NASDAQ:MSFT'}],'ready':1,'eligible':2},
             'ranks':data['ranks'],'series':{SYM:row},'coverage':{}}
        (tmp_path/'market_rhythm.json').write_text(json.dumps(raw));app.collector._snapshot=market()
        assert not app.order_mgr.entry_admission(SYM,'long',NOW)['ok']
    finally:app.close_log_handler()


@pytest.mark.parametrize('strategy',[None,'ema7_autotrade_v2','golden_strategy','opening_vwap_3'])
def test_manual_automatic_and_signal_orders_share_the_same_admission(tmp_path,strategy):
    mgr=make_mgr(tmp_path)
    gateway=AsyncMock(return_value={'ok':True,'dry_run':True,'entry_order_id':1,'stop_order_id':2})
    mgr._gw.place_market_with_stop=gateway
    current={'allow':False}
    def permission(symbol,side,now):
        if not current['allow']:return {'ok':False,'reason':'候选入场限制：未入榜'}
        return check_candidate(candidate_snapshot(frame(),NOW),symbol,side)
    mgr.entry_admission=permission
    async def scenario():
        meta={'strategy_id':strategy} if strategy else {}
        kwargs=dict(symbol=SYM,side='long',qty=10,limit_px=100,stop_px=99,
                    entry_order_type='MKT',signal_snapshot=meta,manual_entry=not strategy)
        lot,reason=await mgr.place_limit_stop_lot(**kwargs)
        assert lot is None and '未入榜' in reason
        gateway.assert_not_awaited()
        current['allow']=True
        lot,reason=await mgr.place_limit_stop_lot(**kwargs)
        assert lot is not None,reason
        assert lot.stop_px==99  # Automatic and signal protective prices are preserved.
        assert lot.signal_snapshot['candidate_admission']['rank']==1
        assert lot.signal_snapshot['candidate_admission']['td_count']==1
        assert gateway.await_count==1
    run(scenario())


@pytest.mark.parametrize('stage',['risk_wait','after_intent'])
def test_candidate_change_before_broker_submission_blocks_without_unknown_acceptance(tmp_path,stage):
    mgr=make_mgr(tmp_path);allowed=[True]
    mgr._gw.place_market_with_stop=AsyncMock(return_value={})
    mgr.entry_admission=lambda *args:({'ok':True,'evidence':{'rank':1}} if allowed[0]
                                     else {'ok':False,'reason':'候选入场限制：排名已变化'})
    if stage=='risk_wait':
        async def pnl():allowed[0]=False;return 0
        mgr._gw.realized_pnl=pnl
    else:
        save=mgr._save
        def changed():save();allowed[0]=False
        mgr._save=changed
    lot,reason=run(mgr.place_limit_stop_lot(SYM,'long',10,100,99,entry_order_type='MKT'))
    assert lot is None and '排名已变化' in reason
    mgr._gw.place_market_with_stop.assert_not_awaited()
    assert all(not row.entry_submission_pending for row in mgr._lots.values())


def test_temporary_admission_rejection_does_not_consume_a_pine_signal_bar():
    from domain.contracts import EntryAdmissionRejected
    from engine.pine_execution import PineExecutionEngine
    class Filtered(StateBroker):
        blocked=True
        async def place_limit_stop(self,**kwargs):
            if self.blocked:raise EntryAdmissionRejected('候选入场限制：暂未入榜')
            return await super().place_limit_stop(**kwargs)
    broker=Filtered();engine=PineExecutionEngine(broker,model=fixture_model())
    engine.start([SYM],100,{SYM:1})
    run(engine.on_price(SYM,100,fixture_output(),600))
    assert not broker.calls
    broker.blocked=False
    run(engine.on_price(SYM,100,fixture_output(),600))
    assert len(broker.calls)==1


@pytest.mark.parametrize('direction,td,allowed',[(1,0,False),(1,1,True),(1,2,True),(1,3,True),(1,-1,False),(-1,0,False),(-1,-1,True),(-1,-2,True),(-1,-3,True),(-1,1,False)])
def test_confirmed_td_must_reach_the_same_side_threshold(direction,td,allowed):
    data=frame();data['ranks'][0]['direction_sign']=direction
    data['tracks'][0]['points'][0][10]=direction;data['tracks'][0]['td']['counts'][0][1]=td
    data['tracks'][0]['points'][0][2]=99 if direction==1 else 101
    snapshot=candidate_snapshot(data,NOW)
    assert len(snapshot['rows'])==1  # Below-threshold candidates remain visible.
    result=check_candidate(snapshot,SYM,'long' if direction==1 else 'short')
    assert result['ok'] is allowed
    if allowed:assert result['evidence']['td_count']==td
    else:assert 'TD' in result['reason']


@pytest.mark.parametrize('fault',['missing','old','future','duplicate','schema','bool','string','fraction','nonfinite'])
def test_invalid_or_wrong_minute_td_cannot_borrow_a_previous_qualifying_count(fault):
    data=frame();td=data['tracks'][0]['td']
    if fault=='missing':data['tracks'][0].pop('td')
    elif fault=='old':td['counts']=[[STAMP-120,8]]
    elif fault=='future':td['counts']=[[STAMP,8]]
    elif fault=='duplicate':td['counts'].append([STAMP-60,4])
    elif fault=='schema':td['comparison_bars']=3
    elif fault=='bool':td['counts'][0][1]=True
    elif fault=='string':td['counts'][0][1]='3'
    elif fault=='fraction':td['counts'][0][1]=3.5
    else:td['counts'][0][1]=float('nan')
    assert not check_candidate(candidate_snapshot(data,NOW),SYM,'long')['ok']


@pytest.mark.parametrize('stage',['risk_wait','after_intent'])
def test_td_dropping_below_threshold_before_submission_prevents_any_broker_order(tmp_path,stage):
    mgr=make_mgr(tmp_path);data=frame()
    mgr.entry_admission=lambda symbol,side,now:check_candidate(candidate_snapshot(data,NOW),symbol,side)
    mgr._gw.place_market_with_stop=AsyncMock(return_value={})
    def drop():data['tracks'][0]['td']['counts'][0][1]=0
    if stage=='risk_wait':
        async def pnl():drop();return 0
        mgr._gw.realized_pnl=pnl
    else:
        save=mgr._save
        def changed():save();drop()
        mgr._save=changed
    lot,reason=run(mgr.place_limit_stop_lot(SYM,'long',10,100,99,entry_order_type='MKT'))
    assert lot is None and 'TD' in reason
    mgr._gw.place_market_with_stop.assert_not_awaited()
    assert all(not row.entry_submission_pending for row in mgr._lots.values())


@pytest.mark.parametrize('side,ema,allowed',[('long',99,True),('long',100,False),('long',101,False),
                                          ('short',101,True),('short',100,False),('short',99,False)])
def test_five_minute_ema9_requires_price_strictly_on_the_entry_side(side,ema,allowed):
    data=market();data[SYM]['ts']['ema_9']=ema
    result=ema9_entry_state(data[SYM],side,NOW)
    assert result['ema9_allowed'] is allowed
    assert result['ema9_5m']==ema and result['ema9_price']==100


@pytest.mark.parametrize('fault',['missing','stale_price','future_price','stale_bar','non_five_minute','misaligned',
                                 'bad_ema','bool_ema','string_ema','zero_price','legacy_ema7_only'])
def test_ema9_missing_stale_or_mixed_bar_data_cannot_allow_entry(fault):
    data=market();row=data[SYM]
    if fault=='missing':data={}
    elif fault=='stale_price':row['price_observed_at']=NOW-46
    elif fault=='future_price':row['price_observed_at']=NOW+1
    elif fault=='stale_bar':row['bar']['time']-=300
    elif fault=='non_five_minute':row['bar']['time']-=60;row['ts']['time']-=60
    elif fault=='misaligned':row['ts']['time']-=300
    elif fault=='bad_ema':row['ts']['ema_9']=float('nan')
    elif fault=='bool_ema':row['ts']['ema_9']=True
    elif fault=='string_ema':row['ts']['ema_9']='99'
    elif fault=='zero_price':row['bar']['close']=0
    else:row['ts'].pop('ema_9')
    result=check_candidate(candidate_snapshot(frame(),NOW,data),SYM,'long')
    assert not result['ok'] and 'EMA9' in result['reason']


def test_td_coloring_metadata_keeps_three_while_entry_threshold_is_one():
    data=frame();snapshot=candidate_snapshot(data,NOW)
    assert snapshot['td_threshold']==1
    assert check_candidate(snapshot,SYM,'long')['ok']
    data['tracks'][0]['td']['trend_threshold']=1
    assert not check_candidate(candidate_snapshot(data,NOW),SYM,'long')['ok']


def test_status_advertises_the_same_entry_threshold_and_ema9_contract(console_app):
    from starlette.testclient import TestClient
    data=TestClient(console_app.app).get('/api/status').json()
    assert data['candidate_td_threshold']==1
    assert data['candidate_ema9_filter']=={'period':9,'interval_seconds':300,'price_source':'bar.close','ema_source':'ts.ema_9'}


@pytest.mark.parametrize('stage',['risk_wait','after_intent'])
def test_crossing_ema9_before_broker_submission_blocks_without_sending(tmp_path,stage):
    mgr=make_mgr(tmp_path);data=market()
    mgr.entry_admission=lambda symbol,side,now:check_candidate(candidate_snapshot(frame(),NOW,data),symbol,side)
    mgr._gw.place_market_with_stop=AsyncMock(return_value={})
    def cross():data[SYM]['ts']['ema_9']=101
    if stage=='risk_wait':
        async def pnl():cross();return 0
        mgr._gw.realized_pnl=pnl
    else:
        save=mgr._save
        def changed():save();cross()
        mgr._save=changed
    lot,reason=run(mgr.place_limit_stop_lot(SYM,'long',10,100,99,entry_order_type='MKT'))
    assert lot is None and 'EMA9' in reason
    mgr._gw.place_market_with_stop.assert_not_awaited()
    assert all(not row.entry_submission_pending for row in mgr._lots.values())


@pytest.mark.parametrize('direction,vwap,allowed',[(1,99,True),(1,100,False),(1,101,False),
                                                (-1,101,True),(-1,100,False),(-1,99,False),
                                                (1,None,False),(1,float('nan'),False),(1,True,False)])
def test_vwap_midline_is_mandatory_for_all_entries_even_without_the_optional_outer_band_filter(direction,vwap,allowed):
    data=frame();data['ranks'][0]['direction_sign']=direction
    data['tracks'][0]['points'][0][10]=direction;data['tracks'][0]['points'][0][2]=vwap
    data['tracks'][0]['td']['counts'][0][1]=direction
    result=check_candidate(candidate_snapshot(data,NOW),SYM,'long' if direction==1 else 'short')
    assert result['ok'] is allowed
    if allowed:assert result['evidence']['vwap_direction_allowed']
    else:assert 'VWAP' in result['reason']


@pytest.mark.parametrize('stage',['risk_wait','after_intent'])
def test_vwap_midline_crossing_before_submission_never_reaches_the_broker(tmp_path,stage):
    mgr=make_mgr(tmp_path);data=frame();live=market()
    mgr.entry_admission=lambda symbol,side,now:check_candidate(candidate_snapshot(data,NOW,live),symbol,side)
    mgr._gw.place_market_with_stop=AsyncMock(return_value={})
    def cross():data['tracks'][0]['points'][0][2]=101
    if stage=='risk_wait':
        async def pnl():cross();return 0
        mgr._gw.realized_pnl=pnl
    else:
        save=mgr._save
        def changed():save();cross()
        mgr._save=changed
    lot,reason=run(mgr.place_limit_stop_lot(SYM,'long',10,100,99,entry_order_type='MKT'))
    assert lot is None and 'VWAP' in reason
    mgr._gw.place_market_with_stop.assert_not_awaited()
    assert all(not row.entry_submission_pending for row in mgr._lots.values())
