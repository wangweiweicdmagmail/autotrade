"""Read confirmed ranking facts; never calculate indicators or manage exits."""
from datetime import datetime
from zoneinfo import ZoneInfo
import config
from domain.contracts import finite_number
from execution.candidate_policy import confirmed_candidate_st
from execution.confirmed_minute import TD_SOURCE, VWAP_SOURCE, build_snapshot as confirmed_snapshot

ET = ZoneInfo('America/New_York')
PREFIX = '候选入场限制：'
ENTRY_POLICY = 'stock_vwap_side_v1'


def quote_entry_state(data, now):
    data = data if isinstance(data, dict) else {}
    bar = data.get('bar') if isinstance(data.get('bar'), dict) else {}
    price, stamp, observed = (finite_number(bar.get('close')), finite_number(bar.get('time')),
                              finite_number(data.get('price_observed_at')))
    ready = bool(price is not None and price>0 and stamp is not None and stamp%300==0
                 and 0<=now-stamp<300 and observed is not None and stamp<=observed<=now and now-observed<=45)
    return {'quote_price':price,'quote_ready':ready,'quote_bar_time':stamp,'quote_observed_at':observed,
            'quote_block_reason':'' if ready else '等待新鲜且有效的当前价格'}


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


def candidate_snapshot(frame, now, market=None, *, ranking='trend', view='mixed', td_snapshot=None, direction_snapshot=None):
    at = datetime.fromtimestamp(now, ET)
    stamp = finite_number(frame.get('as_of'))
    reason = ''
    if at.weekday()<5 and 570<=at.hour*60+at.minute<585:
        reason='开盘前15分钟不开仓，09:45起允许新开仓'
    elif frame.get('status') != 'ready':
        reason = frame.get('message') or '候选榜单未就绪'
    elif at.weekday() >= 5 or not 570 <= at.hour * 60 + at.minute < 960:
        reason = '候选入场仅限当日常规盘'
    elif (frame.get('ranking_session') or {}).get('phase') == 'premarket':
        reason = '盘前观察榜不能开仓，等待常规盘完整榜单'
    elif frame.get('day') != at.date().isoformat() or stamp is None or stamp > now:
        reason = '候选榜单不是当日有效收盘批次'
    else:
        coverage, accepted = frame.get('coverage'), frame.get('ranking_snapshot')
        if (not isinstance(coverage, dict) or type(coverage.get('total')) is not int or coverage['total']<=0
                or type(coverage.get('aligned')) is not int or coverage['aligned']!=coverage['total']
                or coverage.get('missing')!=[] or not isinstance(accepted, dict)
                or type(accepted.get('version')) is not int or accepted['version']!=1
                or not isinstance(accepted.get('id'), str) or not accepted['id']
                or accepted.get('as_of')!=stamp or accepted.get('day')!=frame.get('day')):
            reason = '等待服务端接纳完整的一分钟收盘榜单'
    if ranking!='trend':
        reason='TD榜已移除，仅支持趋势榜开仓'
    if view not in ('mixed','long','short'):
        reason='候选榜视图无效'
    scoring=frame.get('scoring') if isinstance(frame.get('scoring'),dict) else {}
    tracks={row.get('symbol'):row for row in [*frame.get('tracks',[]),*frame.get('watch_tracks',[]),*frame.get('ranking_tracks',[])]}
    direction_snapshot=confirmed_snapshot(market,now,require_td=False) if direction_snapshot is None else direction_snapshot
    td_snapshot=confirmed_snapshot(market,now) if td_snapshot is None else td_snapshot
    if direction_snapshot.get('version')!=1 or direction_snapshot.get('source')!=VWAP_SOURCE:
        reason=reason or '逐标的VWAP确认数据契约无效'
    rows=[]
    selected_ranks=(frame.get('rankings') or {}).get(view,frame.get('ranks',[]) if view=='mixed' else [])
    for ranked in selected_ranks[:10]:
        row=tracks.get(ranked.get('symbol')) or {}
        points=row.get('points') or []
        point=points[-1] if points else None
        rank=finite_number(ranked.get('rank'))
        position=finite_number(ranked.get('opening_position'))
        if position is None and scoring.get('model')!='opening15_midpoint_v7':position=finite_number(point[8]) if point and len(point)==11 else None
        rank_direction=(1 if position>0 else -1 if position<0 else 0) if position is not None else 0
        if (not row.get('aligned') or row.get('issue') or row.get('is_beta') or not point
                or len(point)!=11 or finite_number(point[0]) is None or point[0]+60!=stamp
                or rank is None or not 1<=rank<=10 or int(rank)!=rank
                or ranked.get('direction_sign')!=rank_direction or not rank_direction):
            continue
        symbol=ranked['symbol']
        fact=(direction_snapshot.get('rows') or {}).get(symbol,{})
        td=(td_snapshot.get('rows') or {}).get(symbol,{})
        direction=fact.get('direction') if fact.get('known') else 0
        side='long' if direction==1 else 'short' if direction==-1 else None
        quote=quote_entry_state((market or {}).get(symbol),now)
        allowed=bool(fact.get('ready') is True and direction in (-1,1))
        block=(fact.get('reason') or '等待本标的最新收盘价与VWAP' if not fact.get('ready') else
               '一分钟收盘价等于VWAP，暂不允许开仓')
        ranking_facts={'batch_id':frame.get('ranking_batch_id'),'snapshot_id':(frame.get('ranking_snapshot') or {}).get('id'),
            'model':scoring.get('model'),'version':scoring.get('version'),'weights':scoring.get('weights'),'ranking_view':view,
            **{key:ranked.get(key) for key in ('opening_position','range_position','from_open_position','day_open','opening_high','opening_low','opening_width','opening_start','range_confirmed_at','opening_midpoint','source','score','score_parts','z','price_source','direction_price','direction_vwap','position_score','vwap_progress_score',
                'vwap_progress_sigma','vwap_aligned_progress_sigma','vwap_open','vwap_open_bar_time',
                'classification_rate','classification_correct','classification_samples','classification_start_bar','classification_end_bar',
                'ema5_5m','ema5_bar_time','ema5_known_at','classification_evidence')}}
        rows.append({'symbol':symbol,'rank':ranked['rank'],'ranking_mode':'trend','ranking_view':view,'ranking_direction':rank_direction,'direction':direction,'side':side,
            'entry_policy':ENTRY_POLICY,'entry_allowed':allowed and quote['quote_ready'],
            'entry_block_reason':block if not allowed else quote['quote_block_reason'],
            'direction_fact':fact,'direction_ready':fact.get('ready') is True,'direction_source':VWAP_SOURCE,
            'close':fact.get('close'),'vwap':fact.get('vwap'),'sigma':finite_number(fact.get('sigma')),'ema3':fact.get('ema3'),
            'vwap_direction_ready':fact.get('ready') is True,'vwap_direction_allowed':allowed,
            'vwap_direction_block_reason':'' if allowed else block,
            'td_count':td.get('count') if td.get('known') else None,'td_ready':td.get('ready') is True,'td_fact':td,
            'candidate_st':confirmed_candidate_st((market or {}).get(symbol) or {},now),
            **quote,'ranking_evidence':ranking_facts,'bar_time':fact.get('bar_time'),'confirmed_at':fact.get('confirmed_at'),
            'ranking_bar_time':point[0],'ranking_confirmed_at':stamp})
    return {'ready':not reason,'reason':reason,'day':frame.get('day'),'as_of':stamp,
        'ranking_snapshot':frame.get('ranking_snapshot'),'data_contract_version':4,'entry_policy':ENTRY_POLICY,
        'direction_source':VWAP_SOURCE,'td_source':TD_SOURCE,'evaluated_at':now,
        'ranking_age_seconds':max(0,now-stamp) if stamp is not None else None,'ranking_mode':'trend','ranking_view':view,
        'td_required':False,'ema9_required':False,'vwap_direction_required':True,'beta_filter_required':False,'rows':rows}


def check_candidate(snapshot,symbol,side):
    if (snapshot.get('data_contract_version')!=4 or snapshot.get('entry_policy')!=ENTRY_POLICY
            or snapshot.get('direction_source')!=VWAP_SOURCE or snapshot.get('ranking_mode')!='trend'
            or snapshot.get('td_required') is not False or snapshot.get('ema9_required') is not False
            or snapshot.get('vwap_direction_required') is not True or snapshot.get('beta_filter_required') is not False):
        return {'ok':False,'reason':PREFIX+'等待收盘价／VWAP趋势准入契约'}
    if not snapshot.get('ready'):
        return {'ok':False,'reason':PREFIX+(snapshot.get('reason') or '候选榜单未就绪')}
    symbol=symbol.strip().upper()
    rows=[r for r in snapshot['rows'] if r['symbol']==symbol or ':' not in symbol and r['symbol'].rsplit(':',1)[-1]==symbol]
    if len(rows)!=1:return {'ok':False,'reason':PREFIX+f'{symbol} 不在当前有效前十候选中'}
    row=rows[0];fact=row.get('direction_fact') or {}
    close,vwap=finite_number(fact.get('close')),finite_number(fact.get('vwap'))
    sign=1 if close is not None and vwap is not None and close>vwap else -1 if close is not None and vwap is not None and close<vwap else 0
    if (fact.get('version')!=1 or fact.get('source')!=VWAP_SOURCE or fact.get('ready') is not True
            or close is None or vwap is None or close<=0 or vwap<=0
            or fact.get('bar_time')!=row.get('bar_time') or fact.get('confirmed_at')!=row.get('confirmed_at')
            or fact.get('direction')!=sign or row.get('direction')!=sign
            or fact.get('close')!=row.get('close') or fact.get('vwap')!=row.get('vwap')):
        return {'ok':False,'reason':PREFIX+(row.get('entry_block_reason') or '收盘价与VWAP准入证据未对齐')}
    if not sign or side!=('long' if sign==1 else 'short') or row.get('side')!=side:
        return {'ok':False,'reason':PREFIX+f'一分钟收盘价 {close:g} / VWAP {vwap:g}，'+('仅可做多' if sign==1 else '仅可做空' if sign==-1 else '相等不开仓')}
    if not row.get('entry_allowed') or not row.get('quote_ready'):
        return {'ok':False,'reason':PREFIX+(row.get('entry_block_reason') or '等待新鲜报价')}
    return {'ok':True,'evidence':dict(row,day=snapshot['day'],as_of=snapshot['as_of'])}
