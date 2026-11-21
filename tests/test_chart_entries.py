from console.services.chart_entries import entry_markers
from console.services.tv_paper_sync import configured_paper_account, TvPaperSync
from types import SimpleNamespace
from unittest.mock import Mock
import config


def test_only_current_confirmed_positions_supply_cost_and_stop_segments():
    from datetime import datetime
    from zoneinfo import ZoneInfo
    stamp=datetime(2026,11,19,10,0,7,tzinfo=ZoneInfo('America/New_York')).timestamp()
    end=datetime(2026,11,19,16,0,tzinfo=ZoneInfo('America/New_York')).timestamp()
    base=dict(lot_id='a',symbol='NASDAQ:NVDA',side='long',state='ACTIVE',entry_cumulative_qty=20,
              filled_qty=10,avg_fill=100,stop_px=98,entry_time=stamp)
    rows=[base,{**base,'lot_id':'unfilled','filled_qty':0}, {**base,'lot_id':'closed','state':'CLOSED'},
          {**base,'lot_id':'unknown','avg_fill':0}, {**base,'lot_id':'future','entry_time':stamp+2000},
          {**base,'lot_id':'no-stop','stop_px':0}]
    result=entry_markers(rows,now=stamp+100)
    assert result==[dict(id='a',symbol='NASDAQ:NVDA',side='long',time=stamp,price=100,stop=98,qty=10,session_end=end)]
    assert entry_markers([{**base,'stop_px':100}],now=stamp+100)[0]['stop']==100


def test_paper_sync_stays_disabled_with_existing_account_binding_and_env(tmp_path,monkeypatch):
    (tmp_path/'tv_paper_sync_account').write_text('existing')
    monkeypatch.setenv('TV_PAPER_SYNC_ACCOUNT','override')
    monkeypatch.setattr(config,'TV_PAPER_SYNC_ENABLED',False)
    assert configured_paper_account(tmp_path)==''
    context=SimpleNamespace(paths=SimpleNamespace(data_dir=tmp_path),tasks=SimpleNamespace(start=Mock()))
    sync=TvPaperSync(context,'existing');sync.start()
    context.tasks.start.assert_not_called()
    assert (tmp_path/'tv_paper_sync_account').read_text()=='existing'
