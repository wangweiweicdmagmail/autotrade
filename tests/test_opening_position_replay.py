"""Opening-position experiments use only temporary research data and never execution state."""
import asyncio
from datetime import datetime
import json
import pytest

from engine.opening_position_replay import opening_position_inputs, rank_opening_positions, save_report
from engine.research_archive import envelope
from engine.research_calendar import ET
from engine.research_store import ResearchStore

DAY='2026-10-07';OPEN=int(datetime.strptime(DAY+' 09:30','%Y-%m-%d %H:%M').replace(tzinfo=ET).timestamp())
SYMBOL='NASDAQ:TEST'


def candles():
    return [{'time':OPEN+i*60,'close_time':OPEN+(i+1)*60,'open':100,'high':101 if i<15 else 104,'low':99,
             'close':100 if i<15 else 103,'volume':100} for i in range(30)]


def seed(store, symbol=SYMBOL, rows=None, interval='1m'):
    rows=candles() if rows is None else rows
    if interval=='5m':
        original=rows;rows=[]
        for i in range(0,len(original),5):
            group=original[i:i+5]
            if len(group)<5:continue
            rows.append({'time':group[0]['time'],'close_time':group[-1]['close_time'],'open':group[0]['open'],
                         'high':max(b['high'] for b in group),'low':min(b['low'] for b in group),
                         'close':group[-1]['close'],'volume':sum(b['volume'] for b in group)})
    series={'provider':'tradingview','symbol':symbol,'resolved_symbol':symbol,'session':'regular','adjustment':'splits',
            'interval':interval,'timezone':'America/New_York','minmove':1,'pricescale':100,'type':'stock'}
    store.apply(envelope('bars',{'series':series,'rows':rows,'received_at':OPEN+3600},observed_at=OPEN+3600))


def test_input_cutoff_excludes_all_future_minutes_and_preserves_cached_full_day(tmp_path):
    store=ResearchStore(tmp_path/'archive.sqlite3');seed(store);seed(store,interval='5m')
    inputs=opening_position_inputs(store,DAY,'09:50',[SYMBOL])
    data=inputs['datasets'][SYMBOL]
    assert len(data['bars'])==20 and len(data['five_bars'])==4
    assert data['bars'][-1]['time']==OPEN+1140 and data['five_bars'][-1]['time']==OPEN+900
    assert all(b['time']+60<=inputs['as_of'] for b in data['bars'])
    assert len(store.bars(store.series(SYMBOL,'1m')[0]['id'],OPEN,OPEN+3600))==30
    assert inputs['sources'][SYMBOL]['input_digest']


def test_pine_experiment_is_separate_from_actual_ranking_history_and_reports_are_immutable(tmp_path):
    store=ResearchStore(tmp_path/'archive.sqlite3');seed(store);seed(store,interval='5m')
    result=asyncio.run(rank_opening_positions(store,DAY,'09:50',[SYMBOL]))
    assert result['complete'] and result['ranks'][0]['coordinate']==3
    assert result['ranks'][0]['cross_timeframe_checked']
    assert result['ranks'][0]['opening_midpoint']==100 and result['source_bundle_id']
    assert result['indicator_source_sha256'] and result['pinets_version']=='0.13.1'
    assert result['vwap_baseline']['complete']
    assert len(result['long_top10'])==1 and not result['short_top10']
    assert result['side_counts']=={'long':1,'short':0,'neutral':0}
    with store.read() as db:
        assert db.execute('SELECT COUNT(*) FROM ranking_batches').fetchone()[0]==0
        assert db.execute("SELECT COUNT(*) FROM definition_versions WHERE json_extract(payload,'$.kind')='opening_position_ranking_experiment'").fetchone()[0]==1
    with store.read() as db:
        payload=json.loads(db.execute('SELECT payload FROM definition_versions WHERE id=?',(result['source_bundle_id'],)).fetchone()[0])
        assert 'float midpoint' in payload['sources']['indicators/opening-range-position.pine']
        assert payload['model']=='opening15_midpoint_half_position_research_v4'
    report=save_report(result,tmp_path/'reports')
    assert '09:50 ET' in __import__('pathlib').Path(report).read_text()
    assert '多头前十' in __import__('pathlib').Path(report).read_text() and '空头前十' in __import__('pathlib').Path(report).read_text()
    assert save_report(result,tmp_path/'reports')==report
    __import__('pathlib').Path(report).write_text('another session changed this')
    with pytest.raises(ValueError,match='禁止覆盖'):save_report(result,tmp_path/'reports')


def test_no_five_minute_input_is_needed_for_pine_but_missing_opening_or_target_cannot_be_fabricated(tmp_path):
    store=ResearchStore(tmp_path/'archive.sqlite3');seed(store)
    result=asyncio.run(rank_opening_positions(store,DAY,'09:50',[SYMBOL]))
    assert result['complete'] and not result['ranks'][0]['cross_timeframe_checked']
    seed(store,'NASDAQ:PARTIAL',[b for b in candles() if b['time']!=OPEN+180])
    result=asyncio.run(rank_opening_positions(store,DAY,'09:50',['NASDAQ:PARTIAL']))
    assert not result['complete'] and not result['ranks']
    assert result['missing'][0]['reason']=='opening15_minute_gap'
    seed(store,'NASDAQ:OLD',[b for b in candles() if b['time']<OPEN+1140])
    result=asyncio.run(rank_opening_positions(store,DAY,'09:50',['NASDAQ:OLD']))
    assert not result['ranks'] and result['missing'][0]['reason']=='target_minute_missing'


def test_missing_series_and_invalid_times_are_explicit_without_any_network_fallback(tmp_path):
    store=ResearchStore(tmp_path/'archive.sqlite3')
    result=asyncio.run(rank_opening_positions(store,DAY,'09:50',[SYMBOL]))
    assert not result['complete'] and result['missing'][0]['reason']=='missing_regular_1m_series'
    for clock in ('09:40','16:01','08:50'):
        with pytest.raises(ValueError):opening_position_inputs(store,DAY,clock,[SYMBOL])
    with pytest.raises(ValueError):opening_position_inputs(store,'2026-10-10','09:50',[SYMBOL])


def test_default_session_indices_are_not_ranked_as_equities(tmp_path):
    store=ResearchStore(tmp_path/'archive.sqlite3');seed(store)
    result=asyncio.run(rank_opening_positions(store,DAY,'09:50',[SYMBOL,'CAPITALCOM:NAS100'],{'CAPITALCOM:NAS100'}))
    assert result['complete'] and len(result['ranks'])==1
    assert not result['missing']


def test_missing_reference_etf_does_not_block_equity_research_coverage(tmp_path):
    store=ResearchStore(tmp_path/'archive.sqlite3');seed(store)
    result=asyncio.run(rank_opening_positions(store,DAY,'09:50',[SYMBOL,'AMEX:SPY']))
    assert result['complete'] and result['eligible']==1
    assert result['vwap_baseline']['complete'] and not result['missing']
