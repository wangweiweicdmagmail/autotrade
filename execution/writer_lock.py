"""跨进程执行写锁：同一时刻只允许一个订单管理写进程。"""

import fcntl
from pathlib import Path

_lock_handle = None
_lock_owner = None


def acquire_writer_lock(path: str, owner=None) -> bool:
    global _lock_handle, _lock_owner
    if _lock_handle is not None:
        return owner is _lock_owner
    lock_path = Path(path)
    if not lock_path.is_absolute():
        lock_path = Path(__file__).resolve().parent.parent / lock_path
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+", encoding="utf-8")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        return False
    _lock_handle = handle
    _lock_owner = owner
    return True


def release_writer_lock(owner=None) -> None:
    global _lock_handle, _lock_owner
    if owner is not None and owner is not _lock_owner:
        return
    if _lock_handle is None:
        return
    try:
        fcntl.flock(_lock_handle.fileno(), fcntl.LOCK_UN)
    finally:
        _lock_handle.close()
        _lock_handle = None
        _lock_owner = None
