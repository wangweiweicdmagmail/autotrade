"""Read confirmed ranking facts; never calculate indicators or manage exits."""
from datetime import datetime
from zoneinfo import ZoneInfo
import math
from domain.contracts import finite_number
from execution.candidate_policy import confirmed_candidate_st

ET = ZoneInfo('America/New_York')
PREFIX = '候选入场限制：'
TD_THRESHOLD = 1
# The indicator's coloring contract is independent of the execution admission threshold.
TD_TREND_THRESHOLD = 3


def confirmed_td(track, bar_time):
    td = track.get('td')
    if (not isinstance(td, dict) or td.get('version') != 1 or td.get('comparison_bars') != 4
            or td.get('trend_threshold') != TD_TREND_THRESHOLD or not isinstance(td.get('counts'), list)
            or any(isinstance(td.get(k), bool) for k in ('version', 'comparison_bars', 'trend_threshold'))):
        return None
    matching = [p for p in td['counts'] if isinstance(p, list) and len(p) == 2 and p[0] == bar_time]
    if len(matching) != 1:
        return None
    value = matching[0][1]
    if (isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)
            or int(value) != value or abs(value) > 2**53 - 1):
        return None
    return int(value)


def ema9_entry_state(data, side, now):
    """Compare the observed price with the existing ingestion-layer EMA9; no indicator calculation."""
    data = data if isinstance(data, dict) else {}
    bar = data.get('bar') if isinstance(data.get('bar'), dict) else {}
    ts = data.get('ts') if isinstance(data.get('ts'), dict) else {}
    price, ema = finite_number(bar.get('close')), finite_number(ts.get('ema_9'))
    stamp, observed = finite_number(bar.get('time')), finite_number(data.get('price_observed_at'))
    ready = (stamp is not None and stamp % 300 == 0 and 0 <= now-stamp < 300
             and observed is not None and stamp <= observed <= now and now-observed <= 45
             and price is not None and price > 0 and ema is not None and ema > 0
             and (ts.get('time') is None or finite_number(ts.get('time')) == stamp))
    allowed = bool(ready and ((side == 'long' and price > ema) or (side == 'short' and price < ema)))
    reason = '' if allowed else ('等待新鲜且同根的五分钟EMA9与价格' if not ready else
              f'当前价 {price:g}，五分钟EMA9 {ema:g}，'+('做多要求价格 > EMA9' if side == 'long' else '做空要求价格 < EMA9'))
    return {'ema9_5m': ema, 'ema9_price': price, 'ema9_bar_time': stamp, 'ema9_observed_at': observed,
            'ema9_ready': bool(ready), 'ema9_allowed': allowed, 'ema9_block_reason': reason}


def candidate_snapshot(frame, now, market=None):
    at = datetime.fromtimestamp(now, ET)
    stamp = finite_number(frame.get('as_of'))
    reason = ''
    if frame.get('status') != 'ready':
        reason = frame.get('message') or '候选榜单未就绪'
    elif at.weekday() >= 5 or not 570 <= at.hour * 60 + at.minute < 960:
        reason = '候选入场仅限当日常规盘'
    elif frame.get('day') != at.date().isoformat() or stamp is None or not 0 <= now - stamp <= 120:
        reason = '候选榜单确认数据已过期'
    tracks = {row.get('symbol'): row for row in frame.get('tracks', [])}
    scoring = frame.get('scoring') if isinstance(frame.get('scoring'), dict) else {}
    rows = []
    for ranked in frame.get('ranks', [])[:10]:
        row = tracks.get(ranked.get('symbol')) or {}
        points = row.get('points') or []
        point = points[-1] if points else None
        direction = ranked.get('direction_sign')
        rank = finite_number(ranked.get('rank'))
        point_time = finite_number(point[0]) if point and len(point)==11 else None
        if (not row.get('aligned') or row.get('issue') or row.get('is_beta') or not point
                or len(point) != 11 or stamp is None or point_time is None or point_time + 60 != stamp
                or rank is None or not 1<=rank<=10 or int(rank)!=rank
                or isinstance(direction,bool) or direction not in (-1, 1) or point[10] != direction):
            continue
        td_count = confirmed_td(row, point_time)
        td_allowed = td_count is not None and direction * td_count >= TD_THRESHOLD
        td_reason = ('等待当前分钟TD收盘确认数据' if td_count is None else
                     f'当前TD {td_count:+d}，'+(f'做多要求TD ≥ {TD_THRESHOLD}' if direction == 1 else f'做空要求TD ≤ −{TD_THRESHOLD}'))
        ema = ema9_entry_state((market or {}).get(ranked['symbol']), 'long' if direction == 1 else 'short', now)
        vwap = finite_number(point[2])
        vwap_ready = bool(ema['ema9_ready'] and vwap is not None and vwap > 0)
        vwap_allowed = bool(vwap_ready and direction * (ema['ema9_price'] - vwap) > 0)
        vwap_reason = '' if vwap_allowed else ('等待有效VWAP中线与新鲜价格' if not vwap_ready else
                      f'当前价 {ema["ema9_price"]:g}，VWAP中线 {vwap:g}，'+
                      ('做多要求价格 > VWAP中线' if direction == 1 else '做空要求价格 < VWAP中线'))
        ranking = {'model': scoring.get('model'), 'version': scoring.get('version'), 'weights': scoring.get('weights'),
                   **{key: ranked.get(key) for key in ('score', 'score_parts', 'vwap_score', 'ema9_5m',
                      'ema9_known_at', 'ema9_slope_atr', 'classification_rate', 'classification_correct',
                      'classification_samples', 'classification_start_bar', 'classification_end_bar', 'ema9_bias_atr',
                      'direction_price', 'direction_vwap')}}
        rows.append({'symbol': ranked['symbol'], 'rank': ranked['rank'], 'direction': direction,
                     'candidate_st': confirmed_candidate_st((market or {}).get(ranked['symbol']) or {}, now),
                     'side': 'long' if direction == 1 else 'short', 'sigma': finite_number(point[3]),
                     'vwap': vwap, 'ema3': finite_number(point[1]),
                     'vwap_direction_ready': vwap_ready, 'vwap_direction_allowed': vwap_allowed,
                     'vwap_direction_block_reason': vwap_reason,
                     'td_count': td_count, 'td_ready': td_count is not None,
                     'entry_allowed': td_allowed and ema['ema9_allowed'] and vwap_allowed,
                     **ema, 'ranking_evidence': ranking,
                     'entry_block_reason': td_reason if not td_allowed else ema['ema9_block_reason'] or vwap_reason,
                     'bar_time': point[0], 'confirmed_at': stamp})
    return {'ready': not reason, 'reason': reason, 'day': frame.get('day'), 'as_of': stamp,
            'td_threshold': TD_THRESHOLD, 'ema9_period': 9, 'ema9_interval_seconds': 300,
            'vwap_direction_required': True, 'rows': rows}


def check_candidate(snapshot, symbol, side):
    if not snapshot.get('ready'):
        return {'ok': False, 'reason': PREFIX + (snapshot.get('reason') or '候选榜单未就绪')}
    symbol = symbol.strip().upper()
    rows = [r for r in snapshot['rows'] if r['symbol'] == symbol or
            (':' not in symbol and r['symbol'].rsplit(':', 1)[-1] == symbol)]
    if len(rows) != 1:
        return {'ok': False, 'reason': PREFIX + f'{symbol} 不在当前有效前十候选中'}
    row = rows[0]
    if side != row['side']:
        return {'ok': False, 'reason': PREFIX + f'{symbol} 当前趋势为{row["side"]}，禁止反向开仓'}
    td = row.get('td_count')
    if snapshot.get('td_threshold') != TD_THRESHOLD or td is None or not row.get('entry_allowed'):
        return {'ok': False, 'reason': PREFIX + (row.get('entry_block_reason') or '等待TD确认数据')}
    if snapshot.get('ema9_period') != 9 or snapshot.get('ema9_interval_seconds') != 300 or not row.get('ema9_allowed'):
        return {'ok': False, 'reason': PREFIX + (row.get('ema9_block_reason') or '等待五分钟EMA9过滤数据')}
    if snapshot.get('vwap_direction_required') is not True or not row.get('vwap_direction_allowed'):
        return {'ok': False, 'reason': PREFIX + (row.get('vwap_direction_block_reason') or '等待VWAP中线方向过滤数据')}
    return {'ok': True, 'evidence': dict(row, day=snapshot['day'], as_of=snapshot['as_of'])}
