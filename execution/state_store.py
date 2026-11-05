"""Versioned local execution ledger. SQLite is authoritative; JSON is a projection."""

import hashlib
import json
import os
import sqlite3
import tempfile
import time
from contextlib import closing
from pathlib import Path


class PersistenceError(RuntimeError):
    pass


class RecoveryRequired(PersistenceError):
    """A rollback journal must be recovered only after obtaining the writer lock."""


def encode(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def validate(payload):
    if not isinstance(payload, dict):
        raise PersistenceError("状态必须为对象")
    version = payload.get("schema_version", 0)
    if type(version) is not int or version not in (0, 1):
        raise PersistenceError(f"不支持的状态版本 {version}")
    if not isinstance(payload.get("lots"), list) or not isinstance(
        payload.get("pyramid_groups", {}), dict
    ):
        raise PersistenceError("订单或交易组状态格式损坏")
    ids = [row.get("lot_id") for row in payload["lots"] if isinstance(row, dict)]
    if (
        len(ids) != len(payload["lots"])
        or any(not isinstance(key, str) or not key for key in ids)
        or len(set(ids)) != len(ids)
    ):
        raise PersistenceError("订单身份缺失或重复")
    return payload


def atomic_write(path, content):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class ExecutionStateStore:
    SCHEMA = 1

    def __init__(self, legacy_path):
        self.legacy_path = Path(legacy_path)
        self.path = self.legacy_path.with_suffix(".sqlite3")
        self.revision = 0
        self.export_error = ""
        self._dirty_days = set()

    def _legacy(self):
        if not self.legacy_path.exists():
            backup = self.legacy_path.with_suffix(
                self.legacy_path.suffix + ".pre-v1.bak"
            )
            if backup.exists() or any(
                self.legacy_path.parent.glob("trades-????????.json")
            ):
                raise PersistenceError(
                    "存在历史交易或迁移备份但当前账本缺失，禁止按空账本启动"
                )
            return {"lots": [], "pyramid_groups": {}}
        payload = validate(json.loads(self.legacy_path.read_text(encoding="utf-8")))
        if payload.get("storage") == "sqlite":
            raise PersistenceError(
                "权威账本缺失，禁止使用 JSON 导出回退；请恢复 SQLite 备份"
            )
        return payload

    def load(self, *, allow_recovery=False):
        try:
            if not self.path.exists():
                return self._legacy()
            if not allow_recovery and Path(str(self.path) + "-journal").exists():
                raise RecoveryRequired("发现未完成事务日志，等待取得写锁后恢复")
            mode = "rw" if allow_recovery else "ro"
            with closing(
                sqlite3.connect(
                    self.path.resolve().as_uri() + f"?mode={mode}", uri=True
                )
            ) as connection:
                version = connection.execute("PRAGMA user_version").fetchone()[0]
                tables = connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
                if version == 0 and not tables:
                    return (
                        self._legacy()
                    )  # interrupted first migration; no committed ledger
                if version != self.SCHEMA:
                    raise PersistenceError(f"不支持的账本版本 {version}")
                if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                    raise PersistenceError("账本完整性检查失败")
                row = connection.execute(
                    "SELECT revision,payload,digest FROM snapshot WHERE id=1"
                ).fetchone()
                if row is None or hashlib.sha256(row[1].encode()).hexdigest() != row[2]:
                    raise PersistenceError("账本快照缺失或校验失败")
                payload = validate(json.loads(row[1]))
                self.revision = row[0]
                self._dirty_days.update(
                    day
                    for (day,) in connection.execute(
                        "SELECT DISTINCT day FROM archives"
                    )
                )
                return payload
        except (OSError, sqlite3.Error, ValueError, TypeError) as error:
            raise PersistenceError(f"执行状态读取失败：{error}") from error

    def _legacy_archives(self):
        for path in sorted(self.legacy_path.parent.glob("trades-????????.json")):
            records = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(records, list) or any(
                not isinstance(row, dict) for row in records
            ):
                raise PersistenceError(f"历史归档损坏：{path.name}")
            day = path.stem.removeprefix("trades-")
            for index, row in enumerate(records):
                identity = (
                    f"{row['lot_id']}:{row.get('closed_at', 0)}"
                    if row.get("lot_id")
                    else f"legacy:{index}"
                )
                yield day, identity, encode(row)

    def save(self, payload, archive=None, archives=()):
        archives = list(archives) + ([archive] if archive else [])
        payload = {
            **validate(payload),
            "schema_version": self.SCHEMA,
            "storage": "sqlite",
            "saved_at": time.time(),
        }
        content = encode(payload)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with closing(
                sqlite3.connect(self.path, timeout=5, isolation_level=None)
            ) as connection:
                connection.execute("PRAGMA synchronous=FULL")
                connection.execute("BEGIN IMMEDIATE")
                try:
                    version = connection.execute("PRAGMA user_version").fetchone()[0]
                    if version == 0:
                        # Never overwrite an unreadable/unknown database as a fresh ledger.
                        if connection.execute(
                            "SELECT name FROM sqlite_master WHERE type='table'"
                        ).fetchall():
                            raise PersistenceError("未识别的账本结构")
                        original = self._legacy()
                        backup = self.legacy_path.with_suffix(
                            self.legacy_path.suffix + ".pre-v1.bak"
                        )
                        if self.legacy_path.exists() and not backup.exists():
                            atomic_write(
                                backup, self.legacy_path.read_text(encoding="utf-8")
                            )
                        validate(original)
                        connection.execute(
                            "CREATE TABLE snapshot(id INTEGER PRIMARY KEY CHECK(id=1),revision INTEGER NOT NULL,payload TEXT NOT NULL,digest TEXT NOT NULL)"
                        )
                        connection.execute(
                            "CREATE TABLE archives(day TEXT NOT NULL,identity TEXT NOT NULL,payload TEXT NOT NULL,PRIMARY KEY(day,identity))"
                        )
                        connection.executemany(
                            "INSERT OR REPLACE INTO archives VALUES(?,?,?)",
                            self._legacy_archives(),
                        )
                        connection.execute("PRAGMA user_version=1")
                    elif version != self.SCHEMA:
                        raise PersistenceError(f"不支持的账本版本 {version}")
                    current = connection.execute(
                        "SELECT revision FROM snapshot WHERE id=1"
                    ).fetchone()
                    revision = current[0] if current else 0
                    if revision != self.revision:
                        raise PersistenceError(
                            "账本已被其他写入者修改，必须重新加载并对账"
                        )
                    connection.execute(
                        "INSERT OR REPLACE INTO snapshot VALUES(1,?,?,?)",
                        (
                            revision + 1,
                            content,
                            hashlib.sha256(content.encode()).hexdigest(),
                        ),
                    )
                    for day, row in archives:
                        identity = f"{row['lot_id']}:{row.get('closed_at', 0)}"
                        connection.execute(
                            "INSERT OR REPLACE INTO archives VALUES(?,?,?)",
                            (day, identity, encode(row)),
                        )
                    self._before_commit(
                        connection
                    )  # deterministic fault injection boundary
                    connection.execute("COMMIT")
                    self.revision = revision + 1
                except BaseException:
                    connection.execute("ROLLBACK")
                    raise
                self._dirty_days.update(item[0] for item in archives)
                self.export_error = ""
                try:
                    atomic_write(self.legacy_path, content)
                    for day in self._dirty_days:
                        rows = connection.execute(
                            "SELECT payload FROM archives WHERE day=? ORDER BY rowid",
                            (day,),
                        ).fetchall()
                        atomic_write(
                            self.legacy_path.parent / f"trades-{day}.json",
                            encode([json.loads(row[0]) for row in rows]),
                        )
                    self._dirty_days.clear()
                except OSError as error:
                    # COMMIT succeeded: a stale report projection must not pretend the trade failed.
                    self.export_error = str(error)
        except (OSError, sqlite3.Error, ValueError, TypeError) as error:
            raise PersistenceError(f"执行状态写入失败：{error}") from error

    def archive_records(self):
        if not self.revision:
            return [json.loads(row[2]) for row in self._legacy_archives()]
        try:
            with closing(
                sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True)
            ) as connection:
                return [
                    json.loads(row[0])
                    for row in connection.execute(
                        "SELECT payload FROM archives ORDER BY day,rowid"
                    )
                ]
        except (sqlite3.Error, ValueError) as error:
            raise PersistenceError(f"历史账本读取失败：{error}") from error

    def _before_commit(self, connection):
        pass
