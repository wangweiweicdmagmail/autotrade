"""IPO trade-arrival alerts. Independent of indicator readiness and execution."""
import json
import math
import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

from console.shared import logger

ET = ZoneInfo('America/New_York')


def candidate(record, watches, now):
    if not isinstance(record, dict) or watches.get(record.get('symbol')) != record.get('listing_date'):
        return None
    def number(name):
        value = record.get(name)
        return value if type(value) in (int,float) and math.isfinite(value) else None
    price, volume, stamp, seen = [number(k) for k in ('price','volume','price_time','observed_at')]
    if any(v is None or v <= 0 for v in (price,volume,stamp,seen)):
        return None
    if not 0 <= now-seen <= 30 or not 0 <= seen-stamp <= 90:
        return None
    day = record['listing_date']
    for t in (stamp,seen,now):
        at = datetime.fromtimestamp(t,ET)
        if at.date().isoformat()!=day or at.weekday()>=5 or not 570<=at.hour*60+at.minute<960:
            return None
    source = record.get('source')
    if source not in ('quote','bar') or record.get('time_kind') != ('trade_time' if source=='quote' else 'bar_open'):
        return None
    return {k:record.get(k) for k in ('symbol','listing_date','price','volume','price_time','observed_at','source','time_kind','update_mode')}


class IpoAlerts:
    def __init__(self, context, watches):
        self.context = context
        self.watches = dict(watches)
        self.path = context.paths.data_dir / 'ipo_alerts.sqlite3'
        self.events = {}
        self.pending = {}
        self.loaded = False
        self.error = ''
        self.last_error = ''

    def _load(self):
        self.path.parent.mkdir(parents=True,exist_ok=True)
        with sqlite3.connect(self.path) as db:
            version = db.execute('PRAGMA user_version').fetchone()[0]
            if version not in (0,1):
                raise ValueError('IPO提醒缓存版本不匹配')
            db.execute('CREATE TABLE IF NOT EXISTS openings (id TEXT PRIMARY KEY, payload TEXT NOT NULL)')
            if version == 0:
                db.execute('PRAGMA user_version=1')
            rows = db.execute('SELECT id,payload FROM openings').fetchall()
            events = {key:json.loads(value) for key,value in rows}
            if any(not isinstance(value,dict) or value.get('id')!=key
                   or type(value.get('price')) not in (int,float) or not math.isfinite(value['price']) or value['price']<=0
                   for key,value in events.items()):
                raise ValueError('IPO提醒缓存内容无效')
        self.events = events
        self.loaded = True

    def refresh(self, records, now):
        """Persist before broadcasting; retry retained raw records if storage failed."""
        if not self.watches:
            return
        try:
            for raw in records.values():
                event = candidate(raw,self.watches,now)
                if event is None:
                    continue
                key = f"ipo:{event['symbol']}:{event['listing_date']}"
                if key in self.events or key in self.pending:
                    continue
                event.update(id=key,detected_at=now,announce_until=now+120)
                self.pending[key] = event
            if not self.loaded:
                self._load()
            for key,event in list(self.pending.items()):
                if key in self.events:
                    self.pending.pop(key)
                    continue
                with sqlite3.connect(self.path) as db:
                    db.execute('INSERT OR IGNORE INTO openings VALUES (?,?)',
                               (key,json.dumps(event,ensure_ascii=False,allow_nan=False)))
                    saved = json.loads(db.execute('SELECT payload FROM openings WHERE id=?',(key,)).fetchone()[0])
                self.events[key] = saved
                self.pending.pop(key)
                self.context.events.record_event('ipo_first_trade_observed',**saved)
                logger.warning('IPO开交易：%s 首次观察成交价 %.4f（%s，成交量%s）',
                               saved['symbol'],saved['price'],saved['source'],saved['volume'])
            self.error = ''
        except (OSError,sqlite3.Error,ValueError,TypeError) as error:
            self.error = f'IPO提醒状态保存失败：{error}'
            if self.error != self.last_error:
                logger.error(self.error)
                self.last_error = self.error

    def snapshot(self, collector, now):
        if not self.watches:
            return {'watches': [], 'events': [], 'error': ''}
        self.refresh(collector.ipo_trades,now)
        day = datetime.fromtimestamp(now,ET).date().isoformat()
        rows = []
        at = datetime.fromtimestamp(now,ET)
        for symbol,date in self.watches.items():
            if date != day:
                continue
            event = self.events.get(f'ipo:{symbol}:{date}')
            raw = collector.ipo_status.get(symbol) or {}
            phase = 'trade_observed' if event else 'ended' if at.hour*60+at.minute>=960 else raw.get('phase','connecting')
            rows.append({'symbol':symbol,'listing_date':date,'phase':phase,
                         'reason':self.error or raw.get('reason'),'event':event})
        return {'watches':rows,'events':[row['event'] for row in rows if row['event']], 'error':self.error}
