"""Opt-in position exit rules. Consume confirmed ingestion outputs; no indicators."""
from datetime import datetime
from decimal import Decimal

from domain.contracts import finite_number
from execution.candidate_policy import ET, breakeven_due_at, confirmed_minute_end, protection_price
from execution.confirmed_minute import observation

STATE_KEY = 'intelligent_exit'


def valid_options(value):
    return (isinstance(value, dict) and type(value.get('version')) is int and value['version'] == 1
            and type(value.get('enabled')) is bool and type(value.get('breakeven_minutes')) is int
            and 1 <= value['breakeven_minutes'] <= 120)


def confirmed_td(data, now):
    if 'rhythm_confirmed_1m' in data:
        fact=observation(data,now)
        return {'bar_time':fact['bar_time'],'count':fact['count'],'evidence':fact} if fact['ready'] else None
    row = data.get('td_exit_1m_c') or {}
    stamp = finite_number(row.get('time'))
    minute = (data.get('market_1m') or {}).get('bar_c') or {}
    if (type(row.get('version')) is not int or row['version'] != 1
            or row.get('comparison_bars') != 4 or row.get('interval_seconds') != 60
            or row.get('session') not in ('regular', 'extended', '24h')
            or type(row.get('count')) is not int or stamp is None or stamp % 60
            or not 0 <= now-(stamp+60) <= 105
            or finite_number(minute.get('time')) != stamp):
        return None
    return {'bar_time': stamp, 'count': row['count']}


def plan_exit(lot, data, options, now):
    """One deterministic action; R is per-share distance from actual cost/initial stop.

    Partial exits do not change R. One half per reverse-TD episode, with >=4
    taking priority. Missing TD must not disable the independent timed safeguard.
    """
    waiting = {'action': 'waiting'}
    if not valid_options(options) or not options['enabled']:
        return waiting
    at = datetime.fromtimestamp(now, ET)
    if at.weekday() >= 5 or not 570 <= at.hour*60+at.minute < 960:
        return waiting
    price, cost = protection_price(data, now), finite_number(lot.avg_fill)
    if price is None or cost is None or cost <= 0 or lot.side not in ('long', 'short'):
        return waiting
    state = lot.signal_snapshot.get(STATE_KEY) or {}
    entry = state.get('entry_time') or lot.entry_broker_time or lot.entry_time
    if not entry or not 0 < entry <= now:
        return waiting
    common = {'price': price, 'cost': cost, 'effective_options': dict(options)}
    td = confirmed_td(data, now)
    if td and td['bar_time']+60 > entry and td['bar_time'] > state.get('td_consumed_bar', 0):
        reverse = -td['count'] if lot.side == 'long' else td['count']
        episode = str(int(td['bar_time']-(abs(td['count'])-1)*60))
        info = {**common, 'td': td, 'episode': episode}
        if reverse >= 4:
            return {**info, 'action': 'close', 'exit_reason': 'intelligent_td_reverse_4'}
        if reverse == 3 and state.get('half_episode') != episode:
            initial = finite_number(lot.init_stop_px)
            if initial is not None and ((lot.side == 'long' and 0 < initial < cost)
                                       or (lot.side == 'short' and initial > cost)):
                distance = abs(Decimal(str(cost))-Decimal(str(initial)))
                pnl = (Decimal(str(price))-Decimal(str(cost)))*(1 if lot.side == 'long' else -1)
                r = pnl/distance
                return {**info, 'profit_r': float(r), 'initial_stop': initial,
                        'action': 'close' if r <= Decimal('0.3') else 'half',
                        'exit_reason': 'intelligent_td_reverse_3'}
    end = confirmed_minute_end(data, now)
    due = breakeven_due_at(entry, options['breakeven_minutes'])
    if not state.get('timed_breakeven_done') and end is not None and end >= due and now >= due:
        stop = round(cost, 2)
        protected = lot.stop_px >= stop if lot.side == 'long' else 0 < lot.stop_px <= stop
        legal = 0 < stop < price if lot.side == 'long' else stop > price > 0
        return {**common, 'action': 'protected' if protected else 'stop' if legal else 'close',
                'exit_reason': 'intelligent_timed_breakeven', 'target_stop': stop, 'due_at': due,
                'breakeven_minutes': options['breakeven_minutes']}
    return waiting
