"""Regular-session / normalized-position migration uses temporary inputs only."""
from copy import deepcopy
import hashlib
import json
from datetime import datetime
import pytest
import config
from console.services.rhythm import MarketRhythmReader,_encoded
from engine.local_pine_history import LocalPineHistory,LocalPineHistoryError,_validate_dataset
from execution.entry_admission import candidate_snapshot,check_candidate
from .test_ranking_snapshot import raw,put
from .test_candidate_entry_admission import NOW,STAMP,SYM,market,frame
from .conftest import run


def test_current_defaults_are_regular_for_both_stock_resolutions():
    assert config.TV_MARKET_SESSION=='regular'
    from engine.realtime_collector import RealtimeCollector
    assert RealtimeCollector([SYM]).market_session=='regular'
    config.validate_config()


@pytest.mark.parametrize('model,version,session',[('ema9_15m_v3',3,'extended'),('vwap_z_v4',4,'regular'),('vwap_close_z_v5',5,'regular'),('vwap_open_ema5_v6',6,'regular')])
def test_old_accepted_extended_score_is_retained_on_disk_but_never_reused_as_regular(tmp_path,model,version,session):
    path=tmp_path/'rhythm.json';cache=path.with_name('rhythm.accepted.json')
    legacy=raw();legacy['scoring'].update(model=model,version=version,stock_session=session)
    encoded=_encoded(legacy);cache.write_text(_encoded({'version':1,'id':hashlib.sha256(encoded.encode()).hexdigest(),'accepted_at':NOW,'frame':legacy}))
    saved=cache.read_bytes();put(path,legacy)
    reader=MarketRhythmReader(path)
    state=reader.read(now=NOW);assert state['status']=='waiting' and not state['ranks']
    assert cache.read_bytes()==saved
    current=raw();put(path,current)
    ready=reader.read(now=NOW)
    assert ready['status']=='ready' and ready['scoring']['model']=='opening15_midpoint_v7'
    assert ready['scoring']['stock_session']=='regular'
    assert ready['ranking_snapshot']['id']!=json.loads(saved)['id']


def test_wrong_session_cannot_be_accepted_even_with_the_new_score_model(tmp_path):
    path=tmp_path/'rhythm.json';data=raw();data['scoring']['stock_session']='extended';put(path,data)
    state=MarketRhythmReader(path).read(now=NOW)
    assert state['status']=='waiting' and state['ranks']==[]


@pytest.mark.parametrize('z',[.2,-.3,2,-3])
def test_position_candidate_can_enter_by_td_even_before_any_breakout_episode(z):
    data=frame();data['scoring']={'model':'opening15_midpoint_v7','version':7,'stock_session':'regular'}
    sign=1 if z>0 else -1
    data['ranks'][0].update(direction_sign=sign,opening_position=z,z=z,score=abs(z))
    data['tracks'][0]['points'][0][8]=z;data['tracks'][0]['points'][0][10]=0
    live=market(sign)
    result=check_candidate(candidate_snapshot(data,NOW,live),SYM,'long' if sign==1 else 'short')
    assert result['ok'],result
    assert result['evidence']['ranking_evidence']['z']==z


@pytest.mark.parametrize('symbol,session',[(SYM,'regular'),('CAPITALCOM:NAS100','default')])
def test_local_history_does_not_borrow_old_extended_cache(tmp_path,monkeypatch,symbol,session):
    from engine import local_pine_history as module
    loader=LocalPineHistory(tmp_path/'history.sqlite3')
    cutoff=int(datetime.fromisoformat('2026-11-20T16:00:00-05:00').timestamp())
    legacy=json.dumps({'metadata':{'session':'extended'},'bars':[]})
    loader._sql('INSERT INTO tv_5m_extended_splits VALUES(?,?,?,?)',(symbol,cutoff,600,legacy),write=True)
    calls=[]
    async def fetch(script,*args,**kwargs):
        calls.append((script,args));raise LocalPineHistoryError('isolated-fetch-stop')
    monkeypatch.setattr(module,'_node',fetch)
    with pytest.raises(LocalPineHistoryError,match='isolated-fetch-stop'):
        run(loader.load(symbol,'2026-11-20',600,datetime.fromisoformat('2026-11-23T17:00:00-05:00')))
    assert calls[0][0]=='tv_backtest_bars.js' and calls[0][1][-1]==session
    assert loader._sql('SELECT dataset FROM tv_5m_extended_splits WHERE symbol=?',(symbol,))[0]==legacy


def test_regular_history_rejects_a_mismatched_session():
    dataset={'metadata':{'session':'extended','timezone':'America/New_York','minmove':1,'pricescale':100},
      'bars':[{'time':300,'open':100,'high':101,'low':99,'close':100,'volume':10}]}
    with pytest.raises(LocalPineHistoryError,match='时段'):_validate_dataset(dataset,600,1,'regular')
    dataset['metadata']['session']='regular'
    assert _validate_dataset(dataset,600,1,'regular')['metadata']['session']=='regular'


@pytest.mark.parametrize('z',[None,True,float('nan'),0])
def test_v4_rank_cannot_borrow_a_legacy_episode_direction_when_z_is_invalid(z):
    data=frame();data['scoring']={'model':'opening15_midpoint_v7','version':7,'stock_session':'regular'}
    data['ranks'][0]['opening_position']=z
    data['tracks'][0]['points'][0][8]=z
    assert not check_candidate(candidate_snapshot(data,NOW,market()),SYM,'long')['ok']
