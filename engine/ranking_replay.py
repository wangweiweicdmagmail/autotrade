"""Database-first historical ranking inputs. No application, broker, or ledger access."""
import asyncio
from datetime import datetime,timedelta
import json
from pathlib import Path
import time
from engine.research_archive import envelope
from engine.research_backfill import fetch_series
from engine.research_calendar import ET,schedule
from engine.research_store import digest


def day_bounds(day):
    plan=schedule(day)
    if not plan or plan['closed']:raise ValueError('该日期不是已知的常规交易日')
    date=datetime.strptime(day,'%Y-%m-%d').replace(tzinfo=ET)
    return plan,int(date.timestamp()),int((date+timedelta(days=1)).timestamp())


def cached_input(store,symbol,interval,session,day):
    plan,start,end=day_bounds(day)
    series=[r for r in store.series(symbol,interval) if r['provider']=='tradingview' and r['session']==session and r['adjustment']=='splits']
    if len(series)>1:raise ValueError('行情序列存在多个解析标的，不能任意选取：'+symbol+'/'+interval)
    if not series:return None,[],{'complete':False,'rows':0,'warmup':0,'missing':[]}
    rows=store.bars(series[0]['id'],start-14*86400,end,limit=10000)
    lo=plan['regular_open'] if session=='regular' else start
    hi=plan['regular_close'] if session=='regular' else end
    actual=[b for b in rows if lo<=b['time']<hi]
    warm=sum(b['time']<plan['regular_open'] for b in rows)
    seconds={'1m':60,'5m':300}[interval]
    expected=set(range(lo,hi,seconds)) if session=='regular' else None
    missing=sorted(expected-{b['time'] for b in actual}) if expected else []
    # Default-provider schedules are unknown; cache returned data without claiming calendar completeness.
    reusable=bool(actual and warm>=(30 if interval=='5m' else 4) and not missing)
    return series[0],rows,{'complete':reusable,'schedule_verified':session=='regular','rows':len(actual),'warmup':warm,'missing':missing}


async def cache_day(store,day,symbols,default_symbols,credentials_path,*,fetch=fetch_series,concurrency=3,now=None,on_progress=None):
    if not 1<=concurrency<=6:raise ValueError('历史并发数须为1至6')
    plan,start,end=day_bounds(day);received=time.time() if now is None else now
    if end>received:raise ValueError('只缓存已结束的历史交易日')
    semaphore=asyncio.Semaphore(concurrency);results=[]
    async def one(symbol,interval):
        async with semaphore:
            session='default' if symbol in default_symbols else 'regular'
            result={'symbol':symbol,'interval':interval,'session':session}
            try:
                series,rows,coverage=cached_input(store,symbol,interval,session,day)
                if coverage['complete']:
                    result.update(status='cached',**coverage,series_id=series['id'])
                else:
                    data=await fetch({'symbol':symbol,'interval':interval,'session':session,'historical_replay':interval=='1m' or session=='default'},end,
                        3000 if session=='default' and interval=='1m' else 1200 if interval=='1m' else 600,credentials_path)
                    observed=time.time() if now is None else now
                    meta=data['metadata'];seconds=60 if interval=='1m' else 300
                    bars=[{k:b[k] for k in ('time','open','high','low','close','volume')} for b in data['bars'] if b['time']+seconds<=end]
                    for bar in bars:bar['close_time']=bar['time']+seconds
                    if not any(start<=b['time']<end for b in bars):raise ValueError('数据商未返回目标日K线（不可确认是否尚未上市或历史授权不足）')
                    identity={'provider':'tradingview','symbol':symbol,'resolved_symbol':meta.get('resolved_symbol') or symbol,
                        'interval':interval,'session':session,'adjustment':'splits',
                        **{k:meta.get(k) for k in ('timezone','minmove','pricescale','type')}}
                    store.apply(envelope('bars',{'series':identity,'rows':bars,'received_at':observed,
                        'source':'historical_ranking_backfill_replay' if interval=='1m' or session=='default' else 'historical_ranking_backfill'},observed_at=observed))
                    series,rows,coverage=cached_input(store,symbol,interval,session,day)
                    result.update(status='downloaded' if coverage['complete'] else 'limited',**coverage,series_id=series['id'])
            except Exception as error:result.update(status='failed',error=str(error))
            results.append(result)
            if on_progress:on_progress(result,len(results),len(symbols)*2)
    await asyncio.gather(*(one(s,i) for s in dict.fromkeys(symbols) for i in ('1m','5m')))
    return {'day':day,'database':str(store.path),'requested_symbols':list(symbols),
        'received_at':received,'rows':sorted(results,key=lambda r:(r['symbol'],r['interval'])),
        'complete':all(r['status'] in ('cached','downloaded') for r in results)}


def replay_inputs(store,day,at,symbols,default_symbols):
    plan,start,end=day_bounds(day)
    if not plan['regular_open']<at<=plan['regular_close'] or at%60:raise ValueError('榜单时间须为常规盘中的一分钟收盘确认时刻')
    datasets={};missing=[];sources={}
    for symbol in symbols:
        if symbol in default_symbols:continue  # Index observations are independent of stock ranking.
        try:
            inputs=[]
            for interval in ('1m','5m'):
                series,rows,coverage=cached_input(store,symbol,interval,'regular',day)
                if not series:
                    if interval=='5m':continue
                    raise ValueError('缺少'+interval+'数据库序列')
                # Only causal candles are handed to PineTS/ranking, even though the database holds the whole day.
                bars=[{k:b[k] for k in ('time','open','high','low','close','volume')} for b in rows if b['close_time']<=at]
                if not bars:
                    if interval=='5m':continue
                    raise ValueError('该时刻之前没有'+interval+'收盘数据')
                inputs.append((series,bars))
            datasets[symbol]={'bars':inputs[0][1],'five_bars':inputs[1][1] if len(inputs)>1 else [],
                'metadata':{k:inputs[0][0][k] for k in ('timezone','minmove','pricescale','type','session')}}
            sources[symbol]={'series_ids':[s['id'] for s,_ in inputs],
                'input_digest':digest(datasets[symbol]),'minute_rows':len(inputs[0][1]),'five_rows':len(inputs[1][1]) if len(inputs)>1 else 0}
        except ValueError as error:missing.append({'symbol':symbol,'reason':str(error)})
    return {'day':day,'as_of':at,'datasets':datasets,'missing':missing,'sources':sources,'mode':'latest_research_reconstruction'}


async def rank_at(store,day,time_et,symbols,default_symbols):
    at=int(datetime.strptime(day+' '+time_et,'%Y-%m-%d %H:%M').replace(tzinfo=ET).timestamp())
    inputs=replay_inputs(store,day,at,symbols,default_symbols)
    root=Path(__file__).resolve().parents[1]
    proc=await asyncio.create_subprocess_exec('node',str(root/'service'/'replay_ranking.js'),
        stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
    try:
        out,err=await asyncio.wait_for(proc.communicate(json.dumps(inputs).encode()),180)
    except (asyncio.CancelledError,TimeoutError):
        if proc.returncode is None:proc.kill()
        await proc.wait();raise
    if proc.returncode:raise ValueError(err.decode(errors='replace')[-1000:])
    result=json.loads(out);result.update(time_et=time_et,mode=inputs['mode'],sources=inputs['sources'],missing=inputs['missing'],database=str(store.path))
    result['complete']=result['complete'] and not inputs['missing']
    result['experiment_id']=digest(result)
    store.apply(envelope('definition',{'kind':'ranking_experiment','result':result}))
    return result
