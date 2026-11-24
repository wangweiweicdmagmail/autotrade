"""VWAP manual MKT/LMT pricing and protected execution; no live I/O."""
from copy import deepcopy
from datetime import datetime
from unittest.mock import AsyncMock
import pytest
from fastapi import HTTPException
from console.schemas import LimitStopOrderRequest,CandidateOrderOptions
from execution.manual_entry import vwap_limit_price
from execution.entry_admission import candidate_snapshot,check_candidate,ET
from .test_candidate_entry_admission import frame,market,NOW,SYM,STAMP
from .test_manual_market_direction import manual_market
from .test_risk_exposure import make_mgr
from .confirmed_minute_helpers import minute_data
from .conftest import run


@pytest.mark.parametrize('side,level,expected',[
    ('long','vwap',100),('long','upper',102),('short','vwap',100),('short','lower',98)])
def test_confirmed_price_mapping(side,level,expected):
    assert vwap_limit_price(side,level,100,2)==expected


@pytest.mark.parametrize('side,level,mid,sigma',[
    ('long','lower',100,2),('short','upper',100,2),('long','unknown',100,2),
    ('long','upper',100,None),('short','lower',100,True),('long','vwap',True,2),
    ('short','lower',1,2),('long','upper',100,float('nan'))])
def test_invalid_or_wrong_direction_limits_rejected(side,level,mid,sigma):
    with pytest.raises(ValueError):vwap_limit_price(side,level,mid,sigma)


def configure(c,monkeypatch,side):
    sign=1 if side=='long' else -1;at=c.data['bar']['time'];now=at+5
    frozen=frame();frozen['day']=datetime.fromtimestamp(at,ET).date().isoformat();frozen['as_of']=at
    frozen['ranking_snapshot'].update(day=frozen['day'],as_of=at)
    frozen['tracks'][0]['points'][0][0]=at-60
    c.data.update(price_observed_at=now,**minute_data(at,-sign,vwap=99 if sign==1 else 101))
    monkeypatch.setattr(c.app.research._market_rhythm,'read',lambda **kw:deepcopy(frozen))
    c.app.order_mgr.entry_admission=c.app.research.check_candidate_entry


@pytest.mark.parametrize('side,level,reference',[
    ('long','vwap',99),('long','upper',101),('short','vwap',101),('short','lower',99)])
@pytest.mark.parametrize('stop_mode',['atr_2','vwap_sigma'])
def test_http_recomputes_limit_stop_and_size_ignoring_browser_prices(manual_market,monkeypatch,side,level,reference,stop_mode):
    c=manual_market;configure(c,monkeypatch,side)
    request=LimitStopOrderRequest(symbol=SYM,side=side,limit_type='vwap_limit',entry_mode='rhythm_atr',
        vwap_limit_level=level,manual_stop_mode=stop_mode,limit_px=999,stop_px=1,candidate_order=CandidateOrderOptions())
    assert run(c.app.trading.api_place_limit_stop(request))['ok']
    sent=c.place.await_args.kwargs
    expected_stop=(94.99 if side=='long' else 105.01) if stop_mode=='atr_2' else reference+(-2 if side=='long' else 2)
    assert sent['entry_order_type']=='LMT' and sent['limit_type']=='vwap_limit'
    assert sent['limit_px']==reference and sent['stop_px']==expected_stop
    assert sent['qty']==int(1000/abs(reference-expected_stop))
    assert sent['risk_ref_px']==0  # LMT sizing uses its fixed price, not the market price.
    intent=sent['signal_snapshot']['rhythm_entry']
    assert intent['limit_level']==level and intent['entry_reference']==reference
    assert intent['direction_fact']['close']==100 and intent['direction_fact']['vwap']==(99 if side=='long' else 101)
    assert sent['signal_snapshot']['candidate_order_policy']['vwap_outside_only'] is False


@pytest.mark.parametrize('fault',['wrong_side','no_sigma','no_vwap','stale','not_candidate'])
def test_http_never_sends_when_limit_or_admission_is_invalid(manual_market,monkeypatch,fault):
    c=manual_market;configure(c,monkeypatch,'long')
    raw=c.data['rhythm_confirmed_1m'];level='upper'
    if fault=='wrong_side':level='lower'
    elif fault=='no_sigma':raw['sigma']=None
    elif fault=='no_vwap':raw['vwap']=None
    elif fault=='stale':raw['time']-=60
    else:c.app.order_mgr.entry_admission=lambda *a,**kw:{'ok':False,'reason':'not in current top ten'}
    request=LimitStopOrderRequest(symbol=SYM,side='long',limit_type='vwap_limit',entry_mode='rhythm_atr',
        vwap_limit_level=level,limit_px=99,stop_px=95)
    with pytest.raises(HTTPException):run(c.app.trading.api_place_limit_stop(request))
    c.place.assert_not_awaited()


@pytest.mark.parametrize('side',['long','short'])
def test_manager_sends_lmt_with_stop_and_never_market_or_oca(tmp_path,side):
    mgr=make_mgr(tmp_path,dry_run=False);sign=1 if side=='long' else -1
    mgr.entry_admission=lambda symbol,direction,now:check_candidate(candidate_snapshot(frame(),NOW,market(sign)),symbol,direction)
    mgr._gw.place_limit_with_stop=AsyncMock(return_value={'ok':True,'entry_order_id':11,'stop_order_id':12})
    mgr._gw.place_market_with_stop=AsyncMock();mgr._gw.place_midpoint_oca=AsyncMock()
    reference=99 if side=='long' else 101;stop=reference-2*sign
    lot,reason=run(mgr.place_limit_stop_lot(SYM,side,10,reference,stop,
        entry_order_type='LMT',limit_type='vwap_limit',manual_entry=True,signal_snapshot={'candidate_ranking':'trend','candidate_order_policy':CandidateOrderOptions().dict()}))
    assert lot is not None,reason
    assert lot.state=='PENDING' and lot.entry_order_type=='LMT'
    assert mgr.risk_exposure()['total']==20
    mgr._gw.place_limit_with_stop.assert_awaited_once()
    mgr._gw.place_market_with_stop.assert_not_awaited();mgr._gw.place_midpoint_oca.assert_not_awaited()


@pytest.mark.parametrize('stage',['risk_wait','after_intent'])
def test_limit_submission_rechecks_direction_without_resubmitting_or_leaving_an_intent(tmp_path,stage):
    mgr=make_mgr(tmp_path,dry_run=False);prices=market()
    mgr._gw.place_limit_with_stop=AsyncMock()
    mgr.entry_admission=lambda symbol,side,now:check_candidate(candidate_snapshot(frame(),NOW,prices),symbol,side)
    def turn():prices[SYM]['rhythm_confirmed_1m']['vwap']=101
    if stage=='risk_wait':
        async def pnl():turn();return 0
        mgr._gw.realized_pnl=pnl
    else:
        save=mgr._save
        def changed():save();turn()
        mgr._save=changed
    lot,reason=run(mgr.place_limit_stop_lot(SYM,'long',10,99,97,
        entry_order_type='LMT',limit_type='vwap_limit',manual_entry=True))
    assert lot is None and 'VWAP' in reason
    mgr._gw.place_limit_with_stop.assert_not_awaited()
    assert all(not row.entry_submission_pending for row in mgr._lots.values())

@pytest.mark.parametrize('first_type,second_type',[('market','vwap_limit'),('vwap_limit','market'),('vwap_limit','vwap_limit')])
def test_market_and_limit_allow_distinct_additions_but_keep_exact_request_deduplication(tmp_path,first_type,second_type):
    mgr=make_mgr(tmp_path,dry_run=False)
    mgr.entry_admission=lambda symbol,side,now:check_candidate(candidate_snapshot(frame(),NOW,market()),symbol,side)
    mgr._gw.place_market_with_stop=AsyncMock(return_value={'ok':True,'entry_order_id':11,'stop_order_id':12})
    mgr._gw.place_limit_with_stop=AsyncMock(return_value={'ok':True,'entry_order_id':21,'stop_order_id':22})
    first,reason=run(mgr.place_limit_stop_lot(SYM,'long',10,100,98,manual_entry=True,limit_type=first_type,
        entry_order_type='MKT' if first_type=='market' else 'LMT'))
    assert first is not None,reason
    again,reason=run(mgr.place_limit_stop_lot(SYM,'long',10,99,97,manual_entry=True,limit_type=second_type,
        entry_order_type='MKT' if second_type=='market' else 'LMT'))
    assert again is not None and again.lot_id != first.lot_id,reason
    duplicate,reason=run(mgr.place_limit_stop_lot(SYM,'long',10,100,98,manual_entry=True,limit_type=first_type,
        entry_order_type='MKT' if first_type=='market' else 'LMT'))
    assert duplicate is None and '重复下单' in reason
    assert mgr._gw.place_market_with_stop.await_count+mgr._gw.place_limit_with_stop.await_count==2


def test_new_confirmation_between_initial_check_and_pricing_cannot_mix_band_or_stop_facts(manual_market,monkeypatch):
    c=manual_market;configure(c,monkeypatch,'long')
    check=c.app.order_mgr.entry_admission
    def advance(*args,**kwargs):
        result=check(*args,**kwargs)
        c.data['rhythm_confirmed_1m'].update(vwap=98,sigma=3)
        return result
    c.app.order_mgr.entry_admission=advance
    request=LimitStopOrderRequest(symbol=SYM,side='long',limit_type='vwap_limit',entry_mode='rhythm_atr',
        vwap_limit_level='vwap',manual_stop_mode='vwap_sigma',limit_px=100,stop_px=98)
    assert run(c.app.trading.api_place_limit_stop(request))['ok']
    sent=c.place.await_args.kwargs
    assert sent['limit_px']==98 and sent['stop_px']==95 and sent['qty']==333
    frozen=sent['signal_snapshot']['rhythm_entry'];admitted=sent['signal_snapshot']['candidate_admission']
    assert frozen['sigma']==admitted['sigma']==3
    assert frozen['direction_fact']['id']==admitted['direction_fact']['id']
