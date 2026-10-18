"""跨进程执行写锁：同一时刻只允许一个订单管理写进程。"""

import fcntl
from pathlib import Path

_lock_handle = None


def acquire_writer_lock(path: str) -> bool:
    global _lock_handle
    if _lock_handle is not None:
        return True
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
    return True


def release_writer_lock() -> None:
    global _lock_handle
    if _lock_handle is None:
        return
    try:
        fcntl.flock(_lock_handle.fileno(), fcntl.LOCK_UN)
    finally:
        _lock_handle.close()
        _lock_handle = None
