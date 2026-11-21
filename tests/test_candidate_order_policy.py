"""Candidate card policies, in-memory broker and temporary SQLite only."""
import asyncio
from copy import deepcopy
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from console.schemas import CandidateOrderOptions, LimitStopOrderRequest
from execution.candidate_policy import POLICY_KEY, TIMER_RULE, breakeven_due_at, vwap_entry_check
from execution.order_lot import ACTIVE, CLOSING, PENDING
from .conftest import run
from .test_manual_market_direction import manual_market
from .test_order_lot import FakeGW, mk_mgr, mk_lot, attach, alive_trail

NOW = datetime(2026, 11, 20, 15, 20, 5, tzinfo=timezone.utc).timestamp()


def policy(**changes):
    return dict(CandidateOrderOptions().dict(), **changes)


def evidence(side='long', now=NOW, price=None):
    return {'side': side, 'vwap': 100, 'sigma': 2, 'confirmed_at': int(now)//60*60,
            'ema9_price': price if price is not None else 103 if side == 'long' else 97,
            'ema9_observed_at': now}


def protection_case(tmp_path, side='long', **options):
    gw = FakeGW(fills={})
    mgr = mk_mgr(gw, tmp_path, 'candidate')
    lot = mk_lot(state=ACTIVE, side=side, stop_px=95 if side == 'long' else 105)
    lot.entry_time = NOW-365
    lot.avg_fill = 100
    lot.filled_qty = 10
    lot.signal_snapshot = {POLICY_KEY: policy(**options)}
    attach(mgr, lot, trail=alive_trail(lot))
    data = {'bar': {'time': int(NOW)//300*300, 'close': 103 if side == 'long' else 97},
            'price_observed_at': NOW,
            'bar_c': {'time': int(NOW)//300*300-300},
            'ts_c': {'time': int(NOW)//300*300-300, 'ema_9': 101 if side == 'long' else 99}}
    data['market_1m']={'bar_c':{'time':int(NOW)//60*60-60}}
    return mgr, lot, gw, data


@pytest.mark.parametrize('side,price,allowed', [('long',103,True),('long',102,False),
    ('long',101,False),('long',97,False),('short',97,True),('short',98,False),('short',99,False),('short',103,False)])
def test_vwap_filter_is_strict_and_directional(side, price, allowed):
    assert vwap_entry_check(evidence(side, price=price),side,NOW)['ok'] == allowed


@pytest.mark.parametrize('field,value', [('sigma',0),('sigma',None),('vwap',None),
    ('ema9_observed_at',NOW-46),('ema9_observed_at',NOW+1),('confirmed_at',NOW-121),('confirmed_at',NOW+1)])
def test_vwap_missing_or_stale_data_never_bypasses_filter(field,value):
    row=evidence();row[field]=value
    assert not vwap_entry_check(row,'long',NOW)['ok']


@pytest.mark.parametrize('side', ['long','short'])
def test_timer_uses_filled_time_and_actual_average_without_reversing_a_better_stop(tmp_path,side):
    mgr,lot,gw,data=protection_case(tmp_path,side)
    lot.entry_time=NOW-300
    assert run(mgr.protect_candidate_order(lot.lot_id,data,now=NOW))['action']=='waiting'
    assert not gw.stop_px_calls
    data['price_observed_at']=NOW+55
    data['market_1m']['bar_c']['time']+=60
    result=run(mgr.protect_candidate_order(lot.lot_id,data,now=NOW+55))
    assert result['ok'] and result['sources']==['timed_breakeven']
    assert gw.stop_px_calls==[100] and lot.stop_px==100
    assert lot.signal_snapshot['candidate_order_protection']['timed_breakeven_done']
    run(mgr.protect_candidate_order(lot.lot_id,data,now=NOW+56))
    assert gw.stop_px_calls==[100] and not gw.close_calls and not gw.stop_only_calls


@pytest.mark.parametrize('side', ['long','short'])
def test_crossed_cost_at_expiry_uses_coordinated_market_exit_without_waiting(tmp_path,side):
    mgr,lot,gw,data=protection_case(tmp_path,side)
    data['bar']['close']=99 if side=='long' else 101
    async def go():
        result=await mgr.protect_candidate_order(lot.lot_id,data,now=NOW)
        assert result['ok'] and result['action']=='close_submitted'
        assert lot.state==CLOSING and lot.close_reason=='candidate_timed_breakeven_stop'
        assert len(gw.close_calls)==1 and gw.close_calls[0][2]==10
        assert gw.close_calls[0][-1].order.orderType=='MKT'
        assert not gw.stop_px_calls and not gw.stop_only_calls and not lot.breakeven_reached
        await mgr.protect_candidate_order(lot.lot_id,data,now=NOW+1)
        assert len(gw.close_calls)==1
        for t in mgr._watchers.values():t.cancel()
    run(go())


def test_unknown_timed_exit_acceptance_stays_closing_and_never_resubmits(tmp_path):
    mgr,lot,gw,data=protection_case(tmp_path)
    data['bar']['close']=99
    gw.place_close=AsyncMock(return_value={'ok':False,'error':'unknown acceptance','trade':None})
    result=run(mgr.protect_candidate_order(lot.lot_id,data,now=NOW))
    assert not result['ok'] and lot.state==CLOSING
    run(mgr.protect_candidate_order(lot.lot_id,data,now=NOW+1))
    gw.place_close.assert_awaited_once()
    assert not gw.stop_only_calls and not gw.stop_px_calls


@pytest.mark.parametrize('side', ['long','short'])
def test_timer_off_and_already_better_stops_are_preserved(tmp_path,side):
    mgr,lot,gw,data=protection_case(tmp_path,side,timed_breakeven_enabled=False)
    run(mgr.protect_candidate_order(lot.lot_id,data,now=NOW))
    assert not gw.stop_px_calls
    lot.signal_snapshot[POLICY_KEY]['timed_breakeven_enabled']=True
    lot.stop_px=101 if side=='long' else 99
    run(mgr.protect_candidate_order(lot.lot_id,data,now=NOW))
    assert not gw.stop_px_calls and lot.signal_snapshot['candidate_order_protection']['timed_breakeven_done']


@pytest.mark.parametrize('side', ['long','short'])
@pytest.mark.parametrize('source', ['ema9_5m','st_1m_10_3'])
def test_trailing_uses_confirmed_selected_reference_and_ratchets(tmp_path,side,source):
    mgr,lot,gw,data=protection_case(tmp_path,side,trailing_enabled=True,trailing_source=source)
    minute=int(NOW)//60*60-60
    data['market_1m']={'bar_c':{'time':minute}}
    data['candidate_st_1m_c']={'version':1,'time':minute,'price':101 if side=='long' else 99,
        'direction':1 if side=='long' else -1,'atr_period':10,'multiplier':3,'interval_seconds':60,'session':'regular'}
    data['ts']={'time':data['bar']['time'],'ema_9':999} # Never uses forming EMA.
    outcome=run(mgr.protect_candidate_order(lot.lot_id,data,now=NOW))
    assert outcome['ok'] and source in outcome['sources']
    assert gw.stop_px_calls==[101 if side=='long' else 99]
    data['ts_c']['ema_9']=98 if side=='long' else 102
    data['candidate_st_1m_c']['price']=98 if side=='long' else 102
    run(mgr.protect_candidate_order(lot.lot_id,data,now=NOW+1))
    assert len(gw.stop_px_calls)==1


@pytest.mark.parametrize('fault', ['st_factor','st_forming','ema_forming','stale','closing','pending',
    'partial_exit','protection_filled','protection_unknown','missing_protection','external','invalid_policy'])
def test_invalid_or_unsafe_trailing_never_places_modifies_or_exits(tmp_path,fault):
    source='st_1m_10_3' if fault.startswith('st_') else 'ema9_5m'
    mgr,lot,gw,data=protection_case(tmp_path,timed_breakeven_enabled=False,trailing_enabled=True,trailing_source=source)
    minute=int(NOW)//60*60-60
    data['market_1m']={'bar_c':{'time':minute}}
    row=data['candidate_st_1m_c']={'version':1,'time':minute,'price':101,'direction':1,
        'atr_period':10,'multiplier':3,'interval_seconds':60,'session':'regular'}
    if fault=='st_factor':row['multiplier']=4
    elif fault=='st_forming':row['time']+=60
    elif fault=='ema_forming':data['ts_c']['time']+=300
    elif fault=='stale':data['price_observed_at']-=46
    elif fault=='closing':lot.state=CLOSING
    elif fault=='pending':lot.state=PENDING
    elif fault=='partial_exit':lot.partial_exit={'unknown':True}
    elif fault=='protection_filled':lot.trail_trade.orderStatus.filled=1
    elif fault=='protection_unknown':lot.trail_trade.orderStatus.status='PendingSubmit'
    elif fault=='missing_protection':lot.trail_trade=None
    elif fault=='external':lot.external_account='U123';lot.limit_type='adopted'
    elif fault=='invalid_policy':lot.signal_snapshot[POLICY_KEY]['version']=99
    run(mgr.protect_candidate_order(lot.lot_id,data,now=NOW))
    assert not gw.stop_px_calls and not gw.close_calls and not gw.stop_only_calls


@pytest.mark.parametrize('side',['long','short'])
def test_only_selected_st_trailing_exits_on_its_confirmed_opposite_direction(tmp_path,side):
    mgr,lot,gw,data=protection_case(tmp_path,side,timed_breakeven_enabled=False,trailing_enabled=True,trailing_source='st_1m_10_3')
    minute=int(NOW)//60*60-60
    data['candidate_st_1m_c']={'version':1,'time':minute,'price':110 if side=='long' else 90,
        'direction':-1 if side=='long' else 1,'atr_period':10,'multiplier':3,'interval_seconds':60,'session':'regular'}
    async def go():
        outcome=await mgr.protect_candidate_order(lot.lot_id,data,now=NOW)
        assert outcome['ok'] and outcome['action']=='close_submitted'
        assert lot.close_reason=='candidate_st_trailing_flip' and lot.state==CLOSING
        assert len(gw.close_calls)==1 and gw.close_calls[0][-1].order.orderType=='MKT'
        await mgr.protect_candidate_order(lot.lot_id,data,now=NOW+1)
        assert len(gw.close_calls)==1 and not gw.stop_only_calls
        for task in mgr._watchers.values():task.cancel()
    run(go())


@pytest.mark.parametrize('side,direction,allowed',[('long',1,True),('long',-1,False),
    ('short',-1,True),('short',1,False),('long',None,False)])
def test_selected_st_trailing_also_requires_matching_confirmed_direction_at_entry(tmp_path,monkeypatch,side,direction,allowed):
    from .test_risk_exposure import make_mgr
    from execution import order_lot
    monkeypatch.setattr(order_lot.time,'time',lambda:NOW)
    mgr=make_mgr(tmp_path)
    mgr.entry_admission=lambda *a:{'ok':True,'evidence':{'candidate_st':{'direction':direction} if direction else None}}
    mgr._gw.place_market_with_stop=AsyncMock(return_value={'ok':True,'dry_run':True})
    value=103 if side=='long' else 97
    stop=95 if side=='long' else 105
    lot,reason=run(mgr.place_limit_stop_lot('NASDAQ:NVDA',side,1,value,stop,
        limit_type='market',manual_entry=True,entry_order_type='MKT',signal_snapshot={POLICY_KEY:policy(
            vwap_outside_only=False,trailing_enabled=True,trailing_source='st_1m_10_3')}))
    assert bool(lot)==allowed
    assert mgr._gw.place_market_with_stop.await_count==int(allowed)
    if not allowed:assert 'ST跟踪入场' in reason


def test_timer_prefers_actual_broker_fill_time_and_average(tmp_path):
    mgr,lot,gw,data=protection_case(tmp_path)
    lot.entry_time=NOW-100
    lot.entry_broker_time=NOW-365
    lot.avg_fill=101.25
    assert run(mgr.protect_candidate_order(lot.lot_id,data,now=NOW))['ok']
    assert gw.stop_px_calls==[101.25]
    assert lot.signal_snapshot['candidate_order_protection']['entry_time_basis']=='broker'


def test_policy_and_timer_progress_survive_sqlite_restore(tmp_path):
    mgr,lot,gw,data=protection_case(tmp_path)
    lot.entry_time=NOW-100
    run(mgr.protect_candidate_order(lot.lot_id,data,now=NOW))
    recovered=mk_mgr(gw,tmp_path,'candidate')._lots[lot.lot_id]
    assert recovered.signal_snapshot[POLICY_KEY]==policy()
    assert recovered.signal_snapshot['candidate_order_protection']['due_at']==NOW+235


def test_timer_excludes_the_entry_minute_even_at_an_exact_minute_boundary():
    minute=int(NOW)//60*60
    assert breakeven_due_at(minute+30,5)==minute+360
    assert breakeven_due_at(minute,5)==minute+360


def test_wall_clock_alone_does_not_allow_breakeven_before_minute_confirmation(tmp_path):
    mgr,lot,gw,data=protection_case(tmp_path)
    data['market_1m']['bar_c']['time']-=60
    run(mgr.protect_candidate_order(lot.lot_id,data,now=NOW))
    assert not gw.stop_px_calls
    assert lot.signal_snapshot['candidate_order_protection']['timer_rule']==TIMER_RULE


def test_live_controls_apply_to_existing_candidate_lots_and_keep_already_tightened_stops(tmp_path):
    mgr,lot,gw,data=protection_case(tmp_path)
    controls=policy(timed_breakeven_enabled=False)
    mgr.candidate_protection_options=lambda:dict(controls)
    run(mgr.protect_candidate_order(lot.lot_id,data,now=NOW));assert not gw.stop_px_calls
    controls['timed_breakeven_enabled']=True
    run(mgr.protect_candidate_order(lot.lot_id,data,now=NOW));assert gw.stop_px_calls==[100]
    controls['trailing_enabled']=True
    run(mgr.protect_candidate_order(lot.lot_id,data,now=NOW));assert gw.stop_px_calls==[100,101]
    controls['trailing_enabled']=False;data['ts_c']['ema_9']=102
    run(mgr.protect_candidate_order(lot.lot_id,data,now=NOW));assert lot.stop_px==101
    assert lot.signal_snapshot['candidate_order_protection']['breakeven_trigger']=='timed_breakeven'


def test_changing_the_timer_preserves_entry_anchor_and_recomputes_full_minute_deadline(tmp_path):
    mgr,lot,gw,data=protection_case(tmp_path)
    controls=policy(breakeven_minutes=10);mgr.candidate_protection_options=lambda:dict(controls)
    run(mgr.protect_candidate_order(lot.lot_id,data,now=NOW));assert not gw.stop_px_calls
    anchor=lot.signal_snapshot['candidate_order_protection']['entry_time']
    controls['breakeven_minutes']=5
    run(mgr.protect_candidate_order(lot.lot_id,data,now=NOW));assert gw.stop_px_calls==[100]
    assert lot.signal_snapshot['candidate_order_protection']['entry_time']==anchor


def test_switching_from_minute_st_to_older_five_minute_ema_is_not_blocked_by_old_source_timestamp(tmp_path):
    mgr,lot,gw,data=protection_case(tmp_path,timed_breakeven_enabled=False)
    controls=policy(timed_breakeven_enabled=False,trailing_enabled=True,trailing_source='st_1m_10_3')
    mgr.candidate_protection_options=lambda:dict(controls)
    minute=int(NOW)//60*60-60
    data['candidate_st_1m_c']={'version':1,'time':minute,'price':101,'direction':1,
        'atr_period':10,'multiplier':3,'interval_seconds':60,'session':'regular'}
    run(mgr.protect_candidate_order(lot.lot_id,data,now=NOW));assert gw.stop_px_calls==[101]
    controls['trailing_source']='ema9_5m';data['ts_c']['ema_9']=102
    run(mgr.protect_candidate_order(lot.lot_id,data,now=NOW));assert gw.stop_px_calls==[101,102]
    lot.signal_snapshot={'strategy_id':'ema7_autotrade_v2'}
    result=run(mgr.protect_candidate_order(lot.lot_id,data,now=NOW))
    assert not result['ok'] and gw.stop_px_calls==[101,102]


def test_shared_settings_persist_but_disk_failure_preserves_previous_runtime_settings(console_app,monkeypatch):
    from console.schemas import SettingsRequest, CandidateProtectionOptions
    c=console_app
    result=run(c.settings.api_update_settings(SettingsRequest(candidate_order_protection=CandidateProtectionOptions(trailing_enabled=True)),trader='test'))
    assert result['candidate_order_protection']['trailing_enabled']
    previous=c.settings._candidate_order_protection()
    monkeypatch.setattr(c.settings,'_save_settings',lambda:False)
    with pytest.raises(HTTPException,match='保存失败'):
        run(c.settings.api_update_settings(SettingsRequest(candidate_order_protection=CandidateProtectionOptions(trailing_enabled=False))))
    assert c.settings._candidate_order_protection()==previous


def test_failed_modification_retains_original_stop_and_can_retry(tmp_path):
    mgr,lot,gw,data=protection_case(tmp_path)
    real=gw.modify_stop
    gw.modify_stop=AsyncMock(return_value=False)
    assert not run(mgr.protect_candidate_order(lot.lot_id,data,now=NOW))['ok']
    assert lot.stop_px==95 and not lot.signal_snapshot['candidate_order_protection'].get('timed_breakeven_done')
    gw.modify_stop=real
    assert run(mgr.protect_candidate_order(lot.lot_id,data,now=NOW))['ok']
    assert lot.stop_px==100 and not gw.stop_only_calls


def test_exact_one_cent_trailing_change_is_not_lost_to_binary_float_rounding(tmp_path):
    mgr,lot,gw,data=protection_case(tmp_path,'short',timed_breakeven_enabled=False,trailing_enabled=True)
    lot.stop_px=356.67;lot.avg_fill=358.12
    data['bar']['close']=354.48;data['ts_c']['ema_9']=356.66
    outcome=run(mgr.protect_candidate_order(lot.lot_id,data,now=NOW))
    assert outcome['ok'] and gw.stop_px_calls==[356.66]


def test_policy_options_are_scoped_and_persisted_by_manual_endpoint(manual_market,monkeypatch):
    c=manual_market;now=c.data['bar']['time']+5
    c.data['price_observed_at']=now
    c.app.order_mgr.entry_admission=lambda *args:{'ok':True,'evidence':evidence(now=now,price=103)}
    monkeypatch.setattr(c.app.trading,'_rhythm_entry_plan',lambda *a:('long',103,95,{'sigma':2,'stop_atr_value':2}))
    args=dict(symbol='NASDAQ:NVDA',side='long',limit_type='market',limit_px=103,stop_px=95,
              entry_mode='rhythm_atr',candidate_order=policy())
    assert run(c.app.trading.api_place_limit_stop(LimitStopOrderRequest(**args)))['ok']
    assert c.place.await_args.kwargs['signal_snapshot'][POLICY_KEY]==policy()
    args['entry_mode']='default'
    with pytest.raises(HTTPException,match='仅用于轨道'):run(c.app.trading.api_place_limit_stop(LimitStopOrderRequest(**args)))


@pytest.mark.parametrize('values', [{'breakeven_minutes':0},{'breakeven_minutes':121},
    {'breakeven_minutes':1.5},{'breakeven_minutes':True},{'trailing_enabled':'false'},
    {'trailing_source':'st_1m_10_4'},{'version':2}])
def test_invalid_http_policy_options_are_rejected(values):
    with pytest.raises(ValidationError):CandidateOrderOptions(**values)


@pytest.mark.parametrize('stage', ['risk_wait','after_intent'])
def test_vwap_crossing_during_submission_is_rechecked_without_broker_order(tmp_path,monkeypatch,stage):
    from execution import order_lot
    from .test_risk_exposure import make_mgr
    monkeypatch.setattr(order_lot.time,'time',lambda:NOW)
    mgr=make_mgr(tmp_path)
    facts=evidence()
    mgr.entry_admission=lambda *args:{'ok':True,'evidence':deepcopy(facts)}
    mgr._gw.place_market_with_stop=AsyncMock()
    if stage=='risk_wait':
        async def pnl():facts['ema9_price']=101;return 0
        mgr._gw.realized_pnl=pnl
    else:
        save=mgr._save
        def changed():save();facts['ema9_price']=101
        mgr._save=changed
    lot,reason=run(mgr.place_limit_stop_lot('NASDAQ:NVDA','long',10,103,95,
        limit_type='market',manual_entry=True,entry_order_type='MKT',signal_snapshot={POLICY_KEY:policy()}))
    assert lot is None and 'VWAP区间过滤' in reason
    mgr._gw.place_market_with_stop.assert_not_awaited()
    assert all(not row.entry_submission_pending for row in mgr._lots.values())


def test_explicit_timer_off_does_not_fall_back_to_old_r_breakeven(console_app,monkeypatch):
    from console.services import monitoring
    c=console_app
    c.gateway._connected=True
    monkeypatch.setattr(c.system,'_is_trading_hours',lambda:True)
    monkeypatch.setattr(c.collector,'data_health',lambda:{'stale':False})
    monkeypatch.setattr(c.collector,'latest',lambda:{})
    monkeypatch.setattr(c.order_mgr,'list_lots',lambda **k:[{'lot_id':'x','state':'ACTIVE','symbol':'NASDAQ:NVDA',
        'side':'long','avg_fill':100,'filled_qty':10,'signal_snapshot':{POLICY_KEY:policy(timed_breakeven_enabled=False)}}])
    protect=AsyncMock(return_value={'ok':True,'action':'waiting'})
    old=AsyncMock()
    monkeypatch.setattr(c.order_mgr,'protect_candidate_order',protect)
    monkeypatch.setattr(c.order_mgr,'breakeven',old)
    ticks=[]
    async def tick(*args):
        if ticks:raise asyncio.CancelledError
        ticks.append(True)
    monkeypatch.setattr(monitoring.asyncio,'sleep',tick)
    with pytest.raises(asyncio.CancelledError):run(c.monitoring._auto_breakeven_loop())
    protect.assert_awaited_once();old.assert_not_awaited()


def test_an_exit_request_invalidates_queued_candidate_protection(tmp_path):
    from execution.operations import OperationKind
    async def go():
        mgr,lot,gw,data=protection_case(tmp_path)
        lock=mgr._lot_lock(lot)
        async with lock:
            pending=asyncio.create_task(mgr.protect_candidate_order(lot.lot_id,data,now=NOW))
            await asyncio.sleep(0)
            async def terminal():
                async with mgr._operations.run(lot.lot_id,OperationKind.EXIT,lock=lock):
                    lot.state=CLOSING
            exit_task=asyncio.create_task(terminal())
            await asyncio.sleep(0)
        result=await pending;await exit_task
        assert result['action']=='blocked'
        assert not gw.stop_px_calls
    run(go())
