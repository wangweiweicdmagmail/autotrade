import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import json
import sqlite3
import pytest

from engine.research_archive import ResearchArchive, envelope
from engine.research_calendar import ET, schedule
from engine.research_store import ResearchStore, encode
from engine.research_backfill import repair_day


def stamp(value):
    return int(datetime.fromisoformat(value).replace(tzinfo=ET).timestamp())


SERIES = dict(provider='tradingview',symbol='NASDAQ:TEST',resolved_symbol='BATS:TEST',
              interval='1m',session='regular',adjustment='splits',timezone='America/New_York',minmove=1,pricescale=100,type='stock')
OPEN = stamp('2026-11-20T09:30:00')


def bar(time=OPEN, close=100):
    return dict(time=time,close_time=time+60,open=100,high=102,low=98,close=close,volume=1000)


def save(store, bars=None, observed=None, identity=None, series=None):
    observed = observed or OPEN+120
    record = envelope('bars',{'series':series or SERIES,'rows':bars or [bar()],'received_at':observed},observed_at=observed,identity=identity)
    store.apply(record)
    return record


def test_candles_revisions_are_idempotent_and_respect_observed_time(tmp_path):
    store = ResearchStore(tmp_path/'research.sqlite3')
    record = save(store,identity='p:1')
    store.apply(record)
    save(store,observed=OPEN+125)
    save(store,[bar(close=101)],OPEN+130)
    save(store,[bar()],OPEN+140)
    series = store.series()[0]['id']
    assert store.bars(series,OPEN,OPEN+60,as_of=OPEN+119)==[]
    assert store.bars(series,OPEN,OPEN+60,as_of=OPEN+120)[0]['close']==100
    assert store.bars(series,OPEN,OPEN+60,as_of=OPEN+135)[0]['close']==101
    assert store.bars(series,OPEN,OPEN+60)[0]['close']==100
    with store.read() as db:
        assert db.execute('SELECT COUNT(*) FROM candle_versions').fetchone()[0]==2
        assert db.execute('SELECT COUNT(*) FROM candle_changes').fetchone()[0]==3
    # A delayed import cannot overwrite a more recently observed correction.
    save(store,[bar(close=99)],OPEN+129)
    assert store.bars(series,OPEN,OPEN+60)[0]['close']==100


@pytest.mark.parametrize('bad',[
    {**bar(),'close_time':OPEN+200}, {**bar(),'high':99}, {**bar(),'volume':-1},
    {**bar(),'time':True}, {**bar(),'close':float('nan')},
])
def test_invalid_or_unclosed_bars_do_not_advance_import(tmp_path,bad):
    store = ResearchStore(tmp_path/'research.sqlite3')
    with pytest.raises((ValueError,TypeError)):
        save(store,[bad],identity='bad')
    if store.path.exists():
        with store.read() as db:
            assert db.execute('SELECT COUNT(*) FROM imported_records').fetchone()[0]==0


def test_unknown_database_is_not_recreated_and_backup_is_consistent(tmp_path):
    path=tmp_path/'unknown.sqlite3'
    with sqlite3.connect(path) as db:db.execute('CREATE TABLE precious (value TEXT)')
    with pytest.raises(ValueError,match='结构未知'):save(ResearchStore(path))
    store=ResearchStore(tmp_path/'ok.sqlite3');save(store)
    store.backup(tmp_path/'backup.sqlite3')
    assert ResearchStore(tmp_path/'backup.sqlite3').series()==store.series()
    with pytest.raises(ValueError):store.backup(tmp_path/'backup.sqlite3')


def test_journal_crash_retry_torn_tail_and_corruption_quarantine(tmp_path,monkeypatch):
    archive=ResearchArchive(tmp_path/'research');archive.spool.mkdir(parents=True)
    a=envelope('bars',{'series':SERIES,'rows':[bar()]},observed_at=OPEN+120,identity='producer:1')
    b=envelope('bars',{'series':SERIES,'rows':[bar(OPEN+60)]},observed_at=OPEN+180,identity='producer:2')
    file=archive.spool/'producer.ndjson'
    file.write_text(json.dumps(a)+'\n'+json.dumps(b)[:30])
    assert archive.pump()==1
    assert archive.store.cursor('journal:producer.ndjson')[0]==len((json.dumps(a)+'\n').encode())
    assert archive.store.status()['issues']==1
    with file.open('a') as f:f.write(json.dumps(b)[30:]+'\n')
    assert archive.pump()==1
    assert archive.store.status()['issues']==0
    assert ResearchArchive(tmp_path/'research').pump()==0
    bad={**a,'id':'corrupt','sha256':'wrong'}
    with file.open('a') as f:f.write(json.dumps(bad)+'\n'+json.dumps(b)+'\n')
    assert archive.pump()==2
    assert archive.store.status()['issues']==1
    # Storage failure leaves cursor at the last committed record.
    c={**b,'id':'producer:3'}
    with file.open('a') as f:f.write(json.dumps(c)+'\n')
    original=archive.store.apply
    monkeypatch.setattr(archive.store,'apply',lambda *a,**k:(_ for _ in ()).throw(sqlite3.OperationalError('disk')))
    with pytest.raises(sqlite3.OperationalError):archive.pump()
    monkeypatch.setattr(archive.store,'apply',original)
    assert archive.pump()==1


def test_partial_and_future_rankings_are_rejected_and_revisions_remain_queryable(tmp_path):
    store=ResearchStore(tmp_path/'archive.sqlite3')
    base={'batch_id':'one','day':'2026-11-20','as_of':OPEN+60,'ranks':[],
          'coverage':{'total':2,'aligned':2,'missing':[]},'scoring':{'pending':[]}}
    store.apply(envelope('ranking',base,observed_at=OPEN+61))
    store.apply(envelope('ranking',{**base,'batch_id':'two'},observed_at=OPEN+70))
    assert len(store.rankings('2026-11-20',as_of=OPEN+65))==1
    for payload,observed in [({**base,'coverage':{'total':2,'aligned':1,'missing':['TEST']}},OPEN+61),(base,OPEN+59)]:
        with pytest.raises(ValueError):store.apply(envelope('ranking',payload,observed_at=observed))


def test_calendar_early_close_dst_and_unknown_index_schedule(tmp_path):
    assert schedule('2026-07-03')['closed']
    assert schedule('2026-11-27')['regular_close']-schedule('2026-11-27')['regular_open']==210*60
    assert schedule('2026-03-09')['regular_open']-schedule('2026-03-06')['regular_open']==3*86400-3600
    assert schedule('2035-01-02') is None
    store=ResearchStore(tmp_path/'archive.sqlite3')
    start=schedule('2026-11-27')['regular_open']
    rows=[bar(t) for t in range(start,schedule('2026-11-27')['regular_close'],60)]
    save(store,rows,stamp('2026-11-28T10:00:00'))
    report=store.manifest('2026-11-27',now=stamp('2026-11-28T10:00:00'))
    assert report['complete']
    index={**SERIES,'symbol':'CAPITALCOM:NAS100','session':'default','resolved_symbol':'CAPITALCOM:NAS100'}
    save(store,rows,stamp('2026-11-28T10:00:00'),series=index)
    assert not store.manifest('2026-11-27')['complete']


def test_backfill_is_bounded_and_does_not_backdate_observation(tmp_path):
    store=ResearchStore(tmp_path/'archive.sqlite3')
    now=OPEN+120
    store.apply(envelope('configuration',{'series':[SERIES]},observed_at=now))
    calls=[]
    async def fetch(series,end,count,credentials):
        calls.append(series)
        return {'metadata':{'timezone':'America/New_York','minmove':1,'pricescale':100,'type':'stock'},'bars':[{k:v for k,v in bar().items() if k!='close_time'}]}
    result=asyncio.run(repair_day(store,'2026-11-20',tmp_path/'unused',fetch=fetch,max_requests=1,now=now))
    assert result['requests']==1
    series=store.series()[0]['id']
    assert store.bars(series,OPEN,OPEN+60,as_of=now-1)==[]
    assert asyncio.run(repair_day(store,'2026-11-20',tmp_path/'unused',fetch=fetch,now=now+1))['requests']==0
    assert len(calls)==1


def test_schema_metadata_updates_keep_series_identity_and_import_watermarks_are_monotonic(tmp_path):
    store=ResearchStore(tmp_path/'db.sqlite3')
    record=save(store)
    first=store.series()[0]['id']
    save(store,series={**SERIES,'pricescale':1000})
    assert [s['id'] for s in store.series()]==[first]
    store.apply(record,source='journal',offset=100,signature='same')
    store.apply(record,source='journal',offset=50,signature='same')
    assert store.cursor('journal')[0]==100


def test_verified_sealed_transport_copy_can_be_removed_without_touching_audit(tmp_path):
    archive=ResearchArchive(tmp_path/'research');archive.spool.mkdir(parents=True)
    original=tmp_path/'live_audit.sqlite3';original.write_bytes(b'preserve')
    file=archive.spool/'sealed.ndjson'
    file.write_text(json.dumps(envelope('configuration',{'series':[]},observed_at=OPEN))+'\n')
    (archive.spool/'sealed.ndjson.sealed').write_text(json.dumps({'bytes':file.stat().st_size}))
    archive.pump()
    assert not file.exists()
    assert original.read_bytes()==b'preserve'


def test_version_capture_failure_remains_visible_after_a_healthy_poll(tmp_path,monkeypatch):
    async def run():
        archive=ResearchArchive(tmp_path/'research')
        monkeypatch.setattr(archive,'capture_versions',lambda:(_ for _ in ()).throw(ValueError('missing source')))
        archive.start()
        await asyncio.sleep(.03)
        assert 'missing source' in archive.status()['version_error']
        await archive.stop()
    asyncio.run(run())
