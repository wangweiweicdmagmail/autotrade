# -*- coding: utf-8 -*-
"""LIVE：tv_cloud_daemon 分批启动 + 巡检自愈覆盖率实测（真实 TV 连接）。

守的口径：完整 watchlist 启动后 5 分钟内全部标的都有 bar 且 attack_c.pmh
有值。需 TV 凭证（service/creds.json）+ 网络，且会临时占用一条 TV 会话
——只在本机手动验收时跑：python3 -m pytest tests/live/ -m live
"""
import json
import queue
import subprocess
import threading
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.live

ROOT = Path(__file__).resolve().parent.parent.parent

# 与生产 watchlist 一致（config.WATCHLIST）
SYMBOLS = None


def _symbols():
    global SYMBOLS
    if SYMBOLS is None:
        import sys
        sys.path.insert(0, str(ROOT))
        import config
        SYMBOLS = ",".join(config.WATCHLIST)
    return SYMBOLS


def test_daemon_full_coverage():
    syms = _symbols()
    n = len(syms.split(","))
    err = open(Path("/tmp/pytest_daemon_live_stderr.log"), "w")
    proc = subprocess.Popen(
        ["node", "service/tv_cloud_daemon.js", "--symbols", syms],
        cwd=str(ROOT), stdout=subprocess.PIPE, stderr=err,
        text=True, bufsize=1)
    q = queue.Queue()
    threading.Thread(target=lambda: [q.put(l) for l in proc.stdout],
                     daemon=True).start()
    t0 = time.time()
    best_bar = best_atk = 0
    try:
        while time.time() - t0 < 300:
            try:
                line = q.get(timeout=1.0)
            except queue.Empty:
                if proc.poll() is not None:
                    pytest.fail(f"daemon 提前退出 rc={proc.returncode}")
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if msg.get("type") != "tick":
                continue
            ss = msg.get("symbols", {})
            best_bar = max(best_bar, sum(1 for s in ss.values() if s.get("bar")))
            best_atk = max(best_atk, sum(1 for s in ss.values()
                                         if (s.get("attack_c") or {}).get("pmh")
                                         is not None))
            if best_bar >= n and best_atk >= n:
                return   # 全部就绪
        pytest.fail(f"300s 内未全部就绪: bar {best_bar}/{n}"
                    f" attack_c {best_atk}/{n}")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        err.close()
