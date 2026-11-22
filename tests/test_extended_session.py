"""Extended indicator inputs do not expand execution windows or reverse exits."""
from datetime import datetime

from execution.candidate_policy import confirmed_candidate_st, trailing_reference
from execution.entry_admission import candidate_snapshot, check_candidate
from .test_candidate_entry_admission import frame, market, NOW, STAMP, SYM


def test_extended_st_is_accepted_only_as_a_fresh_confirmed_matching_reference():
    data = {'market_1m': {'bar_c': {'time': STAMP-60}}, 'bar': {'close': 100},
            'candidate_st_1m_c': {'version': 1, 'time': STAMP-60, 'price': 99,
                'direction': 1, 'atr_period': 10, 'multiplier': 3,
                'interval_seconds': 60, 'session': 'extended'}}
    assert confirmed_candidate_st(data, NOW)['price'] == 99
    assert trailing_reference(data, 'st_1m_10_3', 'long', NOW) is not None
    assert trailing_reference(data, 'st_1m_10_3', 'short', NOW) is None
    assert confirmed_candidate_st(data, NOW+106) is None
    data['market_1m']['bar_c']['time'] -= 60
    assert confirmed_candidate_st(data, NOW) is None


def test_preheated_indicators_cannot_grant_a_premarket_or_afterhours_entry():
    for stamp in ['2026-11-19T09:29:30-05:00', '2026-11-19T16:01:00-05:00']:
        now = datetime.fromisoformat(stamp).timestamp()
        result = check_candidate(candidate_snapshot(frame(), now, market()), SYM, 'long')
        assert not result['ok'] and '常规盘' in result['reason']
