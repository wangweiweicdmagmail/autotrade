"""Close/VWAP direction admission; selected-stock facts and isolated broker stubs."""
from unittest.mock import AsyncMock
import pytest
from console.schemas import CandidateOrderOptions
from execution.entry_admission import candidate_snapshot,check_candidate,ENTRY_POLICY
from execution.candidate_policy import POLICY_KEY
from .test_candidate_entry_admission import frame,market,NOW,SYM,BETA_SYMBOLS
from .test_risk_exposure import make_mgr
from .conftest import run


@pytest.mark.parametrize('vwap,side',[(99,'long'),(101,'short'),(100,None)])
@pytest.mark.parametrize('td',[None,True,0,3,-4])
def test_only_latest_close_vs_vwap_sets_direction(vwap,side,td):
    prices=market()
    for index in BETA_SYMBOLS:prices.pop(index)
    prices[SYM]['ts']={}
    prices[SYM]['rhythm_confirmed_1m'].update(td_count=td,vwap=vwap)
    snapshot=candidate_snapshot(frame(),NOW,prices)
    assert snapshot['entry_policy']==ENTRY_POLICY and snapshot['data_contract_version']==4
    assert snapshot['td_required'] is snapshot['ema9_required'] is snapshot['beta_filter_required'] is False
    assert snapshot['vwap_direction_required'] is True
    for wanted in ('long','short'):
        result=check_candidate(snapshot,SYM,wanted)
        assert result['ok'] is (wanted==side)
        if result['ok']:assert result['evidence']['direction_fact']['close']==100


@pytest.mark.parametrize('fault',['missing','forming','stale','vwap_absent','vwap_bool','close_zero','quote_absent','quote_stale','quote_future'])
def test_selected_stock_integrity_still_required(fault):
    prices=market();data=prices[SYM];raw=data['rhythm_confirmed_1m']
    if fault=='missing':data.pop('rhythm_confirmed_1m')
    elif fault=='forming':raw['time']+=60
    elif fault=='stale':raw['time']-=60
    elif fault=='vwap_absent':raw['vwap']=None
    elif fault=='vwap_bool':raw['vwap']=True
    elif fault=='close_zero':raw['close']=0
    elif fault=='quote_absent':data.pop('bar')
    elif fault=='quote_stale':data['price_observed_at']=NOW-46
    else:data['price_observed_at']=NOW+1
    assert not check_candidate(candidate_snapshot(frame(),NOW,prices),SYM,'long')['ok']


def test_new_vwap_side_can_differ_from_frozen_ranking_without_waiting_for_full_batch():
    snapshot=candidate_snapshot(frame(),NOW,market(-1))
    assert snapshot['rows'][0]['direction']==-1
    assert check_candidate(snapshot,SYM,'short')['ok']
    assert not check_candidate(snapshot,SYM,'long')['ok']


@pytest.mark.parametrize('fault',['old_version','old_policy','old_flags','forged_sign','forged_fact','quote_flag'])
def test_old_or_inconsistent_contracts_cannot_bypass_direction(fault):
    data=candidate_snapshot(frame(),NOW,market())
    if fault=='old_version':data['data_contract_version']=3
    elif fault=='old_policy':data['entry_policy']='stock_td_sign_v1'
    elif fault=='old_flags':data['beta_filter_required']=True
    elif fault=='forged_sign':data['rows'][0]['direction']=-1
    elif fault=='forged_fact':data['rows'][0]['direction_fact']['vwap']=101
    else:data['rows'][0]['quote_ready']=False
    assert not check_candidate(data,SYM,'long')['ok']


@pytest.mark.parametrize('side',['long','short'])
def test_old_client_band_filter_and_trailing_st_do_not_add_entry_gates(tmp_path,side):
    mgr=make_mgr(tmp_path);prices=market(1 if side=='long' else -1)
    prices[SYM].pop('candidate_st_1m_c',None)
    mgr.entry_admission=lambda symbol,direction,now:check_candidate(candidate_snapshot(frame(),NOW,prices),symbol,direction)
    mgr._gw.place_market_with_stop=AsyncMock(return_value={'ok':True,'dry_run':True,'entry_order_id':1,'stop_order_id':2})
    options=CandidateOrderOptions(vwap_outside_only=True,trailing_enabled=True,trailing_source='st_1m_10_3').dict()
    lot,reason=run(mgr.place_limit_stop_lot(SYM,side,10,100,99 if side=='long' else 101,
        limit_type='market',entry_order_type='MKT',manual_entry=True,signal_snapshot={POLICY_KEY:options}))
    assert lot is not None,reason
    policy=lot.signal_snapshot[POLICY_KEY]
    assert policy['vwap_outside_only'] is False
    assert policy['trailing_enabled'] and policy['trailing_source']=='st_1m_10_3'
    mgr._gw.place_market_with_stop.assert_awaited_once()
