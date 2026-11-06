"""Reverse FVG must never amend/close positions without explicit user action."""
import asyncio
from types import SimpleNamespace
import pytest
import config
from .conftest import run

@pytest.mark.parametrize('side,price',[('long',99),('long',101),('short',99),('short',101)])
@pytest.mark.parametrize('old_switch',[False,True])
def test_reverse_fvg_is_alert_only_even_with_old_auto_close_switch(console_app,monkeypatch,side,price,old_switch):
    app=console_app;events=[]
    monkeypatch.setattr(config,'FVG_REVERSE_GUARD_ENABLED',True)
    monkeypatch.setattr(config,'FVG_REVERSE_AUTO_CLOSE',old_switch)
    monkeypatch.setattr(app.system,'_is_trading_hours',lambda:True)
    monkeypatch.setattr(app.gateway,'_connected',True)
    monkeypatch.setattr(app.collector,'data_health',lambda:{'stale':False})
    lot={'lot_id':'asml','symbol':'NASDAQ:ASML','side':side,'qty':68,'filled_qty':68,
         'avg_fill':100,'stop_px':90 if side=='long' else 110,'state':'ACTIVE'}
    async def forbidden(*a,**k):pytest.fail('FVG attempted a position action without consent')
    monkeypatch.setattr(app.monitoring.context,'order_mgr',SimpleNamespace(
        list_lots=lambda **kw:[lot],breakeven=forbidden,close_lot=forbidden))
    monkeypatch.setattr(app.collector,'latest',lambda:{'NASDAQ:ASML':{
        'bar_c':{'time':1000,'close':price},
        'fvg_c':{'time':1000,'fvg_event':-1 if side=='long' else 1}}})
    monkeypatch.setattr(app.accounts,'_tv_last_price',lambda symbol:price)
    monkeypatch.setattr(app.events,'record_event',lambda kind,**row:events.append((kind,row)))
    ticks=0
    async def sleep(_):
        nonlocal ticks
        ticks+=1
        if ticks>2:raise asyncio.CancelledError
    monkeypatch.setattr('console.services.monitoring.asyncio.sleep',sleep)
    with pytest.raises(asyncio.CancelledError):run(app.monitoring._fvg_reverse_guard_loop())
    assert len(events)==1 and events[0][0]=='fvg_reverse_alert'
    alert=app.monitoring._fvg_reverse_alerts['asml']
    assert alert['requires_confirmation'] and alert['kind']=='reverse_warning'
    assert lot['stop_px']==(90 if side=='long' else 110)
