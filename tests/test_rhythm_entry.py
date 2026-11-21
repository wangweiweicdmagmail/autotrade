"""Six-panel manual market intent; all market/cache/order state is isolated."""
from copy import deepcopy
from unittest.mock import Mock
import pytest
from fastapi import HTTPException
from console.schemas import LimitStopOrderRequest
from .conftest import run
from .test_manual_market_direction import manual_market


@pytest.fixture
def rhythm_case(manual_market, monkeypatch):
    c = manual_market
    minute = c.data['bar']['time']
    c.data['price_observed_at'] = minute + 5
    c.frame = {'status': 'ready', 'tracks': [{'symbol': 'NASDAQ:NVDA', 'aligned': True,
        'is_beta': False, 'issue': None,
        'points': [[minute-60, 99, 98, 2, 100, 101, 99, 100, 1.5, 'outside_progress', 1]]}]}
    monkeypatch.setattr(c.app.research._market_rhythm, 'read', lambda *a, **k: deepcopy(c.frame))
    c.trend = Mock(side_effect=AssertionError('rhythm must use its own recommended direction'))
    monkeypatch.setattr(c.app.trading, '_check_trend_filter', c.trend)
    def submit(side='long', **changes):
        values = dict(symbol='NASDAQ:NVDA', side=side, limit_type='market', entry_mode='rhythm_atr',
                      limit_px=999, stop_px=1, qty=99999)
        values.update(changes)
        return run(c.app.trading.api_place_limit_stop(LimitStopOrderRequest(**values)))
    c.submit = submit
    return c


@pytest.mark.parametrize('direction,side,stop', [(1,'long',94.99),(-1,'short',105.01)])
def test_rhythm_reprices_with_the_common_five_minute_market_stop(rhythm_case, direction, side, stop):
    c = rhythm_case
    c.frame['tracks'][0]['points'][0][10] = direction
    assert c.submit(side)['ok']
    kw = c.place.await_args.kwargs
    assert (kw['side'], kw['limit_px'], kw['stop_px']) == (side,100,stop)
    assert kw['entry_order_type'] == 'MKT' and kw['limit_type'] == 'market'
    assert kw['qty'] == 199 and kw['risk_usd'] == 1000
    assert kw['manual_entry'] is True
    assert kw['atr_mult'] == 2
    assert kw['signal_snapshot']['stop_source'] == 'market_2atr_outside_bar'
    assert kw['signal_snapshot']['stop_atr_interval_seconds'] == 300
    assert kw['signal_snapshot']['stop_atr_multiple'] == 2
    assert kw['signal_snapshot']['stop_atr_value'] == 2
    assert kw['signal_snapshot']['stop_bar_time'] == c.data['bar']['time']
    assert 'stop_sigma_multiple' not in kw['signal_snapshot']
    assert kw['signal_snapshot']['rhythm_entry']['sigma'] == 2
    c.trend.assert_not_called()


@pytest.mark.parametrize('fault', ['direction','gap','alignment','old','atr','bar_time','bar_prices','price','beta','missing'])
def test_rhythm_rejects_invalid_perception_or_price_before_any_order(rhythm_case, fault):
    c = rhythm_case
    row = c.frame['tracks'][0]
    if fault == 'direction': row['points'][0][10] = -1
    elif fault == 'gap': row['issue'] = 'minute_gap'
    elif fault == 'alignment': row['aligned'] = False
    elif fault == 'old': row['points'][0][0] -= 180
    elif fault == 'atr': c.data['ts_c']['atr'] = None
    elif fault == 'bar_time': c.data['bar']['time'] -= 300
    elif fault == 'bar_prices': c.data['bar']['low'] = 101
    elif fault == 'price': c.data['price_observed_at'] -= 46
    elif fault == 'beta': row['is_beta'] = True
    elif fault == 'missing': c.frame['status'] = 'waiting'
    with pytest.raises(HTTPException): c.submit()
    c.place.assert_not_awaited()


@pytest.mark.parametrize('change', [{'force':True},{'limit_type':'ema'},
    {'market_direction_mode':'manual','range_direction_confirmed':True}])
def test_rhythm_cannot_mix_entry_modes(rhythm_case,change):
    with pytest.raises(HTTPException): rhythm_case.submit(**change)
    rhythm_case.place.assert_not_awaited()


def test_rhythm_preserves_common_order_manager_risk_rejection(rhythm_case):
    rhythm_case.place.return_value=(None,'多头风险敞口超预算1100')
    with pytest.raises(HTTPException,match='多头风险敞口'): rhythm_case.submit()
    assert rhythm_case.place.await_count == 1


@pytest.mark.parametrize('direction,side,stop', [(1,'long',96),(-1,'short',104)])
@pytest.mark.parametrize('sigma', [.001, 200])
def test_rhythm_uses_confirmed_five_minute_atr_independently_of_vwap_width(rhythm_case, direction, side, stop, sigma):
    c = rhythm_case
    c.data['bar'].update(low=99, high=101)
    c.data['ts'] = {'atr': 50}
    c.data['vwap_pullback_c']['atr'] = 100
    c.app.settings._settings['stop_atr_mult'] = .5
    c.frame['tracks'][0]['points'][0][10] = direction
    c.frame['tracks'][0]['points'][0][3] = sigma
    assert c.submit(side)['ok']
    kw = c.place.await_args.kwargs
    assert kw['stop_px'] == stop and kw['qty'] == 250
    assert kw['atr_mult'] == 2
    assert kw['signal_snapshot']['rhythm_entry']['stop_atr_value'] == 2


def test_legacy_rhythm_mode_cannot_reinstate_sigma_stop(rhythm_case):
    assert rhythm_case.submit(entry_mode='rhythm_sigma')['ok']
    kw = rhythm_case.place.await_args.kwargs
    assert kw['stop_px'] == 94.99
    assert kw['signal_snapshot']['stop_source'] == 'market_2atr_outside_bar'


@pytest.mark.parametrize('direction,side,stop',[(1,'long',98),(-1,'short',102)])
def test_manual_sigma_choice_is_repriced_from_confirmed_vwap_and_1000_dollar_risk(rhythm_case,direction,side,stop):
    c=rhythm_case;c.frame['tracks'][0]['points'][0][10]=direction
    assert c.submit(side,manual_stop_mode='vwap_sigma')['ok']
    kw=c.place.await_args.kwargs
    assert kw['stop_px']==stop and kw['qty']==500 and kw['risk_usd']==1000
    assert kw['signal_snapshot']['stop_source']=='manual_vwap_sigma'
    assert kw['signal_snapshot']['stop_sigma_value']==2


@pytest.mark.parametrize('mode,stop',[('atr_2',94.99),('vwap_sigma',98)])
def test_all_manual_entries_take_candidate_direction_instead_of_st(manual_market,monkeypatch,mode,stop):
    c=manual_market
    c.data['vwap_pullback_c']['direction']=-1
    c.data['ts_c']['direction']=-1
    c.app.order_mgr.entry_admission=lambda *args:{'ok':True,'evidence':{'sigma':2,'side':'long','rank':1}}
    trend=Mock(side_effect=AssertionError('ST must not override a confirmed candidate'))
    monkeypatch.setattr(c.app.trading,'_check_trend_filter',trend)
    assert c.submit('long',market_direction_mode='candidate',range_direction_confirmed=False,manual_stop_mode=mode)['ok']
    kw=c.place.await_args.kwargs
    assert kw['stop_px']==stop and kw['side']=='long'
    assert kw['signal_snapshot']['candidate_admission']['rank']==1
    assert kw['signal_snapshot']['market_direction_mode']=='candidate'
    trend.assert_not_called()


@pytest.mark.parametrize('sigma',[0,None,float('nan')])
def test_manual_sigma_never_uses_invalid_width_as_a_stop(manual_market,sigma):
    c=manual_market;c.app.order_mgr.entry_admission=lambda *args:{'ok':True,'evidence':{'sigma':sigma}}
    with pytest.raises(HTTPException,match='标准差未就绪'):
        c.submit('long',market_direction_mode='candidate',range_direction_confirmed=False,manual_stop_mode='vwap_sigma')
    c.place.assert_not_awaited()


def test_sigma_market_stop_does_not_require_atr(manual_market):
    c=manual_market;c.data['ts_c']['atr']=None
    c.app.order_mgr.entry_admission=lambda *args:{'ok':True,'evidence':{'sigma':2}}
    assert c.submit('long',market_direction_mode='candidate',range_direction_confirmed=False,manual_stop_mode='vwap_sigma')['ok']
    assert c.place.await_args.kwargs['stop_px']==98


@pytest.mark.parametrize('mode,stop',[('atr_2',94.99),('vwap_sigma',98)])
@pytest.mark.parametrize('filled',[0,1])
def test_pending_retest_repricing_preserves_the_selected_manual_stop_policy(manual_market,monkeypatch,mode,stop,filled):
    import asyncio
    from unittest.mock import AsyncMock
    from console.services import trading
    c=manual_market;c.data['bar_c2']={'time':c.data['bar']['time']-600,'high':100,'low':99}
    lot={'lot_id':'pending','symbol':'NASDAQ:NVDA','side':'long','state':'PENDING','limit_type':'retest',
         'signal_snapshot':{'manual_stop_mode':mode,'stop_sigma_value':2},'init_risk_usd':500,
         'qty':10,'filled_qty':filled,'stop_px':90}
    monkeypatch.setattr(c.app.order_mgr,'list_lots',lambda **kwargs:[lot])
    reprice=AsyncMock(return_value=(True,'unchanged'))
    monkeypatch.setattr(c.app.order_mgr,'reprice_retest_lot',reprice)
    ticks=[]
    async def tick(*args):
        if ticks:raise asyncio.CancelledError
        ticks.append(True)
    monkeypatch.setattr(trading.asyncio,'sleep',tick)
    with pytest.raises(asyncio.CancelledError):run(c.app.trading._retest_limit_reprice_loop())
    assert reprice.await_count==1
    assert reprice.await_args.args[2]==(90 if filled else stop)


@pytest.mark.parametrize('direction,side,stop', [(1,'long',94.99),(-1,'short',105.01)])
def test_opening_rhythm_follows_the_common_missing_atr_fallback(rhythm_case, monkeypatch, direction, side, stop):
    from datetime import datetime
    from console.services import trading
    now = datetime(2026, 11, 17, 9, 35, 5, tzinfo=trading.ET)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return now.astimezone(tz)

    monkeypatch.setattr(trading, 'datetime', Clock)
    c = rhythm_case
    c.data['bar']['time'] = now.timestamp() - 5
    c.data['price_observed_at'] = now.timestamp()
    c.data['ts_c']['atr'] = None
    c.frame['tracks'][0]['points'][0][0] = now.timestamp() - 65
    c.frame['tracks'][0]['points'][0][10] = direction
    assert c.submit(side)['ok']
    assert c.place.await_args.kwargs['stop_px'] == stop
