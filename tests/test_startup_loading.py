# -*- coding: utf-8 -*-
"""启动加载页、首次就绪锁定与控制台/TV 双标签页回归。"""
from types import SimpleNamespace

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


def test_live_startup_requires_ibkr(console_app, monkeypatch):
    monkeypatch.setattr(console_app.lifecycle, "_startup_state", _ready_state())
    monkeypatch.setattr(console_app.config, "WATCHLIST", ["NASDAQ:QQQ"])
    monkeypatch.setattr(console_app.collector, "_running", True)
    monkeypatch.setattr(console_app.collector, "_proc", SimpleNamespace(returncode=None))
    monkeypatch.setattr(console_app.collector, "latest", _ready_market)
    monkeypatch.setattr(console_app.collector, "study_health", lambda: {"systemic": False})
    monkeypatch.setattr(console_app.gateway, "enabled", True)
    monkeypatch.setattr(console_app.gateway, "_connected", False)
    assert console_app.lifecycle._startup_status()["all_ready"] is False

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

    assert run(console_app.desktop._workspace_ensure_browser()) is None
    assert len(commands) == 1
    command = commands[0]
    assert command[:4] == ("open", "-na", "Google Chrome", "--args")
    assert console_app.desktop._CONSOLE_URL in command
    assert console_app.desktop._TV_CHART_URL in command


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
    monkeypatch.setattr(console_app.asyncio, "create_subprocess_exec", unexpected_exec)

    assert run(console_app.desktop._workspace_ensure_browser()) is None
    assert opened == [console_app.desktop._CONSOLE_URL]
