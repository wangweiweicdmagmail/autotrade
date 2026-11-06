"""Append-only audit of the market snapshots published to the execution model."""

import asyncio
from contextlib import closing
from contextvars import ContextVar
import hashlib
import json
import logging
from pathlib import Path
import sqlite3
import zlib

logger = logging.getLogger(__name__)
decision_market_frame: ContextVar[str | None] = ContextVar("decision_market_frame", default=None)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS market_frames (
    id INTEGER PRIMARY KEY, received_at REAL NOT NULL, source_at REAL
);
CREATE TABLE IF NOT EXISTS market_history (
    digest BLOB PRIMARY KEY, payload BLOB NOT NULL
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS market_versions (
    id INTEGER PRIMARY KEY, payload BLOB NOT NULL, history_digest BLOB
);
CREATE TABLE IF NOT EXISTS market_members (
    frame_id INTEGER NOT NULL, symbol TEXT NOT NULL, version_id INTEGER NOT NULL,
    PRIMARY KEY (frame_id, symbol)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS market_members_symbol ON market_members(symbol, frame_id);
"""
_HISTORY_KEY = "opening_breakout_history"


def _encode(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()


class LiveMarketAudit:
    def __init__(self, path: Path):
        self.path = path
        self._queue = asyncio.Queue(maxsize=16)
        self._task = None
        self._versions = {}
        self._last_id = 0
        self._last_committed = None
        self._dropped = 0
        self._error = ""

    def start(self):
        if self._task is None:
            self._task = asyncio.create_task(self._run())

    async def stop(self):
        if self._task is None:
            return
        await self._queue.put(None)
        await self._task
        self._task = None

    def capture(self, snapshot, received_at, source_at):
        """Enqueue a published collector snapshot, which is replaced on change."""
        if self._task is None:
            return None
        self._last_id = max(round(received_at * 1_000_000_000), self._last_id + 1)
        frame = (self._last_id, received_at, source_at, snapshot)
        try:
            self._queue.put_nowait(frame)
        except asyncio.QueueFull:
            self._dropped += 1
            if self._dropped == 1:
                logger.error("行情审计写入队列已满，后续缺口可见于审计状态")
            return None
        return str(frame[0])

    def status(self):
        return {
            "running": self._task is not None and not self._task.done(),
            "queued": self._queue.qsize(),
            "dropped": self._dropped,
            "last_committed_frame": self._last_committed,
            "error": self._error,
        }

    async def _run(self):
        while True:
            first = await self._queue.get()
            if first is None:
                return
            batch = [first]
            stopping = False
            while len(batch) < 8 and not self._queue.empty():
                frame = self._queue.get_nowait()
                if frame is None:
                    stopping = True
                    break
                batch.append(frame)
            try:
                await asyncio.to_thread(self._write, batch)
                self._last_committed = str(batch[-1][0])
                self._error = ""
            except (OSError, sqlite3.Error, ValueError, TypeError) as exc:
                self._dropped += len(batch)
                error = str(exc)
                if error != self._error:
                    logger.exception("行情审计写入失败")
                self._error = error
            if stopping:
                return

    def _write(self, frames):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        versions = self._versions.copy()
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(_SCHEMA)
            with db:
                for frame_id, received_at, source_at, snapshot in frames:
                    db.execute("INSERT INTO market_frames VALUES (?, ?, ?)", (frame_id, received_at, source_at))
                    members = []
                    for symbol, row in snapshot.items():
                        previous, version, digest = versions.get(symbol, (None, None, None))
                        if row is not previous and row != previous:
                            payload = {key: value for key, value in row.items() if key != _HISTORY_KEY}
                            if _HISTORY_KEY not in row:
                                digest = None
                            elif digest is None or row[_HISTORY_KEY] != previous.get(_HISTORY_KEY):
                                history = _encode(row[_HISTORY_KEY])
                                digest = hashlib.sha256(history).digest()
                                db.execute("INSERT OR IGNORE INTO market_history VALUES (?, ?)",
                                           (digest, zlib.compress(history, 1)))
                            version = db.execute(
                                "INSERT INTO market_versions(payload, history_digest) VALUES (?, ?)",
                                (zlib.compress(_encode(payload), 1), digest),
                            ).lastrowid
                            versions[symbol] = (row, version, digest)
                        members.append((frame_id, symbol, version))
                    db.executemany("INSERT INTO market_members VALUES (?, ?, ?)", members)
        self._versions = versions

    def read(self, symbol, *, frame_id=None, before=2**63 - 1, limit=100):
        """Return exact accepted per-symbol snapshots, newest first."""
        if not self.path.exists():
            return []
        with closing(sqlite3.connect(f"{self.path.resolve().as_uri()}?mode=ro", uri=True)) as db:
            rows = db.execute(
                """SELECT f.id, f.received_at, f.source_at, v.payload, h.payload
                   FROM market_members m
                   JOIN market_frames f ON f.id = m.frame_id
                   JOIN market_versions v ON v.id = m.version_id
                   LEFT JOIN market_history h ON h.digest = v.history_digest
                   WHERE m.symbol = ? AND m.frame_id <= ?
                   ORDER BY m.frame_id DESC LIMIT ?""",
                (symbol, frame_id if frame_id is not None else before, limit),
            ).fetchall()
        result = []
        for key, received_at, source_at, payload, history in rows:
            if frame_id is not None and key != frame_id:
                break
            row = json.loads(zlib.decompress(payload))
            if history is not None:
                row[_HISTORY_KEY] = json.loads(zlib.decompress(history))
            result.append({"frame_id": str(key), "received_at": received_at, "source_at": source_at, "data": row})
        return result
