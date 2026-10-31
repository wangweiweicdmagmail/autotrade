# -*- coding: utf-8 -*-
"""客户端看盘（/api/tv/view「每人一格」+ 截图回传）回归。

守的口径：trader→pane 分配（最小空位/复用/失效清理/两小时未用回收/满员
返回 None）；master 直通（只切 pane 0 不截图）；客户端全链路（切 pane →
就绪轮询 → clip 截图 → base64 回传）；已同标的 fast-path（不再轮询就绪）；
画面位已满 → code=pane_full；TV 不就绪 → 原因透传。CDP 全 fake，零浏览器。
"""
import json
import time

import pytest

from .conftest import run

RECT = {"x": 10, "y": 20, "w": 800, "h": 600}


class FakeTV:
    """假 CDP 控制器：按 JS 内容路由（pane 列表 / 切格 / 就绪探针）。"""
    instances = []
    panes_n = 4
    already_on = False       # True 时目标 pane 已是该标的（fast-path）

    def __init__(self):
        self.clip = None
        self.ready_polls = 0

    async def __aenter__(self):
        FakeTV.instances.append(self)
        return self

    async def __aexit__(self, *a):
        pass

    async def evaluate(self, js):
        if "return JSON.stringify(panes.map" in js:        # _TV_PANES_JS
            return json.dumps(
                [{"symbol": f"NASDAQ:S{i}", "rect": RECT}
                 for i in range(FakeTV.panes_n)])
        if "needSwitch" in js:                            # _TV_PANE_SHOW_JS
            return json.dumps({
                "ok": True, "switched": not FakeTV.already_on,
                "before": "NASDAQ:NVDA" if FakeTV.already_on else "NASDAQ:AAPL",
                "after": "NASDAQ:NVDA", "rect": RECT,
                "panes": FakeTV.panes_n})
        if "size() > 5" in js:                            # _TV_PANE_READY_JS
            self.ready_polls += 1
            return "true"
        raise AssertionError(f"未识别 JS: {js[:120]}")

    async def screenshot(self, quality=70, clip=None):
        self.clip = clip
        return b"\xff\xd8fake-jpeg"


@pytest.fixture
def app(console_app, monkeypatch, tmp_path):
    import console.app as a
    monkeypatch.setattr(a, "_TV_PANES_PATH", tmp_path / "tv_panes.json")
    monkeypatch.setattr(a, "_tv_ensure_browser",
                        lambda: _none_coro())
    FakeTV.instances = []
    FakeTV.panes_n = 4
    FakeTV.already_on = False
    import controller.tv_cdp as cdp
    monkeypatch.setattr(cdp, "TradingViewCDPController", FakeTV)
    return a


async def _none_coro():
    return None


# ---------------------------------------------------------------------------
# pane 分配（纯逻辑）
# ---------------------------------------------------------------------------

def test_assign_lowest_free_and_reuse(app):
    """最小空位分配；同一 trader 复用并刷新 last_used；pane 0 永不分配。"""
    assert app._tv_assign_pane("alice", 4) == 1
    assert app._tv_assign_pane("bob", 4) == 2
    assert app._tv_assign_pane("alice", 4) == 1      # 复用
    m = app._tv_load_pane_map()
    assert m["alice"]["pane"] == 1 and m["bob"]["pane"] == 2


def test_assign_layout_shrink_drops_stale(app):
    """布局缩小（4 格→2 格）：越界映射清理（bob 的 pane 2 失效）；
    仍有效的 alice（pane 1）不受影响，新客户端无位 → None。"""
    app._tv_assign_pane("alice", 4)                  # pane 1
    app._tv_assign_pane("bob", 4)                    # pane 2（布局缩小后越界）
    assert app._tv_assign_pane("carol", 2) is None   # pane 1 仍被 alice 占着
    m = app._tv_load_pane_map()
    assert "bob" not in m and m["alice"]["pane"] == 1


def test_assign_full_reclaims_stale(app):
    """满员时回收两小时未用的旧映射；都在用 → None（画面位已满）。"""
    app._tv_assign_pane("alice", 2)                  # pane 1（仅存的客户端位）
    # 都新鲜 → 满
    assert app._tv_assign_pane("bob", 2) is None
    # alice 两小时未用 → 回收给 bob
    m = app._tv_load_pane_map()
    m["alice"]["last_used"] = time.time() - (2 * 3600 + 1)
    app._tv_save_pane_map(m)
    assert app._tv_assign_pane("bob", 2) == 1
    assert "alice" not in app._tv_load_pane_map()


# ---------------------------------------------------------------------------
# /api/tv/view 端点
# ---------------------------------------------------------------------------

def test_master_passthrough_no_screenshot(app, monkeypatch):
    """master：走 _tv_show_symbol 只切 pane 0，不碰 CDP 截图链路。"""
    called = {}

    async def fake_show(symbol):
        called["symbol"] = symbol
        return {"ok": True, "shown": symbol}
    monkeypatch.setattr(app, "_tv_show_symbol", fake_show)
    req = app.TvShowRequest(symbol="NASDAQ:NVDA")
    out = run(app.api_tv_view(req, trader="master"))
    assert out["ok"] and out["master"] is True
    assert called["symbol"] == "NASDAQ:NVDA"
    assert not FakeTV.instances          # master 不开截图 CDP 连接


def test_client_full_flow_screenshot(app):
    """客户端：分配 pane → 切标的 → 就绪轮询 → 按 pane rect clip 截图回传。"""
    req = app.TvShowRequest(symbol="NASDAQ:NVDA")
    out = run(app.api_tv_view(req, trader="alice"))
    assert out["ok"] and out["pane"] == 1 and out["shown"] == "NASDAQ:NVDA"
    import base64
    assert base64.b64decode(out["image"]) == b"\xff\xd8fake-jpeg"
    tv = FakeTV.instances[-1]
    assert tv.clip == {"x": 10, "y": 20, "width": 800, "height": 600}
    assert tv.ready_polls >= 1           # 切换后有就绪轮询
    # 映射落盘
    assert app._tv_load_pane_map()["alice"]["pane"] == 1


def test_client_fast_path_no_ready_poll(app):
    """该 pane 已是目标标的：跳过 setSymbol，也不轮询就绪，直接截图。"""
    FakeTV.already_on = True
    req = app.TvShowRequest(symbol="NASDAQ:NVDA")
    out = run(app.api_tv_view(req, trader="alice"))
    assert out["ok"]
    assert FakeTV.instances[-1].ready_polls == 0


def test_client_pane_full(app):
    """只有 1 格（master 独占）→ code=pane_full，原因带「画面位已满」。"""
    FakeTV.panes_n = 1
    req = app.TvShowRequest(symbol="NASDAQ:NVDA")
    out = run(app.api_tv_view(req, trader="alice"))
    assert out["ok"] is False and out["code"] == "pane_full"
    assert "画面位已满" in out["reason"]


def test_client_tv_not_ready(app, monkeypatch):
    """TV 不就绪（浏览器没起）→ 原因透传，不分配 pane。"""
    async def not_ready():
        return "浏览器未启动"
    monkeypatch.setattr(app, "_tv_ensure_browser", not_ready)
    req = app.TvShowRequest(symbol="NASDAQ:NVDA")
    out = run(app.api_tv_view(req, trader="alice"))
    assert out["ok"] is False and "浏览器未启动" in out["reason"]
    assert app._tv_load_pane_map() == {}


def test_empty_symbol_400(app):
    """symbol 空 → 400。"""
    from fastapi import HTTPException
    with pytest.raises(HTTPException):
        run(app.api_tv_view(app.TvShowRequest(symbol="  "), trader="alice"))
