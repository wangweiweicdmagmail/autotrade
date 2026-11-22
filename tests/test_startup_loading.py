# -*- coding: utf-8 -*-
"""启动加载页、首次就绪锁定与控制台/TV 双标签页回归。"""
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from .conftest import run


def _ready_state():
    return {"started_at": 1.0, "core_ready": True, "browser_ready": True,
            "browser_error": "", "ready": False}


def _ready_market():
    return {"NASDAQ:QQQ": {"ts_c": {"trend_state": 1}}}


def test_root_stays_on_loading_until_ready(console_app, monkeypatch):
    state = _ready_state()
    monkeypatch.setattr(console_app.lifecycle, "_startup_state", state)
    client = TestClient(console_app.app)
    response = client.get("/")
    assert response.status_code == 200
    assert "正在准备交易工作区" in response.text
    assert 'id="app" v-cloak' not in response.text

    state["ready"] = True
    response = client.get("/")
    assert response.status_code == 200
    assert 'id="app" v-cloak' in response.text
    assert response.headers["cache-control"] == "no-store"


def test_startup_status_requires_full_confirmed_watchlist(console_app, monkeypatch):
    monkeypatch.setattr(console_app.lifecycle, "_startup_state", _ready_state())
    monkeypatch.setattr(console_app.config, "WATCHLIST", ["NASDAQ:QQQ", "NASDAQ:AAPL"])
    monkeypatch.setattr(console_app.collector, "_running", True)
    monkeypatch.setattr(console_app.collector, "_proc", SimpleNamespace(returncode=None))
    monkeypatch.setattr(console_app.collector, "latest", _ready_market)
    monkeypatch.setattr(console_app.collector, "study_health", lambda: {"systemic": False})
    monkeypatch.setattr(console_app.gateway, "enabled", False)
    monkeypatch.setattr(console_app.gateway, "_connected", False)

    payload = console_app.lifecycle._startup_status()
    market = next(s for s in payload["stages"] if s["key"] == "market")
    ibkr = next(s for s in payload["stages"] if s["key"] == "ibkr")
    assert payload["all_ready"] is False
    assert market["ready"] is False and market["detail"] == "确认指标 1/2"
    assert ibkr["ready"] is True       # dry-run 不强制等待 TWS


def test_startup_reports_local_indicators_without_cloud_cache_actions(console_app, monkeypatch):
    monkeypatch.setattr(console_app.config, "WATCHLIST", ["NASDAQ:QQQ"])
    monkeypatch.setattr(console_app.collector, "latest", lambda: {})
    local = {"loaded": 3, "total": 8, "source": "pinets", "tv_study_subscriptions": 0}
    monkeypatch.setattr(console_app.collector, "startup_indicators", local)
    client = TestClient(console_app.app)
    status = client.get("/api/startup").json()
    assert status["market_progress"]["indicators"] == local
    assert "cache_issues" not in status and "cache_repair_running" not in status
    assert client.post("/api/startup/repair-pine-cache").status_code == 404
    page = client.get("/").text
    assert "repair-pine-cache" not in page and "cache-panel" not in page


def test_live_startup_opens_workspace_with_tws_offline(console_app, monkeypatch):
    monkeypatch.setattr(console_app.lifecycle, "_startup_state", _ready_state())
    monkeypatch.setattr(console_app.config, "WATCHLIST", ["NASDAQ:QQQ"])
    monkeypatch.setattr(console_app.collector, "_running", True)
    monkeypatch.setattr(console_app.collector, "_proc", SimpleNamespace(returncode=None))
    monkeypatch.setattr(console_app.collector, "latest", _ready_market)
    monkeypatch.setattr(console_app.collector, "study_health", lambda: {"systemic": False})
    monkeypatch.setattr(console_app.gateway, "enabled", True)
    monkeypatch.setattr(console_app.gateway, "_connected", False)
    status = console_app.lifecycle._startup_status()
    assert status["all_ready"] is False
    assert status["workspace_ready"] is True
    ibkr = next(stage for stage in status["stages"] if stage["key"] == "ibkr")
    assert ibkr["ready"] is False and ibkr["blocking"] is False

    monkeypatch.setattr(console_app.desktop, "_CONSOLE_AUTO_OPEN_BROWSER", False)

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(console_app.asyncio, "sleep", no_sleep)
    run(console_app.desktop._startup_prepare())
    client = TestClient(console_app.app)
    assert 'id="app" v-cloak' in client.get("/").text

    monkeypatch.setattr(console_app.gateway, "_connected", True)
    assert console_app.lifecycle._startup_status()["all_ready"] is True


def test_first_readiness_is_latched(console_app, monkeypatch):
    state = _ready_state()
    monkeypatch.setattr(console_app.lifecycle, "_startup_state", state)
    monkeypatch.setattr(console_app.config, "WATCHLIST", ["NASDAQ:QQQ"])
    monkeypatch.setattr(console_app.collector, "_running", True)
    monkeypatch.setattr(console_app.collector, "_proc", SimpleNamespace(returncode=None))
    monkeypatch.setattr(console_app.collector, "latest", _ready_market)
    monkeypatch.setattr(console_app.collector, "study_health", lambda: {"systemic": False})
    monkeypatch.setattr(console_app.gateway, "enabled", False)
    monkeypatch.setattr(console_app.gateway, "_connected", False)
    monkeypatch.setattr(console_app.desktop, "_CONSOLE_AUTO_OPEN_BROWSER", False)

    async def no_sleep(_seconds):
        return None
    monkeypatch.setattr(console_app.asyncio, "sleep", no_sleep)
    run(console_app.desktop._startup_prepare())
    assert state["ready"] is True

    monkeypatch.setattr(console_app.collector, "_running", False)
    assert console_app.lifecycle._startup_status()["ready"] is True


def test_cold_launch_opens_eight_grid_then_prepares_three_independent_windows(console_app, monkeypatch):
    from controller import browser_workspace
    commands=[];alive=iter([False, True]);seen=[]
    async def fake_alive():return next(alive)
    async def fake_exec(*args,**kwargs):
        commands.append(args)
        async def wait():return 0
        return SimpleNamespace(wait=wait)
    async def no_sleep(_seconds):pass
    async def windows(endpoint,urls):
        seen.extend(urls);return {url:'target-'+str(i) for i,url in enumerate(urls)}
    async def activate():pass
    monkeypatch.setattr(console_app.desktop,'_cdp_alive',fake_alive)
    monkeypatch.setattr(console_app.asyncio,'create_subprocess_exec',fake_exec)
    monkeypatch.setattr(console_app.asyncio,'sleep',no_sleep)
    monkeypatch.setattr(browser_workspace,'ensure_independent_windows',windows)
    monkeypatch.setattr(console_app.desktop,'_activate_console_tab',activate)
    assert run(console_app.desktop._workspace_ensure_browser()) is None
    assert len(commands)==1 and commands[0][-1]==console_app.desktop._TV_CHART_URL
    assert seen==[console_app.desktop._TV_CHART_URL,console_app.desktop._TV_FOCUS_CHART_URL,console_app.desktop._CONSOLE_URL]
    assert console_app.lifecycle._startup_state['split_ready']
    assert console_app.desktop._tv_bindings['target-2']['chart_id']=='target-0'


def test_window_error_is_visible_without_holding_browser_readiness(console_app,monkeypatch):
    from controller import browser_workspace
    async def alive():return True
    async def fail(*_):raise RuntimeError('图表存在多个副本')
    async def activate():pass
    monkeypatch.setattr(console_app.desktop,'_cdp_alive',alive)
    monkeypatch.setattr(browser_workspace,'ensure_independent_windows',fail)
    monkeypatch.setattr(console_app.desktop,'_activate_console_tab',activate)
    assert run(console_app.desktop._workspace_ensure_browser()) is None
    assert '多个副本' in console_app.lifecycle._startup_state['split_error']
    console_app.lifecycle._startup_state['browser_ready']=True
    stage=next(s for s in console_app.lifecycle._startup_status()['stages'] if s['key']=='browser')
    assert stage['ready'] and '多个副本' in stage['detail']


def test_window_preparation_can_be_disabled_without_ui_commands(console_app,monkeypatch):
    from controller import browser_workspace
    async def alive():return True
    async def forbidden(*_):raise AssertionError('disabled')
    monkeypatch.setattr(console_app.desktop,'_cdp_alive',alive)
    monkeypatch.setattr(console_app.desktop,'_CONSOLE_AUTO_ARRANGE_WINDOWS',False)
    monkeypatch.setattr(browser_workspace,'ensure_independent_windows',forbidden)
    assert run(console_app.desktop._workspace_ensure_browser()) is None


def test_browser_waits_for_http_but_not_full_trading_readiness(console_app, monkeypatch):
    state = _ready_state()
    state["browser_ready"] = False
    monkeypatch.setattr(console_app.lifecycle, "_startup_state", state)
    monkeypatch.setattr(console_app.desktop, "_CONSOLE_AUTO_OPEN_BROWSER", True)
    results = iter([False, False, True])
    calls = []

    async def probe():
        result = next(results)
        calls.append(("http", result))
        assert state["ready"] is False
        return result

    async def open_browser():
        assert calls[-1] == ("http", True)
        calls.append(("browser", True))
        return None

    async def no_sleep(_seconds):
        pass

    monkeypatch.setattr(console_app.desktop, "_console_http_ready", probe)
    monkeypatch.setattr(console_app.desktop, "_workspace_ensure_browser", open_browser)
    monkeypatch.setattr(console_app.asyncio, "sleep", no_sleep)
    monkeypatch.setattr(console_app.lifecycle, "_startup_status", lambda: {"workspace_ready": True})
    run(console_app.desktop._startup_prepare())
    assert calls == [("http", False), ("http", False), ("http", True), ("browser", True)]
    assert state["ready"] and state["browser_ready"]


@pytest.mark.parametrize("payload,status,expected", [
    ({"ready": False, "stages": []}, 200, True),
    ({"ready": True, "stages": []}, 503, False),
    ({"detail": "not the startup endpoint"}, 200, False),
])
def test_http_gate_accepts_loading_response(console_app, monkeypatch, payload, status, expected):
    import io
    import json
    import urllib.request

    class Response(io.BytesIO):
        pass

    def open_url(url, timeout):
        assert url == "http://127.0.0.1:8801/api/startup"
        response = Response(json.dumps(payload).encode())
        response.status = status
        return response

    monkeypatch.setattr(console_app.desktop, "_CONSOLE_URL", "http://127.0.0.1:8801/")
    monkeypatch.setattr(urllib.request, "build_opener", lambda *_args: SimpleNamespace(open=open_url))
    assert run(console_app.desktop._console_http_ready()) is expected


def test_startup_names_partial_subscription_after_timeout(console_app,monkeypatch):
    state=_ready_state()
    monkeypatch.setattr(console_app.lifecycle,'_startup_state',state)
    monkeypatch.setattr(console_app.config,'WATCHLIST',['NASDAQ:QQQ','NYSE:UBER'])
    monkeypatch.setattr(console_app.collector,'latest',_ready_market)
    monkeypatch.setattr(console_app.collector,'study_health',lambda:{'systemic':False})
    status=console_app.lifecycle._startup_status()
    assert status['market_pending']==['NYSE:UBER']
    assert 'NYSE:UBER' in status['error']
    assert not status['all_ready']


def test_offline_live_gateway_rejects_order_operations():
    from execution.ibkr import IBKRGateway

    async def exercise():
        gateway = IBKRGateway("127.0.0.1", 7496, 11, "", "", enabled=True)
        unit = SimpleNamespace(side="long", entry_px=100)
        for result in (
            await gateway.place_bracket(unit),
            await gateway.place_market_with_stop("NASDAQ:QQQ", "long", 1, 99),
            await gateway.place_midpoint_oca("NASDAQ:QQQ", "long", 1, 100, 101, 99, "test"),
        ):
            assert result["ok"] is False
            assert "未连接" in result["error"]
        assert gateway.cancel_order(None) is False

    run(exercise())


def test_24h_session_rejection_is_visible_without_blocking_workspace(console_app, monkeypatch):
    monkeypatch.setattr(console_app.lifecycle, "_startup_state", _ready_state())
    monkeypatch.setattr(console_app.config, "WATCHLIST", ["NASDAQ:QQQ", "NASDAQ:AAPL"])
    collector = console_app.collector
    monkeypatch.setattr(collector, "_running", True)
    monkeypatch.setattr(collector, "_proc", SimpleNamespace(returncode=None))
    monkeypatch.setattr(collector, "latest", _ready_market)
    monkeypatch.setattr(collector, "market_session", "24h")
    monkeypatch.setattr(collector, "market_sessions", {"NASDAQ:AAPL": {"resolved": "extended"}})

    status = console_app.lifecycle._startup_status()
    assert status["workspace_ready"] and not status["all_ready"]
    assert status["market_pending"] == []
    assert "NASDAQ:AAPL（extended）" in status["error"]
    assert status["market_session"]["unavailable"]["NASDAQ:AAPL"]["resolved"] == "extended"
