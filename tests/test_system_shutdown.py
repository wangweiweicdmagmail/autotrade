# -*- coding: utf-8 -*-
"""手动关闭系统：本机权限、关闭顺序、写请求封锁与 IBKR 交易门。"""
import asyncio
import signal

import pytest
from fastapi.testclient import TestClient

from execution.ibkr import IBKRGateway
from .conftest import run


def _gateway():
    return IBKRGateway("127.0.0.1", 7496, 99, "F_TEST", "", enabled=False)


def test_gateway_rejects_async_and_sync_controls_after_disable():
    gw = _gateway()
    run(gw.disable_control())
    assert gw.auto_reconnect is False and gw._connected is False
    with pytest.raises(RuntimeError, match="已禁止 IBKR 交易控制"):
        run(gw.place_close("NASDAQ:QQQ", "long", 1, 100.0))
    with pytest.raises(RuntimeError, match="已禁止 IBKR 交易控制"):
        gw.cancel_order(None)


def test_disable_waits_for_active_control_call():
    gw = _gateway()

    async def scenario():
        gw._begin_control()
        disabling = asyncio.create_task(gw.disable_control())
        await asyncio.sleep(0.01)
        assert gw._active_controls == 1 and not disabling.done()
        gw._active_controls -= 1
        await disabling

    run(scenario())
    assert gw._active_controls == 0 and gw._control_allowed is False


def test_local_shutdown_sequence_and_write_lock(console_app, monkeypatch):
    calls = []
    monkeypatch.setattr(console_app.system, "_system_stopping", False)

    async def disable():
        calls.append("disable_control")

    async def stop_copy():
        calls.append("stop_copy")

    async def close_tws():
        calls.append("close_tws")
        return True

    monkeypatch.setattr(console_app.gateway, "disable_control", disable)
    monkeypatch.setattr(console_app.copy_supervisor, "stop_all", stop_copy)
    monkeypatch.setattr(console_app.system, "_close_tws_process", close_tws)
    monkeypatch.setattr(console_app.system, "_schedule_console_exit", lambda: calls.append("exit"))
    client = TestClient(console_app.app, client=("127.0.0.1", 50000))

    response = client.post("/api/system/shutdown")
    assert response.status_code == 200
    assert response.json()["ok"] is True and response.json()["tws_closed"] is True
    assert calls == ["disable_control", "stop_copy", "close_tws", "exit"]

    response = client.post("/api/auto/stop")
    assert response.status_code == 503
    assert "已停止交易控制" in response.text


def test_remote_client_cannot_shutdown(console_app, monkeypatch):
    monkeypatch.setattr(console_app.system, "_system_stopping", False)

    async def forbidden():
        raise AssertionError("远程请求不得进入关闭流程")

    monkeypatch.setattr(console_app.gateway, "disable_control", forbidden)
    client = TestClient(console_app.app)
    response = client.post("/api/system/shutdown")
    assert response.status_code == 403
    assert console_app.system._system_stopping is False


def test_tws_force_kill_fallback(console_app, monkeypatch):
    killed = []

    async def pids():
        return [123]

    async def quit_tws(_script):
        return ""

    waits = iter([[123], [123], []])

    async def wait(_pids, _timeout):
        return next(waits)

    monkeypatch.setattr(console_app.system, "_tws_pids", pids)
    monkeypatch.setattr(console_app.desktop, "_osascript", quit_tws)
    monkeypatch.setattr(console_app.system, "_wait_tws_exit", wait)
    monkeypatch.setattr(console_app.os, "kill", lambda pid, sig: killed.append((pid, sig)))

    assert run(console_app.system._close_tws_process()) is True
    assert killed == [(123, signal.SIGTERM), (123, signal.SIGKILL)]


def test_tws_already_closed_is_success(console_app, monkeypatch):
    async def pids():
        return []
    monkeypatch.setattr(console_app.system, "_tws_pids", pids)
    assert run(console_app.system._close_tws_process()) is True
