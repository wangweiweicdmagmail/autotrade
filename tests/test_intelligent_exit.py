"""All state and broker traffic stay in temporary SQLite/in-memory gateways."""
import asyncio
from copy import deepcopy

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from console.schemas import IntelligentExitOptions, SettingsRequest
from execution.intelligent_exit import STATE_KEY, plan_exit
from execution.order_lot import ACTIVE, CLOSING, CLOSED
from .conftest import run
from .test_candidate_order_policy import NOW, policy
from .test_order_lot import FakeGW, mk_mgr, mk_lot, alive_trail, attach


def case(tmp_path, side='long', price=102, count=-3, live=True):
    gw = FakeGW(fills={}, positions={'QCOM': 10 if side == 'long' else -10})
    gw.enabled = live
    mgr = mk_mgr(gw, tmp_path, 'smart')
    lot = mk_lot(state=ACTIVE, side=side, qty=10, stop_px=95 if side == 'long' else 105)
    lot.avg_fill = 100
    lot.filled_qty = 10
    lot.entry_time = NOW-100
    attach(mgr, lot, trail=alive_trail(lot))
    options = IntelligentExitOptions(enabled=True).dict()
    mgr.intelligent_exit_options = lambda: dict(options)
    mgr._start_close_watch = lambda *args: None
    mgr._start_partial_watch = lambda *args: None
    mgr._start_exit_watch = lambda *args: None
    data = {'bar': {'time': int(NOW)//300*300, 'close': price}, 'price_observed_at': NOW,
            'market_1m': {'bar_c': {'time': int(NOW)//60*60-60}},
            'td_exit_1m_c': {'version': 1, 'time': int(NOW)//60*60-60, 'count': count,
                            'interval_seconds': 60, 'comparison_bars': 4, 'session': 'extended'}}
    return mgr, lot, gw, data, options


@pytest.mark.parametrize('side,price,count,action', [
    ('long',99,-3,'close'),('long',100,-3,'close'),('long',101.5,-3,'close'),
    ('long',101.5001,-3,'half'),('long',102,-3,'half'),('long',104,-3,'half'),
    ('short',101,3,'close'),('short',100,3,'close'),('short',98.5,3,'close'),
    ('short',98.4999,3,'half'),('short',98,3,'half'),('short',96,3,'half'),
    ('long',120,-4,'close'),('short',80,4,'close'),('long',103,-6,'close'),
    ('long',102,3,'waiting'),('short',98,-3,'waiting'),('long',102,-2,'waiting'),
])
def test_td_boundaries_and_mirrored_directions(tmp_path, side, price, count, action):
    mgr,lot,gw,data,options=case(tmp_path,side,price,count)
    assert plan_exit(lot,data,options,NOW)['action']==action


def test_r_uses_actual_cost_and_initial_stop_not_remaining_qty_or_breakeven_stop(tmp_path):
    mgr,lot,gw,data,options=case(tmp_path,price=102)
    lot.stop_px=101;lot.filled_qty=2
    assert plan_exit(lot,data,options,NOW)['profit_r']==.4
    lot.avg_fill=101
    assert plan_exit(lot,data,options,NOW)['action']=='close'


@pytest.mark.parametrize('mutation', ['stale','future','misaligned','wrong_period','bool_count','wrong_comparison','missing','wrong_session'])
def test_unconfirmed_or_incompatible_td_cannot_exit(tmp_path, mutation):
    mgr,lot,gw,data,options=case(tmp_path)
    td=data['td_exit_1m_c']
    if mutation=='stale':td['time']-=120
    elif mutation=='future':td['time']+=120
    elif mutation=='misaligned':data['market_1m']['bar_c']['time']-=60
    elif mutation=='wrong_period':td['interval_seconds']=300
    elif mutation=='bool_count':td['count']=True
    elif mutation=='wrong_comparison':td['comparison_bars']=3
    elif mutation=='missing':data.pop('td_exit_1m_c')
    else:td['session']='unknown'
    assert run(mgr.protect_intelligent_exit(lot.lot_id,data,now=NOW))['action']=='waiting'
    assert not gw.close_calls


def test_minute_from_before_entry_cannot_exit_new_position(tmp_path):
    mgr,lot,gw,data,options=case(tmp_path,count=-4)
    lot.entry_time=NOW-1
    assert run(mgr.protect_intelligent_exit(lot.lot_id,data,now=NOW))['action']=='waiting'


def test_half_is_market_and_restart_does_not_repeat_unknown_reduction(tmp_path):
    mgr,lot,gw,data,options=case(tmp_path)
    outcome=run(mgr.protect_intelligent_exit(lot.lot_id,data,now=NOW))
    assert outcome['action']=='half_submitted' and len(gw.close_calls)==1
    assert gw.close_calls[0][2]==5 and gw.close_calls[0][-1].order.orderType=='MKT'
    assert gw.stop_qty_calls==[5]
    restored=mk_mgr(gw,tmp_path,'smart')
    restored.intelligent_exit_options=lambda:options
    assert restored._lots[lot.lot_id].partial_exit
    assert run(restored.protect_intelligent_exit(lot.lot_id,data,now=NOW))['action']=='waiting'
    assert len(gw.close_calls)==1


def test_full_exit_sizes_only_reconciled_remainder_after_half(tmp_path):
    async def go():
        mgr,lot,gw,data,options=case(tmp_path)
        assert (await mgr.protect_intelligent_exit(lot.lot_id,data,now=NOW))['ok']
        half=lot.close_trade
        half.orderStatus.status='Filled';half.orderStatus.filled=5;half.orderStatus.avgFillPrice=102
        gw._pos={'QCOM':5}
        assert await mgr._handle_partial_terminal(lot,5)
        assert lot.filled_qty==5 and lot.close_reason=='intelligent_td_reverse_3'
        assert (await mgr.protect_intelligent_exit(lot.lot_id,data,now=NOW))['action']=='waiting'
        data['td_exit_1m_c']['time']+=60;data['td_exit_1m_c']['count']=-4
        data['market_1m']['bar_c']['time']+=60;data['price_observed_at']=NOW+60
        outcome=await mgr.protect_intelligent_exit(lot.lot_id,data,now=NOW+60)
        assert outcome['action']=='close_submitted'
        assert [c[2] for c in gw.close_calls]==[5,5]
        assert lot.state==CLOSING and lot.close_reason=='intelligent_td_reverse_4'
    run(go())


def test_unknown_full_result_stays_closing_and_never_resubmits(tmp_path):
    mgr,lot,gw,data,options=case(tmp_path,count=-4)
    async def unknown(*args,**kwargs):
        gw.close_calls.append(args);return {'ok':False,'error':'timeout'}
    gw.place_close=unknown
    assert not run(mgr.protect_intelligent_exit(lot.lot_id,data,now=NOW))['ok']
    assert lot.state==CLOSING
    run(mgr.protect_intelligent_exit(lot.lot_id,data,now=NOW+1))
    assert len(gw.close_calls)==1 and not gw.stop_only_calls


@pytest.mark.parametrize('reason', ['off','external','pending','stale','offline'])
def test_disabled_or_unverified_positions_never_submit(tmp_path,reason):
    mgr,lot,gw,data,options=case(tmp_path,count=-4)
    if reason=='off':options['enabled']=False
    elif reason=='external':lot.external_account='SYNTHETIC'
    elif reason=='pending':lot.entry_submission_pending=True
    elif reason=='stale':mgr.market_health=lambda:{'stale':True}
    else:gw._connected=False
    assert run(mgr.protect_intelligent_exit(lot.lot_id,data,now=NOW))['action']=='waiting'
    assert not gw.close_calls


@pytest.mark.parametrize('source',[{}, {'strategy_id':'ema7_autotrade_v2'}, {'opening_breakout_id':'sample'},
                                 {'golden_signal_id':'sample'}, {'candidate_order_policy':policy()}])
def test_all_console_strategies_are_in_scope(tmp_path,source):
    mgr,lot,gw,data,options=case(tmp_path,count=-4,live=False)
    lot.signal_snapshot=source
    assert run(mgr.protect_intelligent_exit(lot.lot_id,data,now=NOW))['action']=='close_submitted'
    assert lot.state==CLOSED


@pytest.mark.parametrize('side', ['long','short'])
def test_timer_counts_five_full_minutes_excluding_entry_minute(tmp_path,side):
    mgr,lot,gw,data,options=case(tmp_path,side=side,price=103 if side=='long' else 97,count=0)
    lot.entry_time=NOW-300
    assert run(mgr.protect_intelligent_exit(lot.lot_id,data,now=NOW))['action']=='waiting'
    data['market_1m']['bar_c']['time']+=60;data['price_observed_at']=NOW+55
    outcome=run(mgr.protect_intelligent_exit(lot.lot_id,data,now=NOW+55))
    assert outcome['action']=='stop_updated' and gw.stop_px_calls==[100]
    run(mgr.protect_intelligent_exit(lot.lot_id,data,now=NOW+56))
    assert gw.stop_px_calls==[100]


@pytest.mark.parametrize('side,price',[('long',99),('short',101)])
def test_timer_exits_losing_position_even_without_td(tmp_path,side,price):
    mgr,lot,gw,data,options=case(tmp_path,side=side,price=price,count=0,live=False)
    lot.entry_time=NOW-365;data.pop('td_exit_1m_c')
    outcome=run(mgr.protect_intelligent_exit(lot.lot_id,data,now=NOW))
    assert outcome['action']=='close_submitted' and lot.exit_reason=='intelligent_timed_breakeven'


def test_tighter_stop_is_not_loosened_and_one_share_not_force_liquidated_as_half(tmp_path):
    mgr,lot,gw,data,options=case(tmp_path)
    lot.filled_qty=1
    assert run(mgr.protect_intelligent_exit(lot.lot_id,data,now=NOW))['action']=='half_skipped'
    assert not gw.close_calls
    lot.entry_time=NOW-365;lot.signal_snapshot[STATE_KEY]['entry_time']=lot.entry_time
    lot.stop_px=101
    assert run(mgr.protect_intelligent_exit(lot.lot_id,data,now=NOW))['action']=='waiting'
    assert not gw.stop_px_calls


def test_recheck_switch_and_duplicate_calls_under_operation_lock(tmp_path):
    async def go():
        mgr,lot,gw,data,options=case(tmp_path)
        results=await asyncio.gather(*(mgr.protect_intelligent_exit(lot.lot_id,data,now=NOW) for _ in range(3)))
        assert len(gw.close_calls)==1
        assert sum(r['action']=='half_submitted' for r in results)==1
    run(go())
    mgr,lot,gw,data,options=case(tmp_path/'off')
    original=mgr.close_half
    async def disable(*args,**kwargs):
        options['enabled']=False
        return await original(*args,**kwargs)
    mgr.close_half=disable
    assert not run(mgr.protect_intelligent_exit(lot.lot_id,data,now=NOW))['ok']
    assert not gw.close_calls and not lot.partial_exit


def test_global_module_replaces_candidate_timer_without_disabling_trailing(tmp_path):
    mgr,lot,gw,data,options=case(tmp_path)
    lot.signal_snapshot['candidate_order_policy']=policy(trailing_enabled=True)
    mgr.candidate_protection_options=lambda:policy(trailing_enabled=True)
    effective=mgr._effective_candidate_policy(lot.signal_snapshot['candidate_order_policy'])
    assert not effective['timed_breakeven_enabled'] and effective['trailing_enabled']


def test_settings_default_off_persistence_and_disk_failure(console_app,monkeypatch):
    c=console_app
    assert c.settings._intelligent_exit()=={'version':1,'enabled':False,'breakeven_minutes':5}
    result=run(c.settings.api_update_settings(SettingsRequest(intelligent_exit=IntelligentExitOptions(enabled=True))))
    assert result['intelligent_exit']['enabled']
    before=deepcopy(c.settings._intelligent_exit())
    monkeypatch.setattr(c.settings,'_save_settings',lambda:False)
    with pytest.raises(HTTPException,match='保存失败'):
        run(c.settings.api_update_settings(SettingsRequest(intelligent_exit=IntelligentExitOptions())))
    assert c.settings._intelligent_exit()==before


def test_broker_stop_fill_during_half_preparation_never_sends_extra_close(tmp_path):
    mgr,lot,gw,data,options=case(tmp_path)
    original=gw.modify_stop_qty
    async def stop_fills(trade,quantity):
        await original(trade,quantity)
        trade.orderStatus.filled=5;trade.orderStatus.avgFillPrice=95
        return True
    gw.modify_stop_qty=stop_fills
    outcome=run(mgr.protect_intelligent_exit(lot.lot_id,data,now=NOW))
    assert not outcome['ok'] and not gw.close_calls and not lot.partial_exit


def test_new_reverse_episode_can_reduce_once_but_restart_cannot_repeat_old_episode(tmp_path):
    mgr,lot,gw,data,options=case(tmp_path,live=False)
    assert run(mgr.protect_intelligent_exit(lot.lot_id,data,now=NOW))['action']=='half_submitted'
    assert lot.filled_qty==5
    restored=mk_mgr(gw,tmp_path,'smart');restored.intelligent_exit_options=lambda:options
    assert run(restored.protect_intelligent_exit(lot.lot_id,data,now=NOW))['action']=='waiting'
    data['td_exit_1m_c']['time']+=180;data['market_1m']['bar_c']['time']+=180
    data['price_observed_at']=NOW+180;data['bar']['time']=int(NOW+180)//300*300
    outcome=run(restored.protect_intelligent_exit(lot.lot_id,data,now=NOW+180))
    assert outcome['action']=='half_submitted'
    assert restored._lots[lot.lot_id].filled_qty==3


def test_monitor_calls_global_exit_before_skipping_pyramid_and_signal_profiles(console_app,monkeypatch):
    from console.services import monitoring
    from unittest.mock import AsyncMock
    c=console_app;c.gateway._connected=True
    c.settings._settings['intelligent_exit']=IntelligentExitOptions(enabled=True).dict()
    monkeypatch.setattr(c.system,'_is_trading_hours',lambda:True)
    monkeypatch.setattr(c.collector,'data_health',lambda:{'stale':False})
    monkeypatch.setattr(c.collector,'latest',lambda:{})
    monkeypatch.setattr(c.monitoring,'_is_pyramid_lot',lambda lot:True)
    monkeypatch.setattr(c.order_mgr,'list_lots',lambda **kwargs:[{'lot_id':'pyramid','state':'ACTIVE','symbol':'TEST','side':'long'}])
    protect=AsyncMock(return_value={'ok':True,'action':'waiting'})
    monkeypatch.setattr(c.order_mgr,'protect_intelligent_exit',protect)
    ticks=[]
    async def tick(*args):
        if ticks:raise asyncio.CancelledError
        ticks.append(True)
    monkeypatch.setattr(monitoring.asyncio,'sleep',tick)
    with pytest.raises(asyncio.CancelledError):run(c.monitoring._auto_breakeven_loop())
    protect.assert_awaited_once()


@pytest.mark.parametrize('changes',[{'enabled':'true'},{'breakeven_minutes':0},{'breakeven_minutes':1.5},{'unexpected':True}])
def test_invalid_settings_rejected(changes):
    with pytest.raises(ValidationError):IntelligentExitOptions(**changes)
