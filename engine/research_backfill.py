"""Bounded read-only historical repair. Separate from production trading."""
import asyncio
from datetime import datetime, timedelta
import json
from pathlib import Path
import time

from engine.research_archive import envelope
from engine.research_store import digest, encode
from engine.research_calendar import ET


async def fetch_series(series, end, count, credentials_path):
    root = Path(__file__).resolve().parents[1]
    proc = await asyncio.create_subprocess_exec('node', str(root/'service'/'research_backfill.js'),
        stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(proc.communicate(encode({**series, 'to': end, 'range': count,
            'credentials_path': str(credentials_path)}).encode()), 35)
    except (asyncio.CancelledError, TimeoutError):
        if proc.returncode is None:
            proc.kill()
        await proc.wait()
        raise
    if proc.returncode:
        raise ValueError(err.decode(errors='replace')[-500:])
    return json.loads(out)


async def repair_day(store, day, credentials_path, *, fetch=fetch_series, max_requests=4, now=None, retry_seconds=300):
    now = time.time() if now is None else now
    manifest = store.manifest(day, now=now)
    date = datetime.strptime(day, '%Y-%m-%d').replace(tzinfo=ET)
    end = int((date+timedelta(days=1)).timestamp())
    requested = 0
    results = []
    for row in manifest['series']:
        if requested >= max_requests:
            break
        if row.get('status') not in ('gap_unverified','not_received'):
            continue
        key = digest({'day': day, 'symbol': row['symbol'], 'interval': row['interval'], 'session': row['session']})
        with store.write() as db:
            old = db.execute('SELECT updated_at,payload FROM backfill_attempts WHERE id=?',(key,)).fetchone()
        previous = json.loads(old[1]) if old else {}
        if old and (now-old[0] < retry_seconds or previous.get('attempts', 0) >= 3):
            continue
        requested += 1
        state = {'attempts': previous.get('attempts',0)+1, 'series': row, 'day': day}
        try:
            data = await fetch(row, end, {'1m':1200,'5m':500,'1d':40}[row['interval']], credentials_path)
            metadata = data['metadata']
            series = {**{k:v for k,v in row.items() if k in ('provider','symbol','resolved_symbol','interval','session','adjustment','timezone','minmove','pricescale','type')},
                'provider': 'tradingview', 'resolved_symbol': metadata.get('resolved_symbol') or row.get('resolved_symbol') or row['symbol'],
                'adjustment': 'splits', 'timezone': metadata.get('timezone','unknown'),
                'minmove': metadata.get('minmove'), 'pricescale': metadata.get('pricescale'), 'type': metadata.get('type','unknown')}
            seconds = {'1m':60,'5m':300,'1d':86400}[row['interval']]
            bars = [{**b,'close_time': b['time']+seconds} for b in data['bars'] if b['time']+seconds<=now]
            store.apply(envelope('bars', {'series': series,'rows': bars,'source': 'backfill','received_at': now}, observed_at=now))
            state.update(status='returned', rows=len(bars))
        except Exception as exc:
            state.update(status='failed', error=str(exc))
        with store.write() as db:
            db.execute('INSERT OR REPLACE INTO backfill_attempts VALUES(?,?,?)',(key,now,encode(state)))
        results.append(state)
    return {'day': day, 'requests': requested, 'results': results}
