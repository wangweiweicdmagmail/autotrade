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


def test_startup_reports_cache_mismatch_before_first_market_tick(console_app, monkeypatch):
    monkeypatch.setattr(console_app.config, "WATCHLIST", ["NASDAQ:QQQ"])
    monkeypatch.setattr(console_app.collector, "latest", lambda: {})
    issue = {"id": "USER;example", "name": "EMA20-ST-ATR",
             "cached_version": "1.0", "remote_version": "2.0"}
    monkeypatch.setattr(console_app.collector, "cache_issues", {issue["id"]: issue})
    monkeypatch.setattr(console_app.collector, "startup_indicators", {"loaded": 3, "total": 7})
    client = TestClient(console_app.app)

    status = client.get("/api/startup").json()
    assert status["cache_issues"] == [issue]
    assert status["market_progress"] == {
        "indicators": {"loaded": 3, "total": 7},
        "subscribed": 0, "bars_ready": 0, "confirmed": 0, "total": 1,
    }
    assert client.post("/api/startup/repair-pine-cache").status_code == 403


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


def test_cold_browser_launch_uses_one_process_with_two_tabs(console_app, monkeypatch):
    commands = []
    alive = iter([False, True])

    async def fake_alive():
        return next(alive)

    class FakeProcess:
        async def wait(self):
            return 0

    async def fake_exec(*args, **kwargs):
        commands.append(args)
        return FakeProcess()

    async def no_sleep(_seconds):
        return None

    async def fake_tv_ready():
        return None

    async def fake_activate():
        return None

    monkeypatch.setattr(console_app.desktop, "_cdp_alive", fake_alive)
    monkeypatch.setattr(console_app.asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(console_app.asyncio, "sleep", no_sleep)
    monkeypatch.setattr(console_app.desktop, "_tv_ensure_browser", fake_tv_ready)
    monkeypatch.setattr(console_app.desktop, "_cdp_page_id", lambda _prefix: "console-tab")
    monkeypatch.setattr(console_app.desktop, "_activate_console_tab", fake_activate)
    monkeypatch.setattr(console_app.desktop, "_workspace_arrange_split", fake_activate)

    assert run(console_app.desktop._workspace_ensure_browser()) is None
    assert len(commands) == 1
    command = commands[0]
    assert command[:4] == ("open", "-na", "Google Chrome", "--args")
    assert console_app.desktop._CONSOLE_URL in command
    assert console_app.desktop._TV_CHART_URL in command
    assert command.index(console_app.desktop._CONSOLE_URL) < command.index(console_app.desktop._TV_CHART_URL)
    assert console_app.lifecycle._startup_state["split_ready"] is True


def test_existing_browser_only_opens_missing_console_tab(console_app, monkeypatch):
    opened = []
    console_open = False

    async def fake_alive():
        return True

    def fake_page_id(_prefix):
        return "console-tab" if console_open else None

    async def fake_open_tab(url):
        nonlocal console_open
        opened.append(url)
        console_open = True

    async def fake_tv_ready():
        return None

    async def fake_activate():
        return None

    async def unexpected_exec(*args, **kwargs):
        raise AssertionError("已有浏览器不应再启动 Chrome 进程")

    monkeypatch.setattr(console_app.desktop, "_cdp_alive", fake_alive)
    monkeypatch.setattr(console_app.desktop, "_cdp_page_id", fake_page_id)
    monkeypatch.setattr(console_app.desktop, "_cdp_open_tab", fake_open_tab)
    monkeypatch.setattr(console_app.desktop, "_tv_ensure_browser", fake_tv_ready)
    monkeypatch.setattr(console_app.desktop, "_activate_console_tab", fake_activate)
    monkeypatch.setattr(console_app.desktop, "_workspace_arrange_split", fake_activate)
    monkeypatch.setattr(console_app.asyncio, "create_subprocess_exec", unexpected_exec)

    assert run(console_app.desktop._workspace_ensure_browser()) is None
    assert opened == [console_app.desktop._CONSOLE_URL]


@pytest.mark.parametrize("problem,expected", [
    (RuntimeError("需要辅助功能权限"), "辅助功能"),
    (TimeoutError(), "TimeoutError"),
])
def test_split_permission_problem_is_visible_without_browser_retry(console_app, monkeypatch, problem, expected):
    async def ready():
        return None

    async def alive():
        return True

    async def denied():
        raise problem

    monkeypatch.setattr(console_app.desktop, "_cdp_alive", alive)
    monkeypatch.setattr(console_app.desktop, "_cdp_page_id", lambda _url: "existing")
    monkeypatch.setattr(console_app.desktop, "_tv_ensure_browser", ready)
    monkeypatch.setattr(console_app.desktop, "_activate_console_tab", ready)
    monkeypatch.setattr(console_app.desktop, "_workspace_arrange_split", denied)
    assert run(console_app.desktop._workspace_ensure_browser()) is None
    state = console_app.lifecycle._startup_state
    assert state["split_ready"] is False
    assert expected in state["split_error"]
    state["browser_ready"] = True
    stage = next(s for s in console_app.lifecycle._startup_status()["stages"] if s["key"] == "browser")
    assert stage["ready"] is True
    assert expected in stage["detail"]


def test_split_can_be_disabled_without_suppressing_browser_open(console_app, monkeypatch):
    async def ready():
        return None

    async def alive():
        return True

    async def unexpected():
        raise AssertionError("关闭自动分屏后不能发出 UI 操作")

    monkeypatch.setattr(console_app.desktop, "_CONSOLE_AUTO_SPLIT_BROWSER", False)
    monkeypatch.setattr(console_app.desktop, "_cdp_alive", alive)
    monkeypatch.setattr(console_app.desktop, "_cdp_page_id", lambda _url: "existing")
    monkeypatch.setattr(console_app.desktop, "_tv_ensure_browser", ready)
    monkeypatch.setattr(console_app.desktop, "_activate_console_tab", ready)
    monkeypatch.setattr(console_app.desktop, "_workspace_arrange_split", unexpected)
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
