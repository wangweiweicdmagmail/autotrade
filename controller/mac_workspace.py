"""PID-bound macOS accessibility driver for the dedicated Chrome workspace.

No AppleScript process-name references, browser extensions, or trading imports.
Native handles keep their PID even when two Chrome processes share one name.
"""

import ctypes as C
import sys
import time


class Point(C.Structure):
    _fields_ = [("x", C.c_double), ("y", C.c_double)]


class NativeUI:
    def __init__(self, pid):
        self.pid = pid
        self.refs = []
        self.cf = C.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
        self.ax = C.CDLL("/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices")
        signatures = {
            "CFStringCreateWithCString": (C.c_void_p, [C.c_void_p, C.c_char_p, C.c_uint32]),
            "CFStringGetCString": (C.c_bool, [C.c_void_p, C.c_char_p, C.c_long, C.c_uint32]),
            "CFArrayGetCount": (C.c_long, [C.c_void_p]),
            "CFArrayGetValueAtIndex": (C.c_void_p, [C.c_void_p, C.c_long]),
            "CFRelease": (None, [C.c_void_p]),
            "CFGetTypeID": (C.c_ulong, [C.c_void_p]),
            "CFStringGetTypeID": (C.c_ulong, []),
            "CFURLGetTypeID": (C.c_ulong, []),
            "CFURLGetString": (C.c_void_p, [C.c_void_p]),
        }
        for name, (result, args) in signatures.items():
            fn = getattr(self.cf, name)
            fn.restype, fn.argtypes = result, args
        signatures = {
            "AXIsProcessTrusted": (C.c_bool, []),
            "AXUIElementCreateApplication": (C.c_void_p, [C.c_int]),
            "AXUIElementGetPid": (C.c_int, [C.c_void_p, C.POINTER(C.c_int)]),
            "AXUIElementCopyAttributeValue": (C.c_int, [C.c_void_p, C.c_void_p, C.POINTER(C.c_void_p)]),
            "AXUIElementSetAttributeValue": (C.c_int, [C.c_void_p, C.c_void_p, C.c_void_p]),
            "AXUIElementPerformAction": (C.c_int, [C.c_void_p, C.c_void_p]),
            "AXValueGetValue": (C.c_bool, [C.c_void_p, C.c_int, C.c_void_p]),
            "CGEventCreateMouseEvent": (C.c_void_p, [C.c_void_p, C.c_uint32, Point, C.c_uint32]),
            "CGEventPost": (None, [C.c_uint32, C.c_void_p]),
        }
        for name, (result, args) in signatures.items():
            fn = getattr(self.ax, name)
            fn.restype, fn.argtypes = result, args
        if not self.ax.AXIsProcessTrusted():
            raise RuntimeError("运行控制台的应用尚未获得 macOS 辅助功能权限")
        self.app = self.keep(self.ax.AXUIElementCreateApplication(pid))
        self.check_pid(self.app)

    def keep(self, ref):
        if ref:
            self.refs.append(ref)
        return ref

    def close(self):
        for ref in reversed(self.refs):
            self.cf.CFRelease(ref)
        self.refs.clear()

    def string(self, text):
        return self.keep(self.cf.CFStringCreateWithCString(None, text.encode(), 0x08000100))

    def attr(self, node, name):
        out = C.c_void_p()
        if self.ax.AXUIElementCopyAttributeValue(node, self.string(name), C.byref(out)):
            return None
        return self.keep(out.value)

    def text(self, node, name="AXTitle"):
        ref = self.attr(node, name)
        if not ref:
            return ""
        kind = self.cf.CFGetTypeID(ref)
        if kind == self.cf.CFURLGetTypeID():
            ref = self.cf.CFURLGetString(ref)
        elif kind != self.cf.CFStringGetTypeID():
            return ""
        buf = C.create_string_buffer(16384)
        if not self.cf.CFStringGetCString(ref, buf, len(buf), 0x08000100):
            return ""
        return buf.value.decode(errors="replace")

    def array(self, node, name="AXChildren"):
        ref = self.attr(node, name)
        if not ref:
            return []
        return [self.cf.CFArrayGetValueAtIndex(ref, i)
                for i in range(self.cf.CFArrayGetCount(ref))]

    def geometry(self, node, name, kind):
        ref = self.attr(node, name)
        value = Point()
        if not ref or not self.ax.AXValueGetValue(ref, kind, C.byref(value)):
            raise RuntimeError("无法读取浏览器界面元素的位置")
        return value.x, value.y

    def check_pid(self, node):
        found = C.c_int()
        if self.ax.AXUIElementGetPid(node, C.byref(found)) or found.value != self.pid:
            raise RuntimeError("浏览器进程不匹配，已停止界面操作")

    def walk(self, root, web_contents=False):
        pending = [(root, 0)]
        count = 0
        while pending:
            node, depth = pending.pop()
            count += 1
            if count > 2000 or depth > 30:
                raise RuntimeError("浏览器框架层级异常，已停止遍历")
            yield node
            if not web_contents and self.text(node, "AXRole") == "AXWebArea":
                continue
            pending.extend((child, depth + 1) for child in reversed(self.array(node)))

    def windows(self):
        return self.array(self.app, "AXWindows")

    def nodes(self):
        for window in self.windows():
            yield from self.walk(window)

    def click(self, node):
        self.check_pid(node)
        if self.ax.AXUIElementPerformAction(node, self.string("AXPress")) == 0:
            return
        # Some browser-owned picker rows expose only static text. Their native
        # bounds are still PID-verified; never click a trading-page coordinate.
        x, y = self.geometry(node, "AXPosition", 1)
        w, h = self.geometry(node, "AXSize", 2)
        if w <= 0 or h <= 0:
            raise RuntimeError("浏览器控件不可见")
        for event_type in (1, 2):
            event = self.ax.CGEventCreateMouseEvent(None, event_type, Point(x + w / 2, y + h / 2), 0)
            if not event:
                raise RuntimeError("无法点击浏览器控件")
            self.ax.CGEventPost(0, event)
            self.cf.CFRelease(event)

    def prepare(self):
        self.check_pid(self.app)
        true_ref = C.c_void_p.in_dll(self.cf, "kCFBooleanTrue").value
        self.ax.AXUIElementSetAttributeValue(self.app, self.string("AXEnhancedUserInterface"), true_ref)
        self.ax.AXUIElementSetAttributeValue(self.app, self.string("AXFrontmost"), true_ref)

    def positions(self, console_url, chart_url):
        best = {}
        for window in self.windows():
            positions = {}
            for node in self.walk(window):
                if self.text(node, "AXRole") != "AXWebArea":
                    continue
                url = self.text(node, "AXURL")
                key = "console" if url.startswith(console_url) else "chart" if url.startswith(chart_url) else None
                if key and self.geometry(node, "AXSize", 2)[0] > 100:
                    positions[key] = self.geometry(node, "AXPosition", 1)
            if len(positions) == 2:
                return positions
            if "console" in positions:
                best = positions
        return best

    def menu_command(self, roots, titles):
        for root in roots:
            for node in self.walk(root):
                if self.text(node, "AXRole") in {"AXMenuBarItem", "AXMenuItem"} and self.text(node) in titles:
                    self.click(node)
                    return
        raise RuntimeError("找不到 Chrome 菜单命令：" + " / ".join(titles))

    def adjust(self, titles):
        button = next((n for n in self.nodes() if self.text(n, "AXRole") == "AXPopUpButton"
                       and any(t in (self.text(n) + self.text(n, "AXDescription"))
                               for t in ("调整拆分视图", "Adjust split view"))), None)
        if not button:
            raise RuntimeError("找不到 Chrome 分屏布局按钮")
        self.click(button)
        self.menu_command(self.windows(), titles)

    def split(self, console_url, chart_url, chart_title, chart_host):
        self.prepare()
        positions = self.positions(console_url, chart_url)
        if len(positions) == 2:
            if positions["console"][0] == positions["chart"][0]:
                self.adjust(("并排显示", "Side by side", "Side-by-side"))
                positions = self.positions(console_url, chart_url)
            if positions["console"][0] > positions["chart"][0]:
                self.adjust(("反转视图", "Reverse views", "Swap views"))
        else:
            def picker():
                return next((n for n in self.nodes() if self.text(n, "AXRole") == "AXWebArea"
                             and self.text(n, "AXURL").startswith("chrome://tab-search.top-chrome/")), None)

            if not picker():
                bar = self.attr(self.app, "AXMenuBar")
                self.menu_command([bar], ("标签页", "Tab"))
                self.menu_command([bar], ("在拆分视图中显示标签页", "Show Tab in Split View"))
            deadline = time.monotonic() + 6
            while time.monotonic() < deadline:
                area = picker()
                if area:
                    rows = [n for n in self.walk(area, web_contents=True)
                            if self.text(n, "AXRole") == "AXStaticText"]
                    exact = [n for n in rows if self.text(n, "AXValue").startswith(chart_title)]
                    candidates = exact or [n for n in rows if chart_host in self.text(n, "AXValue")]
                    if len(candidates) == 1:
                        self.click(candidates[0])
                        break
                    if len(candidates) > 1:
                        raise RuntimeError("图表候选不唯一，已停止自动选择")
                time.sleep(0.15)
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            positions = self.positions(console_url, chart_url)
            if len(positions) == 2 and positions["console"][0] < positions["chart"][0]:
                return
            time.sleep(0.15)
        raise RuntimeError("未确认左侧控制台、右侧 TradingView；请检查两个页面是否位于同一窗口")


if __name__ == "__main__":
    driver = None
    try:
        driver = NativeUI(int(sys.argv[1]))
        driver.split(*sys.argv[2:6])
        print("split-ready")
    except Exception as exc:
        print(str(exc) or type(exc).__name__, file=sys.stderr)
        sys.exit(1)
    finally:
        if driver:
            driver.close()
