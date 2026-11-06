"""ST reversals notify only. Synthetic orders, no broker or production state."""
import asyncio
import pytest
import config
from .conftest import run
from .test_pyramid_engine import FakeBroker,_e9
from engine.pyramid_engine import PyramidEngine

SYM='NASDAQ:SMH'

@pytest.mark.parametrize('direction',[1,-1])
@pytest.mark.parametrize('legacy_flag',[False,True])
def test_reverse_alert_keeps_position_and_stop_and_deduplicates(direction,monkeypatch,legacy_flag):
    monkeypatch.setattr(config,'AUTO_CLOSE_ON_ST_FLIP',legacy_flag)
    broker=FakeBroker();events=[]
    engine=PyramidEngine(broker,emit=lambda event,**row:events.append((event,row)))
    engine.start([SYM],300)
    run(engine.on_price(SYM,100,_e9(100,2,direction=direction),600))
    run(engine.on_price(SYM,100,_e9(100,2,direction=direction),600))
    inst=engine._instances[SYM];qty=inst.total_qty;stop=inst.stop_px
    bar={'time':1300,'close':100}
    run(engine.on_bar(SYM,bar,{'direction':-direction}))
    run(engine.on_bar(SYM,bar,{'direction':-direction}))
    assert broker.closes==[]
    assert inst.total_qty==qty and qty>0
    assert inst.stop_px==stop and inst.state=='ACTIVE'
    assert inst.enabled and not inst.exit_requested_reason
    alerts=[row for event,row in events if event=='pyramid_trend_reverse_alert']
    assert len(alerts)==1 and alerts[0]['side']==('long' if direction==1 else 'short')
    # Explicit manual exit remains available.
    engine.stop_symbol(SYM,close_position=True)
    run(engine.on_bar(SYM,bar,{'direction':-direction}))
    assert broker.closes


def test_old_unsubmitted_reverse_intent_does_not_retry(monkeypatch):
    monkeypatch.setattr(config,'AUTO_CLOSE_ON_ST_FLIP',False)
    broker=FakeBroker();engine=PyramidEngine(broker)
    engine.start([SYM],300)
    run(engine.on_price(SYM,100,_e9(100,2,direction=1),600))
    run(engine.on_price(SYM,100,_e9(100,2,direction=1),600))
    inst=engine._instances[SYM]
    inst.exit_requested_reason='trend_reverse'
    inst.state='CLOSING'
    run(engine.on_price(SYM,100,_e9(100,2,direction=1),600))
    assert broker.closes==[] and not inst.exit_requested_reason


@pytest.mark.parametrize('position,direction',[(-155,1),(155,-1)])
@pytest.mark.parametrize('legacy_flag',[False,True])
def test_smh_scan_never_calls_close_when_alert_only(console_app,monkeypatch,position,direction,legacy_flag):
    app=console_app;events=[]
    monkeypatch.setattr(config,'AUTO_CLOSE_ON_ST_FLIP',legacy_flag)
    monkeypatch.setattr(app.system,'_is_trading_hours',lambda:True)
    monkeypatch.setattr(app.collector,'data_health',lambda:{'stale':False})
    monkeypatch.setattr(app.collector,'latest',lambda:{SYM:{'bar':{'time':1300,'close':464.38},
      'bar_c':{'time':1000},'ts_c':{'time':1000,'direction':direction,'supertrend':451.13}}})
    async def positions():return {'SMH':position}
    monkeypatch.setattr(app.gateway,'positions',positions)
    monkeypatch.setattr(app.gateway,'enabled',True)
    monkeypatch.setattr(app.gateway,'_connected',True)
    async def forbidden(*args,**kwargs):pytest.fail('ST reversal attempted to close')
    monkeypatch.setattr(app.monitoring,'_close_on_flip',forbidden)
    monkeypatch.setattr(app.events,'record_event',lambda kind,**row:events.append((kind,row)))
    calls=0
    async def sleep(_):
        nonlocal calls
        calls+=1
        if calls>1:raise asyncio.CancelledError
    monkeypatch.setattr('console.services.monitoring.asyncio.sleep',sleep)
    with pytest.raises(asyncio.CancelledError):run(app.monitoring._position_scan_loop())
    alert=app.monitoring._scan_alerts['SMH']
    assert alert['qty']==155 and not alert['auto_close_failed']
    assert [kind for kind,row in events]==['position_scan_alert']


@pytest.mark.parametrize('reason',['st_flip','trend_reverse','fvg_reverse'])
def test_order_boundary_rejects_automatic_direction_exit_even_if_legacy_switch_true(tmp_path,monkeypatch,reason):
    from .test_order_lot import FakeGW,mk_mgr,mk_lot,attach,alive_trail
    monkeypatch.setattr(config,'AUTO_CLOSE_ON_ST_FLIP',True)
    gw=FakeGW();mgr=mk_mgr(gw,tmp_path,'no-auto-direction')
    lot=mk_lot(state='ACTIVE');lot.filled_qty=100;lot.avg_fill=100
    attach(mgr,lot,trail=alive_trail(lot))
    ok,error=run(mgr.close_lot(lot.lot_id,ref_px=100,exit_reason=reason))
    assert not ok and '仅提醒' in error
    assert gw.close_calls==[] and gw.cancel_calls==[]
