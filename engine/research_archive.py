"""Consume durable research journals independently from live execution."""
import asyncio
import hashlib
import json
import logging
from pathlib import Path
import time
import threading

from engine.research_store import ResearchStore, encode

logger = logging.getLogger(__name__)


class ResearchArchive:
    def __init__(self, root):
        self.root = Path(root)
        self.spool = self.root / 'spool'
        self.store = ResearchStore(self.root / 'archive.sqlite3')
        self.task = None
        self.error = ''
        self.version_error = ''
        self.committed = 0
        self._pump_lock = threading.Lock()
        self.credentials_path = None
        self._repair_at = time.monotonic()+60
        self.runtime_parameters = {}

    def start(self):
        if self.task is None:
            self.task = asyncio.create_task(self._run())

    async def _run(self):
        try:
            await asyncio.to_thread(self.capture_versions)
        except Exception as exc:
            self.version_error = '研究版本记录失败: '+str(exc)
            logger.exception('研究版本记录失败')
        while True:
            try:
                await asyncio.to_thread(self.pump)
                if self.credentials_path and Path(self.credentials_path).exists() and time.monotonic() >= self._repair_at and self.store.path.exists():
                    from engine.research_backfill import repair_day
                    from engine.research_calendar import ET
                    from datetime import datetime
                    self._repair_at = time.monotonic()+300
                    await repair_day(self.store, datetime.now(ET).date().isoformat(), self.credentials_path, max_requests=2)
                self.error = ''
            except Exception as exc:
                if str(exc) != self.error:
                    logger.exception('研究归档失败，原交易链路不变')
                self.error = str(exc)
            await asyncio.sleep(1)

    def capture_versions(self):
        root = Path(__file__).resolve().parents[1]
        names = ('execution/entry_admission.py','execution/candidate_policy.py','execution/intelligent_exit.py','engine/pine_execution.py',
                 'execution/operations.py','execution/risk.py','engine/research_calendar.py')
        payload = {'sources': {name:(root/name).read_text() for name in names}, 'runtime':self.runtime_parameters}
        self.store.apply(envelope('definition',payload))

    async def stop(self):
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None
        await asyncio.to_thread(self.pump)

    def pump(self, limit=1000):
        with self._pump_lock:
            return self._pump(limit)

    def _pump(self, limit):
        count = 0
        for path in sorted(self.spool.glob('*.ndjson')):
            stat = path.stat()
            signature = f'{stat.st_dev}:{stat.st_ino}'
            key = 'journal:'+path.name
            offset, previous = self.store.cursor(key)
            if (previous and previous != signature) or stat.st_size < offset:
                self.store.issue(key, '归档暂存被替换或截断，保留原提交位置')
                continue
            with path.open('rb') as stream:
                stream.seek(offset)
                while count < limit:
                    line = stream.readline(2*1024*1024+1)
                    if not line:
                        break
                    if not line.endswith(b'\n'):
                        # A producer may still be appending. Never acknowledge a partial line.
                        self.store.issue(key+':tail', '暂存尾部未完整提交', offset=offset)
                        break
                    next_offset = stream.tell()
                    try:
                        if len(line) > 2*1024*1024:
                            raise ValueError('暂存记录超出大小限制')
                        record = json.loads(line)
                        self.store.apply(record, source=key, offset=next_offset, signature=signature)
                    except (ValueError, TypeError, KeyError, UnicodeError) as exc:
                        # Quarantine malformed complete records; a broken frame cannot hide later facts.
                        with self.store.write() as db:
                            db.execute('INSERT OR REPLACE INTO archive_issues VALUES(?,?,?)',
                                       (key+':'+str(offset), time.time(), encode({'message': str(exc), 'offset': offset})))
                            db.execute('INSERT OR REPLACE INTO import_offsets VALUES(?,?,?)', (key, next_offset, signature))
                    offset = next_offset
                    count += 1
                    self.committed += 1
                if stream.tell() == offset:
                    with self.store.write() as db:
                        db.execute('DELETE FROM archive_issues WHERE id=?', (key+':tail',))
            sealed = Path(str(path)+'.sealed')
            if sealed.exists() and offset == path.stat().st_size:
                marker = json.loads(sealed.read_text())
                with self.store.read() as db:
                    damaged = db.execute('SELECT 1 FROM archive_issues WHERE id LIKE ? LIMIT 1',(key+'%',)).fetchone()
                if not damaged and marker.get('bytes') == offset and self.store.cursor(key) == (offset,signature):
                    path.unlink() # Only a verified transport copy; original audit files are never touched.
                    sealed.unlink()
            if count >= limit:
                break
        return count

    def status(self):
        try:
            stored = self.store.status()
        except Exception as exc:
            stored = {'available': False, 'storage_error': str(exc)}
        return {'running': self.task is not None and not self.task.done(),
                'error': self.error, 'version_error': self.version_error, 'committed_records': self.committed, **stored}


def envelope(kind, payload, *, identity=None, observed_at=None):
    """Same transport contract for historical backfill and isolated imports."""
    body = encode({'kind': kind, 'payload': payload, 'observed_at': observed_at or time.time()})
    checksum = hashlib.sha256(body.encode()).hexdigest()
    return {'id': identity or checksum, 'body': body, 'sha256': checksum}
