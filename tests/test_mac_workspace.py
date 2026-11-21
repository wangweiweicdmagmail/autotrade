"""Native driver logic without loading macOS frameworks or touching a browser."""

import pytest

from controller.mac_workspace import NativeUI


def driver():
    return object.__new__(NativeUI)


def test_walk_does_not_expand_trading_page():
    ui = driver()
    children = {"window": ["chrome", "web"], "chrome": ["button"], "button": []}
    ui.array = lambda node: children[node]  # Accessing web children would fail.
    ui.text = lambda node, name: "AXWebArea" if node == "web" else "AXGroup"
    assert list(ui.walk("window")) == ["window", "chrome", "button", "web"]


def test_click_rejects_a_different_chrome_pid():
    ui = driver()
    ui.pid = 9138

    class API:
        def AXUIElementGetPid(self, _node, found):
            found._obj.value = 1040
            return 0

        def AXUIElementPerformAction(self, *_args):
            raise AssertionError("Cannot act on the ordinary Chrome")

    ui.ax = API()
    with pytest.raises(RuntimeError, match="进程不匹配"):
        ui.click("ordinary-chrome-node")


def test_two_separate_windows_do_not_count_as_native_split():
    ui = driver()
    ui.windows = lambda: ["left-window", "right-window"]
    ui.walk = lambda window: iter(["console"] if window == "left-window" else ["chart"])
    ui.text = lambda node, name: "AXWebArea" if name == "AXRole" else node + "://page"
    ui.geometry = lambda node, name, kind: (600, 800) if kind == 2 else (0 if node == "console" else 600, 100)
    assert ui.positions("console://", "chart://") == {"console": (0, 100)}
    ui.windows = lambda: ["split-window"]
    ui.walk = lambda _window: iter(["console", "chart"])
    assert len(ui.positions("console://", "chart://")) == 2


def test_correct_split_is_reused_without_clicks():
    ui = driver()
    ui.prepare = lambda: None
    ui.positions = lambda *_args: {"console": (1000, 100), "chart": (100, 100)}
    ui.click = lambda _node: pytest.fail("Existing split must not be clicked")
    ui.split("console://", "chart://", "Chart", "tradingview.com")


def test_picker_uses_static_text_value_and_reuses_pending_picker():
    ui = driver()
    clicked = []
    ui.prepare = lambda: None
    ui.positions = lambda *_args: {"console": (1000, 100), **({"chart": (100, 100)} if clicked else {})}
    ui.nodes = lambda: iter(["picker"])
    ui.walk = lambda *_args, **_kwargs: iter(["row"])

    def text(node, name="AXTitle"):
        return {("picker", "AXRole"): "AXWebArea",
                ("picker", "AXURL"): "chrome://tab-search.top-chrome/split_new_tab_page.html",
                ("row", "AXRole"): "AXStaticText",
                ("row", "AXValue"): "autotrade 127.0.0.1:8000 已打开的标签页"}.get((node, name), "")

    ui.text = text
    ui.click = clicked.append
    ui.split("console://", "chart://", "autotrade", "127.0.0.1:8000")
    assert clicked == ["row"]


def test_existing_console_left_split_is_reversed_once():
    ui = driver()
    calls = []
    ui.prepare = lambda: None
    ui.positions = lambda *_: {"console": (1000 if calls else 100, 100),
                               "chart": (100 if calls else 1000, 100)}
    ui.adjust = lambda titles: calls.append(titles)
    ui.split("console://", "chart://", "Chart", "tradingview.com")
    assert len(calls) == 1
    assert "反转视图" in calls[0]


def test_split_swap_waits_for_layout_without_toggling_back(monkeypatch):
    ui = driver()
    calls = []
    samples = iter([False, False, False, False, True])
    ui.prepare = lambda: None
    def positions(*_):
        ready = next(samples)
        return {"console": (1000 if ready else 100, 100),
                "chart": (100 if ready else 1000, 100)}
    ui.positions = positions
    ui.adjust = lambda titles: calls.append(titles)
    monkeypatch.setattr("controller.mac_workspace.time.sleep", lambda _: None)
    ui.split("console://", "chart://", "Chart", "tradingview.com")
    assert len(calls) == 1
