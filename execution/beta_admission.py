"""Classify confirmed index facts for entry only; no indicator calculation or broker access."""
import math

from domain.contracts import finite_number
from execution.confirmed_td import confirmed_td
from execution.confirmed_minute import TD_SOURCE

BETA_SYMBOLS = ('CAPITALCOM:NAS100', 'CAPITALCOM:SPX500')
BETA_FILTER_VERSION = 1
BETA_TD_RULE = 'any_index_same_sign'


def beta_entry_filter(frame, now, *, absolute_required=True, td_snapshot=None):
    absolute_required = absolute_required is not False
    stamp = finite_number(frame.get('as_of'))
    accepted = frame.get('ranking_snapshot') or {}
    tracks = frame.get('tracks') or []
    rows = []
    for symbol in BETA_SYMBOLS:
        matches = [r for r in tracks if r.get('symbol') == symbol]
        track = matches[0] if len(matches) == 1 else {}
        points = track.get('points') or []
        point = points[-1] if points and isinstance(points[-1], list) and len(points[-1]) == 11 else [None]*11
        price, vwap, sigma = (finite_number(point[i]) for i in (7, 2, 3))
        bar_time = finite_number(point[0])
        ema = track.get('beta_ema9') if isinstance(track.get('beta_ema9'), dict) else {}
        value = finite_number(ema.get('value'))
        cutoff = math.floor(stamp / 300)*300 if stamp is not None else None
        minute_ready = bool(stamp is not None and stamp % 60 == 0 and 0 <= now-stamp <= 120
            and track.get('is_beta') is True and track.get('aligned') is True and not track.get('issue')
            and bar_time is not None and bar_time+60 == stamp
            and price is not None and price > 0)
        ready = bool(minute_ready and all(v is not None and v > 0 for v in (vwap, sigma, value))
            and vwap > sigma
            and type(ema.get('version')) is int and ema['version'] == 1
            and type(ema.get('period')) is int and ema['period'] == 9
            and type(ema.get('interval_seconds')) is int and ema['interval_seconds'] == 300
            and finite_number(ema.get('known_at')) == cutoff
            and finite_number(ema.get('time')) == cutoff-300)
        upper = finite_number(vwap+sigma) if vwap is not None and sigma is not None else None
        lower = finite_number(vwap-sigma) if vwap is not None and sigma is not None else None
        ready = ready and all(v is not None and math.isfinite(v) for v in (upper, lower))
        direction = (1 if price > upper and price > value else -1 if price < lower and price < value else 0) if ready else None
        td = confirmed_td(track, bar_time) if minute_ready else None
        fact = (td_snapshot.get('rows') or {}).get(symbol,{}) if td_snapshot is not None else None
        if fact is not None:
            td = fact.get('count') if fact.get('known') else None
        rows.append({'symbol': symbol, 'ready': ready, 'direction': direction,
                     'price': price, 'vwap': vwap, 'sigma': sigma, 'upper': upper, 'lower': lower,
                     'ema9': value, 'bar_time': bar_time, 'confirmed_at': stamp,
                     'ema9_time': finite_number(ema.get('time')), 'ema9_known_at': finite_number(ema.get('known_at')),
                     'minute_ready': minute_ready, 'td_count': td,
                     'td_ready':fact.get('ready') is True if fact is not None else td is not None,
                     'td_fact':fact})
    absolute_ready = all(row['ready'] for row in rows)
    td_ready = all((td_snapshot is not None or row['minute_ready']) and row['td_ready'] for row in rows)
    ready = td_ready and (not absolute_required or absolute_ready)
    direction = rows[0]['direction'] if absolute_ready and rows[0]['direction'] == rows[1]['direction'] else 0
    missing = '、'.join(row['symbol'].split(':')[-1] for row in rows if not row['ready'])
    absolute_pending = f'等待{missing}同批一分钟收盘轨道及确认五分钟EMA9' if not absolute_ready else ''
    td_detail = '、'.join(row['symbol'].split(':')[-1]+' TD '+(f'{row["td_count"]:+d}' if row['td_ready'] else '待齐') for row in rows)
    absolute_long = not absolute_required or absolute_ready and direction != -1
    absolute_short = not absolute_required or absolute_ready and direction != 1
    td_long = td_ready and any(row['td_count'] > 0 for row in rows)
    td_short = td_ready and any(row['td_count'] < 0 for row in rows)
    absolute_long_reason = (absolute_pending or ('NAS100、SPX500同时绝对空头，禁止做多' if direction == -1 else '')) if absolute_required else ''
    absolute_short_reason = (absolute_pending or ('NAS100、SPX500同时绝对多头，禁止做空' if direction == 1 else '')) if absolute_required else ''
    td_pending = '；'.join(row['symbol'].split(':')[-1]+'：'+(row['td_fact'].get('reason') or '确认TD未就绪')
                          for row in rows if td_snapshot is not None and not row['td_ready'])
    td_long_reason = '' if td_long else td_pending or td_detail+'；做多要求至少一个指数已确认TD > 0'
    td_short_reason = '' if td_short else td_pending or td_detail+'；做空要求至少一个指数已确认TD < 0'
    return {'version': BETA_FILTER_VERSION, 'rule': 'both_indices_absolute',
            'price_source': 'confirmed_1m_close', 'ema_period': 9, 'ema_interval_seconds': 300,
            'as_of': stamp, 'snapshot_id': accepted.get('id'), 'ready': ready, 'direction': direction,
            'absolute_required': absolute_required, 'absolute_ready': absolute_ready,
            'absolute_long_allowed': absolute_long, 'absolute_short_allowed': absolute_short,
            'absolute_long_reason': absolute_long_reason, 'absolute_short_reason': absolute_short_reason,
            'td_required': True, 'td_rule': BETA_TD_RULE, 'td_ready': td_ready,
            'td_source':TD_SOURCE if td_snapshot is not None else 'ranking_snapshot',
            'td_long_allowed': td_long, 'td_short_allowed': td_short,
            'td_long_reason': td_long_reason, 'td_short_reason': td_short_reason,
            'long_allowed': absolute_long and td_long, 'short_allowed': absolute_short and td_short,
            'long_reason': absolute_long_reason or td_long_reason,
            'short_reason': absolute_short_reason or td_short_reason,
            'rows': rows}
