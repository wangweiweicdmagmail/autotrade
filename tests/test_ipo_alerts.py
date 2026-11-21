"""All IPO state is temporary; no market or broker connections."""
import sqlite3
from types import SimpleNamespace
from unittest.mock import Mock
from datetime import datetime, timezone
import pytest
from console.services.ipo_alerts import IpoAlerts, candidate
from engine.realtime_collector import RealtimeCollector

NOW=datetime(2026,11,19,15,12,17,tzinfo=timezone.utc).timestamp()
WATCH={'NASDAQ:ANT':'2026-11-19'}


def trade(**changes):
    return dict(symbol='NASDAQ:ANT',listing_date='2026-11-19',price=101.25,volume=12000,
                price_time=NOW,observed_at=NOW,source='quote',time_kind='trade_time',**changes)


def service(tmp_path):
    context=SimpleNamespace(paths=SimpleNamespace(data_dir=tmp_path),events=SimpleNamespace(record_event=Mock()))
    return IpoAlerts(context,WATCH)


def test_persisted_first_observation_is_not_replaced_or_reannounced_after_restart(tmp_path):
    a=service(tmp_path);record=trade();a.refresh({'NASDAQ:ANT':record},NOW)
    assert len(a.events)==1 and not a.error
    assert a.context.events.record_event.call_count==1
    b=service(tmp_path);b.refresh({'NASDAQ:ANT':{**record,'price':120,'observed_at':NOW+5,'price_time':NOW+5}},NOW+5)
    assert len(b.events)==1
    assert next(iter(b.events.values()))['price']==101.25
    b.context.events.record_event.assert_not_called()
    with sqlite3.connect(b.path) as db:assert db.execute('SELECT COUNT(*) FROM openings').fetchone()[0]==1


@pytest.mark.parametrize('change',[
    {'price':83,'volume':0},{'price_time':NOW-91},{'price_time':NOW+1},
    {'observed_at':NOW-31},{'price':True},{'price':float('nan')},
    {'listing_date':'2026-11-18'},{'symbol':'NASDAQ:OTHER'},{'time_kind':'unknown'},
])
def test_invalid_or_placeholder_prices_never_become_opening_events(tmp_path,change):
    a=service(tmp_path);record={**trade(),**change};a.refresh({'NASDAQ:ANT':record},NOW)
    assert a.events=={}
    a.context.events.record_event.assert_not_called()


def test_storage_failure_keeps_first_observation_for_retry_without_freshness_or_price_replacement(tmp_path,monkeypatch):
    a=service(tmp_path);original=a._load
    monkeypatch.setattr(a,'_load',lambda: (_ for _ in ()).throw(OSError('isolated disk busy')))
    a.refresh({'NASDAQ:ANT':trade()},NOW)
    assert a.error and not a.events and len(a.pending)==1
    monkeypatch.setattr(a,'_load',original)
    a.refresh({},NOW+40)
    saved=next(iter(a.events.values()))
    assert saved['detected_at']==NOW and saved['announce_until']==NOW+120
    assert not a.pending and not a.error


def test_unknown_cache_version_is_visible_without_resetting_state(tmp_path):
    a=service(tmp_path)
    with sqlite3.connect(a.path) as db:db.execute('PRAGMA user_version=99')
    a.refresh({'NASDAQ:ANT':trade()},NOW)
    assert a.error and not a.events
    with sqlite3.connect(a.path) as db:assert db.execute('PRAGMA user_version').fetchone()[0]==99


def test_independent_trade_messages_do_not_refresh_main_market_health_or_indicator_readiness(tmp_path):
    a=service(tmp_path)
    collector=RealtimeCollector(['NASDAQ:QQQ'],ipo_watches=WATCH,on_ipo_trade=a.refresh)
    # Freeze callback clock using its explicit supplied time, not real running data.
    collector.on_ipo_trade=lambda records,now:a.refresh(records,NOW)
    collector._accept_ipo({'type':'ipo_trade',**trade()})
    assert collector.symbols==['NASDAQ:QQQ'] and collector._snapshot=={}
    assert collector._last_update==collector._last_content_update==0
    assert collector.startup_subscribed==0 and collector.startup_indicators=={}
    assert len(a.events)==1


def test_ipo_status_api_uses_independent_market_messages_without_broker_or_execution_access(console_app,monkeypatch):
    from starlette.testclient import TestClient
    from console.routes import research
    monkeypatch.setattr(research,'time',SimpleNamespace(time=lambda:NOW))
    monkeypatch.setattr(console_app.ipo_alerts,'watches',WATCH)
    console_app.collector.ipo_status={'NASDAQ:ANT':{'phase':'waiting_trade'}}
    response=TestClient(console_app.app).get('/api/ipo-openings')
    assert response.status_code==200
    assert response.json()['events']==[]
    assert response.json()['watches'][0]['phase']=='waiting_trade'
    assert console_app.collector.symbols!=['NASDAQ:ANT']


def test_disabled_monitor_ignores_stale_ant_messages_and_has_no_ui_or_database_io(console_app):
    from starlette.testclient import TestClient
    console_app.collector.ipo_status={'NASDAQ:ANT':{'phase':'waiting_trade'}}
    console_app.collector.ipo_trades={'NASDAQ:ANT':trade()}
    assert console_app.collector.ipo_watches == {}
    assert console_app.ipo_alerts.watches == {}
    response=TestClient(console_app.app).get('/api/ipo-openings')
    assert response.status_code==200
    assert response.json()=={'watches':[],'events':[],'error':''}
    assert not console_app.ipo_alerts.path.exists()


def test_disabling_monitor_preserves_historical_ipo_records_without_loading_or_reannouncing(tmp_path,monkeypatch):
    active=service(tmp_path)
    active.refresh({'NASDAQ:ANT':trade()},NOW)
    disabled=IpoAlerts(active.context,{})
    monkeypatch.setattr(disabled,'_load',lambda:pytest.fail('disabled monitor must not access storage'))
    disabled.refresh({'NASDAQ:ANT':trade()},NOW)
    snapshot=disabled.snapshot(SimpleNamespace(ipo_trades={'NASDAQ:ANT':trade()},ipo_status={}),NOW)
    assert snapshot=={'watches':[],'events':[],'error':''}
    assert not disabled.loaded
    with sqlite3.connect(disabled.path) as db:
        assert db.execute('SELECT COUNT(*) FROM openings').fetchone()[0]==1
