from pathlib import Path
from types import SimpleNamespace
import os
import signal
import subprocess
import sys
import time

import pytest

from execution import writer_lock


SCRIPT = Path(__file__).resolve().parents[1] / "restart_live.sh"


@pytest.fixture
def launcher(tmp_path, monkeypatch):
    script = SCRIPT.read_text(encoding="utf-8")
    code = script.split("<<'PY_LAUNCH'\n", 1)[1].split("\nPY_LAUNCH", 1)[0]
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AUTOTRADE_SHUTDOWN_TIMEOUT", "12")
    logs = tmp_path / "logs"
    logs.mkdir()
    log_path = logs / "console-live.log"
    monkeypatch.setattr(sys, "argv", ["-", str(log_path)])

    state = SimpleNamespace(
        now=0, console_exit=6, daemon_exit=8, writer_release=10,
        signals=[], lock_checks=[], launches=[],
    )

    def process_table(*args, **kwargs):
        rows = [" 103 Z /python -m uvicorn console.app:app"]
        if state.now < state.console_exit:
            rows.append(" 101 S /python -m uvicorn console.app:app --port 8000")
        if state.now < state.daemon_exit:
            rows.append(f" 102 S /node {tmp_path}/service/tv_cloud_daemon.js --symbols")
        return SimpleNamespace(stdout="\n".join(rows))

    def acquire(path):
        assert Path(path) == logs / "autotrade_writer.lock"
        state.lock_checks.append(state.now)
        return state.now >= state.writer_release

    def spawn(args, **kwargs):
        state.launches.append((state.now, args))
        return SimpleNamespace(pid=104)

    monkeypatch.setattr(time, "monotonic", lambda: state.now)
    monkeypatch.setattr(time, "sleep", lambda seconds: setattr(state, "now", state.now + seconds))
    monkeypatch.setattr(subprocess, "run", process_table)
    monkeypatch.setattr(subprocess, "Popen", spawn)
    monkeypatch.setattr(os, "kill", lambda pid, sig: state.signals.append((state.now, pid, sig)))
    monkeypatch.setattr(writer_lock, "acquire_writer_lock", acquire)
    monkeypatch.setattr(writer_lock, "release_writer_lock", lambda: None)

    def run():
        exec(compile(code, "restart_live.sh:launch", "exec"), {})

    return state, run, log_path


def test_restart_waits_for_process_exit_and_order_writer_lock(launcher):
    state, run, log_path = launcher
    run()

    assert state.signals == [(0, 101, signal.SIGTERM), (6, 102, signal.SIGTERM)]
    assert state.lock_checks[0] == 8
    assert state.launches == [
        (10, [sys.executable, "-m", "uvicorn", "console.app:app", "--host", "0.0.0.0", "--port", "8000"]),
    ]
    assert "LIVE (restart_live.sh)" in log_path.read_text(encoding="utf-8")


def test_restart_does_not_launch_or_force_kill_a_stuck_console(launcher):
    state, run, _ = launcher
    state.console_exit = 30

    with pytest.raises(SystemExit, match="旧控制台未在时限内退出"):
        run()

    assert state.now == 12
    assert state.signals == [(0, 101, signal.SIGTERM)]
    assert not state.lock_checks
    assert not state.launches


def test_restart_does_not_launch_while_the_writer_lock_is_held(launcher):
    state, run, _ = launcher
    state.writer_release = 30

    with pytest.raises(SystemExit, match="订单写锁仍被占用"):
        run()

    assert state.now == 12
    assert state.lock_checks[0] == 8
    assert not state.launches
