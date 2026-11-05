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
    ui.positions = lambda *_args: {"console": (100, 100), "chart": (1000, 100)}
    ui.click = lambda _node: pytest.fail("Existing split must not be clicked")
    ui.split("console://", "chart://", "Chart", "tradingview.com")


def test_picker_uses_static_text_value_and_reuses_pending_picker():
    ui = driver()
    clicked = []
    ui.prepare = lambda: None
    ui.positions = lambda *_args: {"console": (100, 100), **({"chart": (1000, 100)} if clicked else {})}
    ui.nodes = lambda: iter(["picker"])
    ui.walk = lambda *_args, **_kwargs: iter(["row"])

    def text(node, name="AXTitle"):
        return {("picker", "AXRole"): "AXWebArea",
                ("picker", "AXURL"): "chrome://tab-search.top-chrome/split_new_tab_page.html",
                ("row", "AXRole"): "AXStaticText",
                ("row", "AXValue"): "Chart cn.tradingview.com 已打开的标签页"}.get((node, name), "")

    ui.text = text
    ui.click = clicked.append
    ui.split("console://", "chart://", "Chart", "cn.tradingview.com")
    assert clicked == ["row"]
