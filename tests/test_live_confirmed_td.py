"""Independent confirmed observations, frozen membership, and isolated preflight/exit."""
from copy import deepcopy
from unittest.mock import AsyncMock
import json
from pathlib import Path
import shutil
import subprocess

import pytest

from execution.confirmed_minute import build_snapshot, observation
from execution.entry_admission import candidate_snapshot, check_candidate
from console.services.rhythm import MarketRhythmReader
from .confirmed_minute_helpers import minute_data
from .test_candidate_entry_admission import NOW, STAMP, SYM, BETA_SYMBOLS, market
from .test_ranking_snapshot import raw, put, OTHER
from .test_risk_exposure import make_mgr
from .conftest import run


def accepted(tmp_path):
    path=tmp_path/'rhythm.json'
    put(path,raw(STAMP-600))
    return MarketRhythmReader(path),path


@pytest.mark.parametrize('ranking',['trend'])
def test_old_complete_membership_remains_usable_with_new_individual_confirmation(tmp_path,ranking):
    reader,path=accepted(tmp_path);frame=reader.read(now=NOW)
    saved=reader.accepted_path.read_bytes();prices=market()
    prices[SYM]['rhythm_confirmed_1m'].update(td_count=3,vwap=98,sigma=3)
    put(path,raw(STAMP,complete=False))
    frame=reader.read(now=NOW)
    snapshot=candidate_snapshot(frame,NOW,prices,ranking=ranking)
    result=check_candidate(snapshot,SYM,'long')
    assert result['ok'],result
    row=result['evidence']
    assert row['td_count']==3 and row['vwap']==98 and row['sigma']==3
    assert row['confirmed_at']==STAMP and row['ranking_confirmed_at']==STAMP-600
    assert row['td_fact']==build_snapshot(prices,NOW)['rows'][SYM]
    assert snapshot['ranking_age_seconds']>120 and frame['batch_progress']['waiting']
    assert reader.accepted_path.read_bytes()==saved
    if ranking=='trend':assert not check_candidate(snapshot,OTHER,'long')['ok']


def test_broadcast_and_both_admission_views_share_exact_observations(console_app,tmp_path,monkeypatch):
    reader,path=accepted(tmp_path);frame=reader.read(now=NOW);calls=[]
    monkeypatch.setattr(console_app.research._market_rhythm,'read',lambda **kw:deepcopy(frame))
    prices=market();monkeypatch.setattr(console_app.collector,'latest',lambda:(calls.append(1) or deepcopy(prices)))
    bundle=console_app.research.entry_candidate_snapshots(NOW)
    assert len(calls)==1
    for mode in ('trend',):
        row=next(r for r in bundle[mode]['rows'] if r['symbol']==SYM)
        assert row['td_fact']==bundle['confirmed_td']['rows'][SYM]
        assert row['direction_fact']==bundle['confirmed_vwap']['rows'][SYM]
    assert 'td' not in bundle


@pytest.mark.parametrize('symbol',[SYM,*BETA_SYMBOLS])
@pytest.mark.parametrize('fault',['missing','stale_quote','old_bar','forming_td','bool_count','wrong_period','future_observed','future_publish','gap'])
def test_selected_stock_must_be_fresh_but_indices_are_display_only(tmp_path,symbol,fault):
    reader,_=accepted(tmp_path);prices=market();data=prices[symbol]
    row=data['rhythm_confirmed_1m']
    if fault=='missing':data.pop('rhythm_confirmed_1m')
    elif fault=='stale_quote':data['rhythm_price_1m']['price_observed_at']=NOW-46
    elif fault=='old_bar':row['time']-=60
    elif fault=='forming_td':row['time']+=60
    elif fault=='bool_count':row['td_count']=True
    elif fault=='wrong_period':row['interval_seconds']=300
    elif fault=='future_observed':data['rhythm_price_1m']['price_observed_at']=NOW+1
    elif fault=='future_publish':row['published_at']=NOW+1
    else:row['issue']='minute_gap'
    result=check_candidate(candidate_snapshot(reader.read(now=NOW),NOW,prices),SYM,'long')
    assert result['ok'] is (symbol in BETA_SYMBOLS or fault=='bool_count')


def test_unrelated_missing_symbol_does_not_block_and_live_beta_td_can_override_old_td(tmp_path):
    reader,_=accepted(tmp_path);prices=market()
    prices[OTHER]={}
    # Old frozen TD is +1 for both. Current TD, rather than the old ranking value, is decisive.
    for symbol in BETA_SYMBOLS:prices[symbol]['rhythm_confirmed_1m']['td_count']=-1
    frame=reader.read(now=NOW)
    assert check_candidate(candidate_snapshot(frame,NOW,prices),SYM,'long')['ok']
    prices[SYM]['rhythm_confirmed_1m']['td_count']=-2
    prices[SYM]['rhythm_confirmed_1m']['vwap']=101
    td=candidate_snapshot(frame,NOW,prices)
    assert check_candidate(td,SYM,'short')['ok']
    assert not check_candidate(td,SYM,'long')['ok']


@pytest.mark.parametrize('symbol',[SYM,*BETA_SYMBOLS])
@pytest.mark.parametrize('issue',['missing_session_anchor','missing_open','ema_warmup'])
def test_td_does_not_wait_for_vwap_perception_readiness(tmp_path,symbol,issue):
    reader,_=accepted(tmp_path);prices=market()
    prices[symbol]['rhythm_confirmed_1m'].update(issue=issue,vwap=None,sigma=None)
    snapshot=candidate_snapshot(reader.read(now=NOW),NOW,prices,ranking='trend')
    assert check_candidate(snapshot,SYM,'long')['ok'] is (symbol in BETA_SYMBOLS)
    # TD readouts/exit can remain valid while a missing VWAP blocks new entries.
    assert build_snapshot(prices,NOW)['rows'][symbol]['ready']


@pytest.mark.parametrize('stage',['risk_wait','after_intent'])
def test_a_live_td_turn_is_rechecked_without_waiting_for_a_new_ranking(tmp_path,stage):
    reader,_=accepted(tmp_path);frame=reader.read(now=NOW);prices=market();mgr=make_mgr(tmp_path/'manager')
    mgr._gw.place_market_with_stop=AsyncMock(return_value={})
    mgr.entry_admission=lambda symbol,side,now:check_candidate(candidate_snapshot(frame,NOW,prices),symbol,side)
    def turn():prices[SYM]['rhythm_confirmed_1m']['vwap']=101
    if stage=='risk_wait':
        async def pnl():turn();return 0
        mgr._gw.realized_pnl=pnl
    else:
        save=mgr._save
        def changed():save();turn()
        mgr._save=changed
    lot,reason=run(mgr.place_limit_stop_lot(SYM,'long',10,100,99,entry_order_type='MKT'))
    assert lot is None and 'VWAP' in reason
    mgr._gw.place_market_with_stop.assert_not_awaited()


def test_same_day_and_market_window_boundaries_still_apply(tmp_path):
    reader,_=accepted(tmp_path);frame=reader.read(now=NOW)
    for at in (NOW+86400,STAMP-3600):
        assert not candidate_snapshot(frame,at,market(now=at))['ready']


@pytest.mark.skipif(shutil.which('node') is None,reason='Node is required for ingestion contract validation')
@pytest.mark.parametrize('received_offset',[.01,-.132,-.528])
def test_real_node_output_passes_the_shared_python_contract_without_a_ranking_flush(received_offset):
    script="""
const {createMarketRhythm}=require('./service/market_rhythm');
const at=Number(process.argv[1]),open=at-45*60;
const bar=(time,close)=>({time,open:close,high:close+.1,low:close-.1,close,volume:100});
const warm=Array.from({length:40},(_,i)=>bar(open-86400+i*60,100+i*.1));
const closed=Array.from({length:45},(_,i)=>bar(open+i*60,100+i*.2));
let clock=at+.02;const feed=createMarketRhythm({symbols:['NASDAQ:NVDA','NASDAQ:MISSING'],marketSession:'extended',clock:()=>clock});
feed.observe('NASDAQ:NVDA',[...warm,...closed,bar(at,150)],at+.01,null,[],at+Number(process.argv[2]));
clock=at+.03;process.stdout.write(JSON.stringify(feed.confirmedState('NASDAQ:NVDA',clock)));
"""
    out=subprocess.run(['node','-e',script,str(STAMP),str(received_offset)],cwd=Path(__file__).resolve().parents[1],
                       check=True,text=True,capture_output=True)
    raw_state=json.loads(out.stdout)
    data=minute_data(STAMP);data['rhythm_confirmed_1m']=raw_state
    fact=observation(data,NOW)
    assert fact['ready'],fact
    assert fact['bar_time']==STAMP-60 and fact['count']>0 and fact['latency_seconds']==5
    assert fact['received_at']<fact['computed_at']<fact['published_at']<NOW


@pytest.mark.parametrize('require_td',[True,False])
def test_raw_receipt_before_boundary_does_not_delay_an_already_confirmed_value(require_td):
    data=minute_data(STAMP)
    data['rhythm_confirmed_1m'].update(received_at=STAMP-.132,computed_at=STAMP+.008,published_at=STAMP+.89)
    fact=observation(data,NOW,require_td=require_td)
    assert fact['known'] and fact['ready'],fact
    assert fact['received_at']==STAMP-.132
    assert fact['computed_at']>=fact['confirmed_at']


def test_candidate_can_open_from_fresh_vwap_with_an_early_raw_receipt(tmp_path):
    reader,_=accepted(tmp_path);prices=market()
    prices[SYM]['rhythm_confirmed_1m'].update(received_at=STAMP-.132,computed_at=STAMP+.008,published_at=STAMP+.89)
    result=check_candidate(candidate_snapshot(reader.read(now=NOW),NOW,prices),SYM,'long')
    assert result['ok'],result
    assert result['evidence']['direction_fact']['received_at']==STAMP-.132


@pytest.mark.parametrize('td',[None,False,1.5,-4,0,4])
def test_entry_direction_only_uses_vwap_and_never_requires_td_or_its_confirmation(tmp_path,td):
    reader,_=accepted(tmp_path);prices=market()
    raw=prices[SYM]['rhythm_confirmed_1m'];raw['td_count']=td;raw.pop('comparison_bars',None)
    snapshot=candidate_snapshot(reader.read(now=NOW),NOW,prices)
    assert snapshot['td_required'] is False
    assert check_candidate(snapshot,SYM,'long')['ok']
    assert not check_candidate(snapshot,SYM,'short')['ok']


@pytest.mark.parametrize('require_td',[True,False])
@pytest.mark.parametrize('fault',['before_bar','before_close','receive_after_compute','publish_before_compute','future_publish','missing_stage'])
def test_allowing_early_receipt_does_not_allow_unconfirmed_or_invalid_processing_times(require_td,fault):
    data=minute_data(STAMP);raw=data['rhythm_confirmed_1m']
    raw.update(received_at=STAMP-.132,computed_at=STAMP+.008,published_at=STAMP+.89)
    if fault=='before_bar':raw['received_at']=STAMP-60.01
    elif fault=='before_close':raw['computed_at']=STAMP-.1
    elif fault=='receive_after_compute':raw['received_at']=STAMP+1
    elif fault=='publish_before_compute':raw['published_at']=STAMP+.007
    elif fault=='future_publish':raw['published_at']=NOW+1
    else:raw['received_at']=None
    fact=observation(data,NOW,require_td=require_td)
    assert fact['known'] and not fact['ready'] and fact['reason']=='确认数据处理时间无效',fact


@pytest.mark.parametrize('side,count,price,action',[('long',-3,101.5,'close'),('long',-3,102,'half'),
    ('short',3,98.5,'close'),('short',3,98,'half'),('long',-4,102,'close')])
def test_intelligent_exit_uses_the_same_new_confirmation_without_waiting_for_pinets_bar_cache(tmp_path,side,count,price,action):
    from .test_intelligent_exit import case,NOW as EXIT_NOW
    from execution.intelligent_exit import plan_exit
    mgr,lot,gw,data,options=case(tmp_path,side=side,price=price,count=0)
    at=int(EXIT_NOW//60)*60
    data.update(minute_data(at,count,close=price))
    data['market_1m']['bar_c']['time']-=60  # The old worker output must not delay a newer confirmed TD.
    result=plan_exit(lot,data,options,EXIT_NOW)
    assert result['action']==action
    fact=observation(data,EXIT_NOW)
    assert result['td']['evidence']==fact and result['td']['bar_time']==at-60
    assert result['td']['evidence']['latency_seconds']<60


@pytest.mark.parametrize('fault',['missing','forming','quote_stale'])
def test_new_exit_channel_cannot_borrow_a_valid_legacy_td_when_new_data_is_invalid(tmp_path,fault):
    from .test_intelligent_exit import case,NOW as EXIT_NOW
    from execution.intelligent_exit import plan_exit
    mgr,lot,gw,data,options=case(tmp_path,count=-4)
    data.update(minute_data(int(EXIT_NOW//60)*60,-4))
    if fault=='missing':data['rhythm_confirmed_1m']=None
    elif fault=='forming':data['rhythm_confirmed_1m']['time']+=60
    else:data['rhythm_price_1m']['price_observed_at']=EXIT_NOW-46
    assert plan_exit(lot,data,options,EXIT_NOW)['action']=='waiting'


def test_cross_symbol_payload_is_not_accepted_as_the_selected_symbol(tmp_path):
    reader,_=accepted(tmp_path);prices=market()
    prices[SYM]['rhythm_confirmed_1m']['symbol']=OTHER
    result=check_candidate(candidate_snapshot(reader.read(now=NOW),NOW,prices),SYM,'long')
    assert not result['ok'] and '标的不匹配' in result['reason']
