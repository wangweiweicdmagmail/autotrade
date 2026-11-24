"""Synthetic ingestion facts, independent of live caches, sockets and brokers."""
def minute_data(at, count=1, *, session='regular', vwap=99, sigma=2, close=100):
    return {'rhythm_confirmed_1m':{'version':1,'time':at-60,'td_count':count,
        'comparison_bars':4,'interval_seconds':60,'session':session,'issue':None,
        'close':close,'ema3':close,'vwap':vwap,'sigma':sigma,
        'received_at':at+.01,'computed_at':at+.02,'published_at':at+.03},
        'rhythm_price_1m':{'bar':{'time':at,'open':close,'high':close,'low':close,'close':close,'volume':10},
                           'price_observed_at':at+.01}}
