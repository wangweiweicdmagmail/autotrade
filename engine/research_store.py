"""Versioned research facts. Never writes an execution ledger or imports a broker."""
from contextlib import contextmanager
from datetime import datetime, timedelta
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import threading
import time

from engine.research_calendar import ET, schedule


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def digest(value):
    return hashlib.sha256(encode(value).encode()).hexdigest()


def number(value):
    return type(value) in (int, float) and math.isfinite(value)


SCHEMA = '''
CREATE TABLE market_series(id TEXT PRIMARY KEY, payload TEXT NOT NULL);
CREATE TABLE candle_versions(series_id TEXT NOT NULL, time INTEGER NOT NULL, digest TEXT NOT NULL,
    payload TEXT NOT NULL, PRIMARY KEY(series_id,time,digest));
CREATE TABLE candle_changes(id INTEGER PRIMARY KEY, series_id TEXT NOT NULL, time INTEGER NOT NULL,
    digest TEXT NOT NULL, observed_at REAL NOT NULL, source TEXT NOT NULL);
CREATE INDEX changes_lookup ON candle_changes(series_id,time,observed_at,id);
CREATE TABLE candle_heads(series_id TEXT NOT NULL,time INTEGER NOT NULL,digest TEXT NOT NULL,
    observed_at REAL NOT NULL,PRIMARY KEY(series_id,time));
CREATE TABLE ranking_batches(id TEXT PRIMARY KEY, day TEXT NOT NULL, as_of INTEGER NOT NULL,
    observed_at REAL NOT NULL,payload TEXT NOT NULL);
CREATE INDEX rankings_time ON ranking_batches(day,as_of,observed_at);
CREATE TABLE definition_versions(id TEXT PRIMARY KEY, created_at REAL NOT NULL,payload TEXT NOT NULL);
CREATE TABLE configurations(id TEXT PRIMARY KEY,observed_at REAL NOT NULL,payload TEXT NOT NULL);
CREATE TABLE import_offsets(source TEXT PRIMARY KEY,offset INTEGER NOT NULL,signature TEXT NOT NULL);
CREATE TABLE imported_records(id TEXT PRIMARY KEY,digest TEXT NOT NULL);
CREATE TABLE archive_issues(id TEXT PRIMARY KEY,created_at REAL NOT NULL,payload TEXT NOT NULL);
CREATE TABLE order_records(identity TEXT PRIMARY KEY,day TEXT NOT NULL,payload TEXT NOT NULL);
CREATE TABLE trade_events(id TEXT PRIMARY KEY,lot_id TEXT,ts REAL NOT NULL,payload TEXT NOT NULL);
CREATE INDEX events_lot ON trade_events(lot_id,ts);
CREATE TABLE fills(identity TEXT PRIMARY KEY,lot_id TEXT,ts REAL NOT NULL,payload TEXT NOT NULL);
CREATE TABLE backfill_attempts(id TEXT PRIMARY KEY,updated_at REAL NOT NULL,payload TEXT NOT NULL);
'''


class ResearchStore:
    VERSION = 1

    def __init__(self, path):
        self.path = Path(path)
        self._lock = threading.RLock()

    @contextmanager
    def write(self):
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            db = sqlite3.connect(self.path, timeout=10)
            try:
                version = db.execute('PRAGMA user_version').fetchone()[0]
                if version not in (0, self.VERSION):
                    raise ValueError('研究归档版本未知，禁止覆盖')
                if not version:
                    if db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchone():
                        raise ValueError('研究归档结构未知，禁止覆盖')
                    db.execute('PRAGMA journal_mode=WAL')
                    db.executescript('BEGIN IMMEDIATE;'+SCHEMA+f'PRAGMA user_version={self.VERSION};COMMIT;')
                db.execute('PRAGMA synchronous=FULL')
                db.execute('BEGIN IMMEDIATE')
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                raise
            finally:
                db.close()

    @contextmanager
    def read(self):
        if not self.path.exists():
            raise FileNotFoundError('研究归档尚未建立')
        db = sqlite3.connect(self.path.resolve().as_uri()+'?mode=ro', uri=True, timeout=5)
        try:
            if db.execute('PRAGMA user_version').fetchone()[0] != self.VERSION:
                raise ValueError('研究归档版本不支持')
            yield db
        finally:
            db.close()

    def issue(self, identity, message, **details):
        with self.write() as db:
            db.execute('INSERT OR REPLACE INTO archive_issues VALUES(?,?,?)',
                       (identity, time.time(), encode({'message': message, **details})))

    def apply(self, record, *, source=None, offset=None, signature=''):
        """Record identity and source cursor commit atomically with the data."""
        body = record['body']
        if hashlib.sha256(body.encode()).hexdigest() != record['sha256']:
            raise ValueError('归档记录校验失败')
        data = json.loads(body)
        observed = data['observed_at']
        if not number(observed) or observed <= 0:
            raise ValueError('归档观察时间无效')
        with self.write() as db:
            existing = db.execute('SELECT digest FROM imported_records WHERE id=?', (record['id'],)).fetchone()
            if existing and existing[0] != record['sha256']:
                raise ValueError('同一归档身份出现不同内容')
            if not existing:
                self._apply(db, data['kind'], data['payload'], observed)
                if data.get('health', {}).get('dropped', 0):
                    db.execute('INSERT OR REPLACE INTO archive_issues VALUES(?,?,?)',
                               ('producer:'+record['id'].split(':')[0], observed, encode(data['health'])))
                db.execute('INSERT INTO imported_records VALUES(?,?)', (record['id'], record['sha256']))
            if source is not None:
                db.execute('INSERT INTO import_offsets VALUES(?,?,?) ON CONFLICT(source) DO UPDATE SET offset=MAX(import_offsets.offset,excluded.offset) WHERE import_offsets.signature=excluded.signature', (source, offset, signature))

    def _apply(self, db, kind, payload, observed):
        if kind == 'bars':
            series = payload['series']
            if series.get('interval') not in ('1m', '5m', '1d') or series.get('session') not in ('regular', 'extended', 'default', '24h'):
                raise ValueError('未知行情序列')
            if not all(isinstance(series.get(k), str) and series[k] for k in ('symbol', 'resolved_symbol', 'provider', 'adjustment')):
                raise ValueError('行情序列身份不完整')
            identity = digest({k:series[k] for k in ('provider','symbol','resolved_symbol','interval','session','adjustment')})
            db.execute('INSERT OR REPLACE INTO market_series VALUES(?,?)', (identity, encode(series)))
            metadata = {'kind':'series_metadata','series_id':identity,'series':series}
            db.execute('INSERT OR IGNORE INTO definition_versions VALUES(?,?,?)',(digest(metadata),observed,encode(metadata)))
            interval = {'1m': 60, '5m': 300, '1d': 86400}[series['interval']]
            received = payload.get('received_at', observed)
            if not number(received) or received > observed+1 or received <= 0:
                raise ValueError('行情接收时间无效')
            for bar in payload['rows']:
                stamp, end = bar.get('time'), bar.get('close_time')
                values = [bar.get(k) for k in ('open', 'high', 'low', 'close', 'volume')]
                if (type(stamp) is not int or not number(end) or end <= stamp or end > received
                        or (interval < 86400 and (stamp % interval or end != stamp+interval))
                        or not all(number(v) for v in values) or min(values[:4]) <= 0 or values[4] < 0
                        or not values[2] <= min(values[0], values[3]) <= max(values[0], values[3]) <= values[1]):
                    raise ValueError('K线未收盘、未对齐或OHLCV无效')
                key = digest(bar)
                old = db.execute('SELECT digest,observed_at FROM candle_heads WHERE series_id=? AND time=?', (identity, stamp)).fetchone()
                db.execute('INSERT OR IGNORE INTO candle_versions VALUES(?,?,?,?)', (identity, stamp, key, encode(bar)))
                if not old or (old[0] != key and received >= old[1]):
                    db.execute('INSERT INTO candle_changes(series_id,time,digest,observed_at,source) VALUES(?,?,?,?,?)',
                               (identity, stamp, key, received, payload.get('source', 'unknown')))
                    db.execute('INSERT OR REPLACE INTO candle_heads VALUES(?,?,?,?)', (identity, stamp, key, received))
        elif kind == 'ranking':
            c, scoring = payload['coverage'], payload['scoring']
            if (type(payload['as_of']) is not int or payload['as_of'] % 60 or payload['as_of'] > observed
                    or c['total'] <= 0 or c['aligned'] != c['total'] or c['missing'] or scoring['pending']):
                raise ValueError('榜单不是完整已确认批次')
            db.execute('INSERT OR IGNORE INTO ranking_batches VALUES(?,?,?,?,?)',
                       (payload['batch_id'], payload['day'], payload['as_of'], observed, encode(payload)))
        elif kind in ('configuration', 'definition'):
            table = 'configurations' if kind == 'configuration' else 'definition_versions'
            db.execute(f'INSERT OR IGNORE INTO {table} VALUES(?,?,?)', (digest(payload), observed, encode(payload)))
        elif kind == 'reference':
            db.execute('INSERT OR IGNORE INTO definition_versions VALUES(?,?,?)', (digest(payload), observed, encode(payload)))
        else:
            raise ValueError('未知研究记录类型: '+str(kind))

    def cursor(self, source):
        if not self.path.exists():
            return 0, ''
        with self.read() as db:
            row = db.execute('SELECT offset,signature FROM import_offsets WHERE source=?', (source,)).fetchone()
            return row or (0, '')

    def series(self, symbol=None, interval=None):
        if not self.path.exists():
            return []
        with self.read() as db:
            rows = [{'id': key, **json.loads(payload)} for key, payload in db.execute('SELECT id,payload FROM market_series ORDER BY id')]
        return [r for r in rows if (symbol is None or r['symbol'] == symbol) and (interval is None or r['interval'] == interval)]

    def bars(self, series_id, start, end, *, as_of=None, limit=10000):
        if not 0 < limit <= 10000:
            raise ValueError('查询根数应为1至10000')
        if not self.path.exists():
            return []
        with self.read() as db:
            if as_of is None:
                rows = db.execute('''SELECT v.payload,h.observed_at FROM candle_heads h JOIN candle_versions v
                    ON v.series_id=h.series_id AND v.time=h.time AND v.digest=h.digest
                    WHERE h.series_id=? AND h.time>=? AND h.time<? ORDER BY h.time LIMIT ?''', (series_id, start, end, limit)).fetchall()
            else:
                rows = db.execute('''WITH visible AS (SELECT *,ROW_NUMBER() OVER(PARTITION BY time ORDER BY observed_at DESC,id DESC) AS n
                    FROM candle_changes WHERE series_id=? AND time>=? AND time<? AND observed_at<=?)
                    SELECT v.payload,c.observed_at FROM visible c JOIN candle_versions v
                    ON v.series_id=c.series_id AND v.time=c.time AND v.digest=c.digest WHERE c.n=1 ORDER BY c.time LIMIT ?''',
                                  (series_id, start, end, as_of, limit)).fetchall()
        return [{**json.loads(row), 'observed_at': at} for row, at in rows]

    def rankings(self, day, *, at=None, as_of=None):
        if not self.path.exists():
            return []
        with self.read() as db:
            rows = db.execute('SELECT payload,observed_at FROM ranking_batches WHERE day=? AND as_of<=? AND observed_at<=? ORDER BY as_of,observed_at',
                              (day, at if at is not None else 2**53, as_of if as_of is not None else 2**53)).fetchall()
        return [{**json.loads(payload), 'observed_at': observed} for payload, observed in rows]

    def manifest(self, day, *, now=None):
        """Strict coverage. Unknown/no-trade minutes are reported, never fabricated."""
        now = time.time() if now is None else now
        date = datetime.strptime(day, '%Y-%m-%d').replace(tzinfo=ET)
        start, end = int(date.timestamp()), int((date+timedelta(days=1)).timestamp())
        calendar = schedule(day)
        with self.read() as db:
            row = db.execute('SELECT payload FROM configurations WHERE observed_at<=? ORDER BY observed_at DESC LIMIT 1',(min(now,end),)).fetchone()
            origin = 'observed'
            if row is None:
                row = db.execute('SELECT payload FROM configurations ORDER BY observed_at DESC LIMIT 1').fetchone()
                origin = 'posthoc_universe' if row else 'series_only'
            issues = [json.loads(p) for p, in db.execute('SELECT payload FROM archive_issues ORDER BY created_at DESC LIMIT 20')]
        config = json.loads(row[0]) if row else {}
        actual = self.series()
        results = []
        for expected in config.get('series', actual):
            matches = [s for s in actual if all(s.get(k) == expected.get(k) for k in ('symbol', 'interval', 'session'))]
            if not matches:
                results.append({**expected, 'status': 'not_received', 'count': 0, 'missing': None})
                continue
            for series in matches:
                bars = self.bars(series['id'], start, end)
                all_bars = self.bars(series['id'], 0, end)
                interval = {'1m': 60, '5m': 300, '1d': 86400}[series['interval']]
                missing, state = [], 'complete'
                if not calendar or series['session'] in ('default', '24h') or interval == 86400:
                    state = 'schedule_unknown'
                elif not calendar['closed']:
                    lo = calendar['extended_open'] if series['session'] == 'extended' else calendar['regular_open']
                    hi = calendar['extended_close'] if series['session'] == 'extended' else calendar['regular_close']
                    hi = min(hi, int(now)//interval*interval)
                    stamps = {r['time'] for r in bars}
                    missing = [t for t in range(lo, max(lo, hi), interval) if t not in stamps]
                    state = 'gap_unverified' if missing else 'collecting' if now < calendar['extended_close' if series['session']=='extended' else 'regular_close'] else 'complete'
                results.append({**series, 'status': state, 'count': len(bars), 'missing': missing,
                                'first': bars[0]['time'] if bars else None, 'last': bars[-1]['time'] if bars else None,
                                'warmup_first': all_bars[0]['time'] if all_bars else None,
                                'candle_revision': digest([(b['time'], {k:v for k,v in b.items() if k!='observed_at'}) for b in bars])})
        return {'day': day, 'calendar': calendar, 'series': results, 'issues': issues,
                'complete': bool(results) and not issues and all(r['status']=='complete' for r in results),
                'configuration': config, 'configuration_origin': origin,
                'scope': 'source_grid_coverage; absent minutes require source verification'}

    def status(self):
        if not self.path.exists():
            return {'available': False}
        with self.read() as db:
            return {'available': True, 'schema': self.VERSION, 'last_change': db.execute('SELECT MAX(observed_at) FROM candle_changes').fetchone()[0],
                    'series': db.execute('SELECT COUNT(*) FROM market_series').fetchone()[0],
                    'issues': db.execute('SELECT COUNT(*) FROM archive_issues').fetchone()[0]}

    def backup(self, destination):
        target = Path(destination)
        if target.exists():
            raise ValueError('备份目标已经存在')
        target.parent.mkdir(parents=True, exist_ok=True)
        with self.read() as source:
            db = sqlite3.connect(target)
            try:
                source.backup(db)
                if db.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                    raise ValueError('研究备份校验失败')
            finally:
                db.close()
