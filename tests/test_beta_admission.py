"""Index facts remain observable but do not gate the stock-only TD entry policy."""
from copy import deepcopy
from unittest.mock import AsyncMock

import pytest
import config

from execution.entry_admission import candidate_snapshot, check_candidate
from execution.beta_admission import BETA_SYMBOLS, beta_entry_filter
from execution.confirmed_minute import build_snapshot
from .test_candidate_entry_admission import frame, market, beta_tracks, NOW, STAMP, SYM
from .test_risk_exposure import make_mgr
from .conftest import run


@pytest.fixture
def absolute_filter(monkeypatch):
    monkeypatch.setattr(config,'BETA_ABSOLUTE_ENTRY_FILTER_ENABLED',True)


def context(directions=(0, 0), tds=(1, 1), side='long'):
    data = frame()
    sign = 1 if side == 'long' else -1
    data['ranks'][0]['direction_sign'] = sign
    data['tracks'][0]['points'][0][10] = sign
    data['tracks'][0]['points'][0][8] = 2*sign
    data['tracks'][0]['td']['counts'][0][1] = sign
    data['tracks'][0]['points'][0][2] = 99 if sign == 1 else 101
    data['tracks'][1:] = beta_tracks(directions=directions)
    for row, td in zip(data['tracks'][1:], tds):
        row['td']['counts'][0][1] = td
    live=market(sign)
    for symbol,td in zip(BETA_SYMBOLS,tds):live[symbol]['rhythm_confirmed_1m']['td_count']=td
    return data,live


@pytest.mark.parametrize('directions,side,allowed', [
    ((1, 1), 'long', True), ((1, 1), 'short', False),
    ((-1, -1), 'long', False), ((-1, -1), 'short', True),
    ((1, -1), 'long', True), ((1, -1), 'short', True),
    ((1, 0), 'short', True), ((-1, 0), 'long', True),
    ((0, 0), 'long', True), ((0, 0), 'short', True),
])
def test_absolute_index_states_are_observations_only(directions, side, allowed,absolute_filter):
    td = 1 if side == 'long' else -1
    data, live = context(directions, (td, td), side)
    out = candidate_snapshot(data, NOW, live)
    result = check_candidate(out, SYM, side)
    assert result['ok']
    assert 'beta_evidence' not in result['evidence']
    assert out['beta_filter_required'] is False
    assert beta_entry_filter(data,NOW)['absolute_'+side+'_allowed'] is allowed


@pytest.mark.parametrize('side,tds,allowed', [
    ('long', (1, 1), True), ('long', (9, 2), True),
    ('long', (0, 1), True), ('long', (1, 0), True), ('long', (1, -1), True), ('long', (-1, 1), True),
    ('long', (0, 0), False), ('long', (0, -1), False), ('long', (-1, -1), False),
    ('short', (-1, -1), True), ('short', (-8, -2), True),
    ('short', (0, -1), True), ('short', (-1, 0), True), ('short', (-1, 1), True), ('short', (1, -1), True),
    ('short', (0, 0), False), ('short', (0, 1), False), ('short', (1, 1), False),
])
def test_benchmark_td_sign_does_not_override_stock_td(side, tds, allowed):
    data, live = context(tds=tds, side=side)
    result = check_candidate(candidate_snapshot(data, NOW, live), SYM, side)
    assert result['ok']
    assert result['evidence']['td_count']==(1 if side=='long' else -1)


@pytest.mark.parametrize('side,directions',[('long',(-1,-1)),('short',(1,1))])
def test_stock_td_is_not_blocked_even_by_opposing_absolute_indices(side,directions,absolute_filter):
    data,live=context(directions=directions,tds=(1,-1),side=side)
    result=check_candidate(candidate_snapshot(data,NOW,live),SYM,side)
    assert result['ok']


def test_index_observation_rule_is_not_an_entry_contract():
    data,live=context()
    snapshot=candidate_snapshot(data,NOW,live)
    assert beta_entry_filter(data,NOW)['td_rule']=='any_index_same_sign'
    snapshot['beta_filter']={'td_rule':'both_indices_same_sign'}
    assert check_candidate(snapshot,SYM,'long')['ok']


@pytest.mark.parametrize('price,ema', [(102, 100), (103, 103), (98, 100), (97, 97)])
def test_touching_outer_band_or_ema_is_not_absolute(price, ema):
    data, live = context()
    for row in data['tracks'][1:]:
        row['points'][0][7] = price
        row['beta_ema9']['value'] = ema
    out = candidate_snapshot(data, NOW, live)
    assert beta_entry_filter(data,NOW)['direction'] == 0
    assert check_candidate(out, SYM, 'long')['ok']


@pytest.mark.parametrize('fault', ['missing', 'duplicate', 'provider', 'gap', 'stale_minute', 'sigma',
    'ema_missing', 'ema_wrong_bar', 'ema_future', 'ema_period', 'ema_boolean_version',
    'td_missing', 'td_future', 'td_boolean_version', 'td_boolean', 'td_wrong_comparison'])
def test_missing_stale_or_wrong_index_facts_do_not_block_valid_stock_entry(fault,absolute_filter):
    data, live = context()
    row = data['tracks'][1]
    if fault == 'missing': data['tracks'].pop(1)
    elif fault == 'duplicate': data['tracks'].append(deepcopy(row))
    elif fault == 'provider': row['symbol'] = 'OTHER:NAS100'
    elif fault == 'gap': row['issue'] = 'minute_gap'
    elif fault == 'stale_minute': row['points'][0][0] -= 60
    elif fault == 'sigma': row['points'][0][3] = 0
    elif fault == 'ema_missing': row.pop('beta_ema9')
    elif fault == 'ema_wrong_bar': row['beta_ema9']['time'] -= 300
    elif fault == 'ema_future': row['beta_ema9']['known_at'] += 300
    elif fault == 'ema_period': row['beta_ema9']['period'] = 7
    elif fault == 'ema_boolean_version': row['beta_ema9']['version'] = True
    elif fault == 'td_missing': live[BETA_SYMBOLS[0]].pop('rhythm_confirmed_1m')
    elif fault == 'td_future': live[BETA_SYMBOLS[0]]['rhythm_confirmed_1m']['time']+=60
    elif fault == 'td_boolean_version': live[BETA_SYMBOLS[0]]['rhythm_confirmed_1m']['version']=True
    elif fault == 'td_boolean': live[BETA_SYMBOLS[0]]['rhythm_confirmed_1m']['td_count']=True
    else: live[BETA_SYMBOLS[0]]['rhythm_confirmed_1m']['comparison_bars']=3
    assert check_candidate(candidate_snapshot(data, NOW, live), SYM, 'long')['ok']


@pytest.mark.parametrize('strategy', [None, 'ema7_autotrade_v2', 'golden_strategy', 'opening_vwap_3'])
def test_manual_auto_and_signal_entries_do_not_depend_on_benchmark_td(tmp_path, strategy):
    data, live = context(tds=(-1, 0))
    mgr = make_mgr(tmp_path)
    mgr._gw.place_market_with_stop = AsyncMock(return_value={'ok':True,'dry_run':True,'entry_order_id':1,'stop_order_id':2})
    mgr.entry_admission = lambda symbol, side, now: check_candidate(candidate_snapshot(data, NOW, live), symbol, side)
    lot, reason = run(mgr.place_limit_stop_lot(SYM, 'long', 10, 100, 99, entry_order_type='MKT',
        signal_snapshot={'strategy_id': strategy} if strategy else {}, manual_entry=not strategy))
    assert lot is not None,reason
    mgr._gw.place_market_with_stop.assert_awaited_once()


@pytest.mark.parametrize('stage', ['risk_wait', 'after_intent'])
@pytest.mark.parametrize('change', ['absolute', 'td'])
def test_index_change_during_stock_entry_does_not_create_a_new_gate(tmp_path, stage, change,absolute_filter):
    data, live = context()
    data['as_of'] = STAMP-60
    data['ranking_snapshot']['as_of'] = STAMP-60
    data['tracks'][0]['points'][0][0] -= 60
    data['tracks'][0]['td']['counts'][0][0] -= 60
    data['tracks'][1:] = beta_tracks(STAMP-60)
    if change == 'td':data['tracks'][1]['td']['counts'][0][1]=-1
    mgr = make_mgr(tmp_path)
    mgr._gw.place_market_with_stop = AsyncMock(return_value={'ok':True,'dry_run':True,'entry_order_id':1,'stop_order_id':2})
    mgr.entry_admission = lambda symbol, side, now: check_candidate(candidate_snapshot(data, NOW, live), symbol, side)
    def update():
        next_frame, _ = context(directions=(-1,-1) if change == 'absolute' else (0,0),
                                tds=(-1,0) if change == 'td' else (1,1))
        next_frame['ranking_snapshot']['id'] = 'next-accepted-minute'
        data.clear(); data.update(next_frame)
        if change=='td':
            for symbol,td in zip(BETA_SYMBOLS,(-1,0)):live[symbol]['rhythm_confirmed_1m']['td_count']=td
    if stage == 'risk_wait':
        async def pnl(): update(); return 0
        mgr._gw.realized_pnl = pnl
    else:
        save = mgr._save
        def changed(): save(); update()
        mgr._save = changed
    lot, reason = run(mgr.place_limit_stop_lot(SYM, 'long', 10, 100, 99, entry_order_type='MKT'))
    assert lot is not None,reason
    mgr._gw.place_market_with_stop.assert_awaited_once()
    assert lot.signal_snapshot['candidate_admission']['entry_policy']=='stock_vwap_side_v1'


def test_benchmark_facts_freeze_with_accepted_ranking_and_survive_restart(tmp_path):
    from console.services.rhythm import MarketRhythmReader
    from .test_ranking_snapshot import raw, put
    path = tmp_path/'rhythm.json'
    data = raw(); put(path, data)
    reader = MarketRhythmReader(path)
    first = reader.read(now=NOW)
    old = beta_entry_filter(first,NOW,td_snapshot=build_snapshot(market(),NOW))
    for symbol in BETA_SYMBOLS:
        data['series'][symbol]['td']['counts'][0][1] = -4
        data['series'][symbol]['beta_ema9']['value'] = 120
    put(path, data)
    live=market();live[BETA_SYMBOLS[0]]['rhythm_confirmed_1m']['td_count']=-4
    updated=beta_entry_filter(reader.read(now=NOW+1),NOW+1,td_snapshot=build_snapshot(live,NOW+1))
    assert updated['snapshot_id']==old['snapshot_id'] and updated['rows'][0]['td_count']==-4
    assert reader.read(now=NOW+1)['tracks'][0]['td']!=data['series'][BETA_SYMBOLS[0]]['td']
    restored=beta_entry_filter(MarketRhythmReader(path).read(now=NOW+1),NOW+1,td_snapshot=build_snapshot(live,NOW+1))
    assert restored==updated


@pytest.mark.parametrize('side',['long','short'])
@pytest.mark.parametrize('fault',['opposite_absolute','ema_missing','ema_future','sigma_missing','vwap_missing'])
def test_paused_absolute_filter_does_not_gate_td_on_index_price_or_ema(side,fault):
    assert config.BETA_ABSOLUTE_ENTRY_FILTER_ENABLED is False
    sign=1 if side=='long' else -1
    data,live=context(directions=(-sign,-sign),tds=(sign,0),side=side)
    for row in data['tracks'][1:]:
        if fault=='ema_missing':row.pop('beta_ema9')
        elif fault=='ema_future':row['beta_ema9']['known_at']+=300
        elif fault=='sigma_missing':row['points'][0][3]=None
        elif fault=='vwap_missing':row['points'][0][2]=None
    snapshot=candidate_snapshot(data,NOW,live)
    assert snapshot['beta_filter_required'] is False and 'beta_filter' not in snapshot
    assert check_candidate(snapshot,SYM,side)['ok']


@pytest.mark.parametrize('fault',['missing','gap','stale_minute','td_missing','zero','opposite'])
def test_index_td_quality_is_not_a_stock_entry_dependency(fault):
    data,live=context(tds=(1,0));row=live[BETA_SYMBOLS[0]]['rhythm_confirmed_1m']
    if fault=='missing':live.pop(BETA_SYMBOLS[0])
    elif fault=='gap':row['issue']='minute_gap'
    elif fault=='stale_minute':row['time']-=60
    elif fault=='td_missing':row.pop('td_count')
    elif fault=='zero':row['td_count']=0
    else:row['td_count']=-1
    assert check_candidate(candidate_snapshot(data,NOW,live),SYM,'long')['ok']
