"""Shared read model of per-symbol confirmed observations; never compute indicators."""
import hashlib
import json

from domain.contracts import finite_number

TD_SOURCE = 'confirmed_minute_v1'
VWAP_SOURCE = 'confirmed_vwap_direction_v1'
INDEX_SYMBOLS = ('CAPITALCOM:NAS100', 'CAPITALCOM:SPX500')


def observation(data, now, *, default_session=False, require_td=True):
    data = data if isinstance(data, dict) else {}
    raw = data.get('rhythm_confirmed_1m') or {}
    raw = raw if isinstance(raw, dict) else {}
    stamp, count = finite_number(raw.get('time')), raw.get('td_count')
    known = bool(type(raw.get('version')) is int and raw['version'] == 1
        and (not require_td or type(raw.get('comparison_bars')) is int and raw['comparison_bars'] == 4)
        and type(raw.get('interval_seconds')) is int and raw['interval_seconds'] == 60
        and raw.get('session') in (('default',) if default_session else ('regular', 'extended', '24h') if require_td else ('regular',))
        and stamp is not None and stamp > 0 and stamp % 60 == 0 and stamp+60 <= now
        and finite_number(raw.get('close')) is not None and finite_number(raw.get('close'))>0
        and (type(count) is int and abs(count) <= 2**53-1 if require_td else
             finite_number(raw.get('vwap')) is not None and finite_number(raw.get('vwap'))>0))
    quote = data.get('rhythm_price_1m') or {}
    quote = quote if isinstance(quote, dict) else {}
    bar = quote.get('bar') or {}
    bar = bar if isinstance(bar, dict) else {}
    qtime, observed, price = (finite_number(bar.get('time')), finite_number(quote.get('price_observed_at')),
                               finite_number(bar.get('close')))
    confirmed = stamp+60 if known else None
    quote_ready = bool(qtime is not None and qtime % 60 == 0 and 0 <= now-qtime < 60
        and observed is not None and qtime <= observed <= now and now-observed <= 45
        and price is not None and price > 0)
    facts = {k: finite_number(raw.get(k)) for k in ('close', 'ema3', 'vwap', 'sigma', 'received_at', 'computed_at', 'published_at')}
    stages=[facts[k] for k in ('received_at','computed_at','published_at')]
    # A rollover can arrive before the local minute boundary. The ingestion
    # layer waits until close before computing, retaining the real receipt time.
    # Confirmation depends on computation/publication after close, not on when
    # the raw candle first arrived. Never rewrite receipt timestamps to pass.
    stages_ready=bool(known and all(t is not None for t in stages)
        and stamp<=stages[0]<=stages[1] and confirmed<=stages[1]<=stages[2]<=now)
    issue=raw.get('issue')
    td_issue=issue if not require_td else issue if issue not in (None,'missing_session_anchor','missing_open','ema_warmup') else None
    ready = bool(known and quote_ready and stages_ready and confirmed == qtime and not td_issue)
    reason = ('' if ready else '缺少有效的一分钟收盘确认值' if not known else
              '一分钟确认数据存在缺口：'+str(td_issue) if td_issue else
              '确认数据处理时间无效' if not stages_ready else
              '一分钟行情已中断或报价陈旧' if not quote_ready else '等待本标的最新一分钟收盘计算完成')
    keys=('version','symbol','time','session','close','vwap','sigma','issue')
    if require_td:keys=(*keys,'td_count','ema3')
    identity = hashlib.sha256(json.dumps({k:raw.get(k) for k in keys},
        sort_keys=True,allow_nan=True,separators=(',',':')).encode()).hexdigest() if known else None
    return {'version':1,'source':TD_SOURCE if require_td else VWAP_SOURCE,'id':identity,'known':known,'ready':ready,'reason':reason,
            'bar_time':stamp if known else None,'confirmed_at':confirmed,'count':count if known and type(count) is int else None,
            'direction':(1 if facts['close']>facts['vwap'] else -1 if facts['close']<facts['vwap'] else 0) if known and not require_td else None,
            'session':raw.get('session'),'quote_bar_time':qtime,'observed_at':observed,
            'evaluated_at':now,**facts,
            'latency_seconds':max(0,now-confirmed) if confirmed is not None else None}


def build_snapshot(market, now, *, require_td=True):
    market = market if isinstance(market, dict) else {}
    symbols = sorted(set(market) | set(INDEX_SYMBOLS))
    rows={}
    for symbol in symbols:
        data=market.get(symbol)
        data=data if isinstance(data,dict) else {}
        fact=observation(data,now,default_session=symbol in INDEX_SYMBOLS,require_td=require_td)
        raw=data.get('rhythm_confirmed_1m')
        source=raw.get('symbol') if isinstance(raw,dict) else None
        if source is not None and source!=symbol:
            fact.update(known=False,ready=False,count=None,direction=None,id=None,reason='确认数据标的不匹配')
        rows[symbol]=fact
    return {'version':1,'source':TD_SOURCE if require_td else VWAP_SOURCE,'generated_at':now,'rows':rows}
