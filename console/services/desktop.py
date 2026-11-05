"""desktop application functions and owned state. No console.app dependency."""

from fastapi import HTTPException
from typing import Optional
import asyncio
import base64
import config
import json
import os
import time
from console.shared import logger
from console.schemas import TvShowRequest


class DesktopService:
    def __init__(self, context):
        self.context = context

    async def _tv_startup_check(self) -> None:
        """启动首要任务：TV 数据源就绪检查，正常/异常都给出提示确认（一次性）。

        就绪 = 快照标的数达到股票池且全部 ts_c 有 trend_state；
        异常 = study 通道系统性故障（collector.study_health().systemic，
        覆盖 2026-10-27 的 pine 协议故障模式）或 240s 超时未就绪。
        正常 → info（前端横幅 + 事件，不打扰手机）；异常 → critical（含 iMessage）。
        """

        def _ready_counts() -> tuple:
            snap = self.context.collector.latest() or {}
            total = len(snap)
            ready = sum(
                (
                    1
                    for v in snap.values()
                    if isinstance(v.get("ts_c"), dict)
                    and v["ts_c"].get("trend_state") is not None
                )
            )
            return (total, ready)

        t0 = time.time()
        deadline = t0 + 240.0
        expected = len(config.WATCHLIST)
        while time.time() < deadline:
            health = self.context.collector.study_health()
            if health.get("systemic"):
                await self.context.notifier.notify(
                    "tv_startup",
                    "critical",
                    "TV 数据源异常",
                    f"启动检查发现 study 通道系统性故障（60s 内 study 报错 {health['study_errors_60s']} 条，ts_c 就绪 {health['ts_ready']}/{health['ts_total']}），请检查 daemon 日志",
                )
                return
            total, ready = _ready_counts()
            if total >= expected and ready >= total:
                await self.context.notifier.notify(
                    "tv_startup",
                    "info",
                    "TV 数据源正常",
                    f"{ready}/{expected} 标的数据就绪，启动耗时 {time.time() - t0:.0f}s",
                )
                return
            await asyncio.sleep(5.0)
        total, ready = _ready_counts()
        await self.context.notifier.notify(
            "tv_startup",
            "critical",
            "TV 数据源异常",
            f"启动 240s 后数据仍未就绪：ts_c {ready}/{total}（股票池 {expected}，daemon 运行中={self.context.collector.running}），请检查 daemon/网络",
        )

    async def _tv_activate_window(self) -> None:
        """把 TV 窗口拉到前台（用户点选选股后希望立刻看到图表）。
        两步 best-effort：① CDP /json/activate 把图表标签页提到浏览器内部最前；
        ② 找到 9222 监听进程所属 .app，open -a 把整个应用窗口置前。
        全部失败静默（不影响切标的本身）。"""
        try:
            from controller import tv_cdp as _cdp_mod

            endpoint = _cdp_mod.config.CDP_HTTP_ENDPOINT

            def _activate() -> None:
                import urllib.request

                with urllib.request.urlopen(f"{endpoint}/json", timeout=3) as r:
                    targets = json.loads(r.read())
                page = next(
                    (
                        t
                        for t in targets
                        if t.get("type") == "page"
                        and "tradingview.com/chart" in t.get("url", "")
                    ),
                    None,
                )
                if page:
                    with urllib.request.urlopen(
                        f"{endpoint}/json/activate/{page['id']}", timeout=3
                    ) as r:
                        r.read()

            await asyncio.to_thread(_activate)
            proc = await asyncio.create_subprocess_exec(
                "lsof",
                "-nP",
                "-iTCP:9222",
                "-sTCP:LISTEN",
                "-t",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            out, _ = await proc.communicate()
            pids = out.decode().split()
            if pids:
                try:
                    await self.context.desktop._osascript(
                        f'tell application "System Events" to set frontmost of (first process whose unix id is {int(pids[0])}) to true'
                    )
                    return
                except PermissionError:
                    await self.context.notifier.notify(
                        "a11y_permission",
                        "warning",
                        "TV 窗口置前需要辅助功能权限",
                        "系统设置 → 隐私与安全性 → 辅助功能，授权运行控制台的终端 App",
                    )
                except Exception as e:
                    logger.info("pid 精确置前失败，降级 open -a: %s", e)
            proc = await asyncio.create_subprocess_exec(
                "bash",
                "-c",
                "ps -o comm= -p $(lsof -nP -iTCP:9222 -sTCP:LISTEN -t | head -1) 2>/dev/null",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            out, _ = await proc.communicate()
            comm = out.decode().strip()
            if ".app/" in comm:
                self.context.app = comm.split(".app/")[0] + ".app"
                await asyncio.create_subprocess_exec(
                    "open",
                    "-a",
                    self.context.app,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
        except Exception as e:
            logger.info("TV 窗口置前失败（静默）: %s", e)

    async def _osascript(self, script: str, *args: str) -> str:
        proc = await asyncio.create_subprocess_exec(
            "osascript",
            "-e",
            script,
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await proc.communicate()
        if proc.returncode != 0:
            msg = err.decode(errors="replace").strip()
            if "-1719" in msg or "辅助功能" in msg or "assistive" in msg.lower():
                raise PermissionError(msg)
            raise RuntimeError(msg)
        return out.decode().strip()

    async def _cdp_alive(self) -> bool:
        """9222 调试端口是否有可控浏览器（快速探测）。"""
        from controller import tv_cdp as _cdp_mod

        endpoint = _cdp_mod.config.CDP_HTTP_ENDPOINT

        def _probe() -> bool:
            import urllib.request

            try:
                with urllib.request.urlopen(f"{endpoint}/json/version", timeout=2) as r:
                    return r.status == 200
            except Exception:
                return False

        return await asyncio.to_thread(_probe)

    async def _tv_ensure_browser(self) -> Optional[str]:
        """确保可控浏览器存在。返回 None = 就绪；否则返回失败原因。
        三种情况：① 9222 不通 → 启动 Chrome（带调试端口 + TV 图表页）；
        ② 端口活但图表页被关了（macOS 关窗不退进程，target 列表为空）
        → /json/new 新开一个图表标签页；③ 图表页在 → 复用。最后都等 widget 就绪。"""
        from controller import tv_cdp as _cdp_mod
        from controller.tv_cdp import TradingViewCDPController

        endpoint = _cdp_mod.config.CDP_HTTP_ENDPOINT

        def _chart_page_up() -> bool:
            import urllib.request

            try:
                with urllib.request.urlopen(f"{endpoint}/json", timeout=2) as r:
                    return any(
                        (
                            t.get("type") == "page"
                            and "tradingview.com/chart" in t.get("url", "")
                            for t in json.loads(r.read())
                        )
                    )
            except Exception:
                return False

        def _open_chart_tab() -> None:
            import urllib.request

            req = urllib.request.Request(
                f"{endpoint}/json/new?{self.context.desktop._TV_CHART_URL}",
                method="PUT",
            )
            with urllib.request.urlopen(req, timeout=5) as r:
                r.read()

        if await self.context.desktop._cdp_alive():
            if not await asyncio.to_thread(_chart_page_up):
                logger.info("CDP 在线但无图表页，新开 TradingView 标签页 ...")
                try:
                    await asyncio.to_thread(_open_chart_tab)
                except Exception as e:
                    return f"新开图表标签页失败: {e}"
        else:
            logger.info("CDP 离线，启动浏览器打开 TradingView ...")
            await asyncio.create_subprocess_exec(
                "open",
                "-na",
                "Google Chrome",
                "--args",
                "--remote-debugging-port=9222",
                f"--user-data-dir={self.context.desktop._TV_CHROME_PROFILE}",
                "--no-first-run",
                self.context.desktop._TV_CHART_URL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            for _ in range(15):
                await asyncio.sleep(2)
                if await self.context.desktop._cdp_alive():
                    break
            else:
                return f"调试 Chrome 未以可控模式启动（专用 profile {self.context.desktop._TV_CHROME_PROFILE} 下 9222 端口 30s 未就绪；该 profile 与日常 Chrome 独立，无需退出日常 Chrome——请确认 profile 目录存在后再点一次）"
        for _ in range(15):
            if await asyncio.to_thread(_chart_page_up):
                break
            await asyncio.sleep(2)
        else:
            return "浏览器已启动但图表页未加载出来"
        ready_js = "!!(((window.TradingViewApi||{})._chartWidgetCollection||{getAll:()=>[]}).getAll().length|| (((window._exposed_chartWidgetCollection||{})._chartWidgetsDefs)||[]).length)"
        try:
            for _ in range(30):
                async with TradingViewCDPController() as tv:
                    if await asyncio.wait_for(tv.evaluate(ready_js), timeout=5):
                        logger.info("浏览器已就绪（TV 图表页 + widget 加载完成）")
                        return None
                await asyncio.sleep(2)
            return "图表页已打开但 widget 未就绪（页面可能未登录或加载过慢）"
        except Exception as e:
            return f"浏览器就绪检查异常: {e}"

    def _cdp_page_id(self, url_prefix: str) -> Optional[str]:
        from controller import tv_cdp as _cdp_mod
        import urllib.request

        endpoint = _cdp_mod.config.CDP_HTTP_ENDPOINT
        try:
            with urllib.request.urlopen(f"{endpoint}/json", timeout=2) as r:
                targets = json.loads(r.read())
            page = next(
                (
                    t
                    for t in targets
                    if t.get("type") == "page"
                    and t.get("url", "").startswith(url_prefix)
                ),
                None,
            )
            return page.get("id") if page else None
        except Exception:
            return None

    async def _cdp_open_tab(self, url: str) -> None:
        from controller import tv_cdp as _cdp_mod
        import urllib.request

        endpoint = _cdp_mod.config.CDP_HTTP_ENDPOINT

        def _open() -> None:
            req = urllib.request.Request(f"{endpoint}/json/new?{url}", method="PUT")
            with urllib.request.urlopen(req, timeout=5) as r:
                r.read()

        await asyncio.to_thread(_open)

    async def _activate_console_tab(self) -> None:
        page_id = await asyncio.to_thread(
            self.context.desktop._cdp_page_id, self.context.desktop._CONSOLE_URL
        )
        if not page_id:
            return
        from controller import tv_cdp as _cdp_mod
        import urllib.request

        endpoint = _cdp_mod.config.CDP_HTTP_ENDPOINT

        def _activate() -> None:
            with urllib.request.urlopen(
                f"{endpoint}/json/activate/{page_id}", timeout=3
            ) as r:
                r.read()

        await asyncio.to_thread(_activate)

    async def _workspace_ensure_browser(self) -> Optional[str]:
        """准备专用 Chrome，并组合为左控制台、右 TV 的原生分屏。"""
        if not await self.context.desktop._cdp_alive():
            logger.info("启动交易工作区浏览器（控制台 + TradingView）...")
            proc = await asyncio.create_subprocess_exec(
                "open",
                "-na",
                "Google Chrome",
                "--args",
                "--remote-debugging-port=9222",
                f"--user-data-dir={self.context.desktop._TV_CHROME_PROFILE}",
                "--no-first-run",
                "--new-window",
                self.context.desktop._CONSOLE_URL,
                self.context.desktop._TV_CHART_URL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await proc.wait()
            for _ in range(15):
                await asyncio.sleep(2)
                if await self.context.desktop._cdp_alive():
                    break
            else:
                return "调试 Chrome 的 9222 端口 30s 未就绪"
        elif not await asyncio.to_thread(
            self.context.desktop._cdp_page_id, self.context.desktop._CONSOLE_URL
        ):
            await self.context.desktop._cdp_open_tab(self.context.desktop._CONSOLE_URL)
        not_ready = await self.context.desktop._tv_ensure_browser()
        if not_ready:
            return not_ready
        if not await asyncio.to_thread(
            self.context.desktop._cdp_page_id, self.context.desktop._CONSOLE_URL
        ):
            await self.context.desktop._cdp_open_tab(self.context.desktop._CONSOLE_URL)
        try:
            await self.context.desktop._activate_console_tab()
        except Exception as e:
            logger.info("控制台标签页置前失败（静默）: %s", e)
        if self.context.desktop._CONSOLE_AUTO_SPLIT_BROWSER:
            try:
                await self.context.desktop._workspace_arrange_split()
                self.context.lifecycle._startup_state["split_ready"] = True
                self.context.lifecycle._startup_state["split_error"] = ""
            except asyncio.CancelledError:
                raise
            except Exception as e:
                # Do not retry native menu actions forever or hold the trading
                # readiness gate closed for a desktop permission problem.
                self.context.lifecycle._startup_state["split_ready"] = False
                reason = str(e).strip() or f"自动分屏失败（{type(e).__name__}）"
                self.context.lifecycle._startup_state["split_error"] = reason
                logger.warning("浏览器已打开，自动分屏未完成: %s", reason)
        return None

    async def _workspace_arrange_split(self) -> None:
        from controller import tv_cdp
        from controller.browser_workspace import arrange_split

        await arrange_split(tv_cdp.config.CDP_HTTP_ENDPOINT, self._CONSOLE_URL)

    async def _console_http_ready(self) -> bool:
        """Wait for an actual HTTP response, not full trading readiness.

        Lifespan background tasks start before Uvicorn binds its socket. Full
        readiness includes the browser, so waiting for `ready` would deadlock.
        """
        from urllib.parse import urljoin
        from urllib.request import ProxyHandler, build_opener

        def probe():
            try:
                opener = build_opener(ProxyHandler({}))
                url = urljoin(self._CONSOLE_URL, "api/startup")
                with opener.open(url, timeout=2) as response:
                    payload = json.load(response)
                    return response.status == 200 and isinstance(payload, dict) and isinstance(payload.get("stages"), list)
            except Exception:
                return False

        return await asyncio.to_thread(probe)

    async def _startup_prepare(self) -> None:
        """打开工作区并等待全链路首次就绪；就绪锁定后不再因断线退回加载页。"""
        await asyncio.sleep(0.5)
        if self.context.desktop._CONSOLE_AUTO_OPEN_BROWSER:
            while not await self.context.desktop._console_http_ready():
                self.context.lifecycle._startup_state["browser_error"] = "等待控制台 HTTP 服务可访问后打开浏览器"
                await asyncio.sleep(0.5)
            self.context.lifecycle._startup_state["browser_error"] = ""
            while not self.context.lifecycle._startup_state["browser_ready"]:
                try:
                    error = await self.context.desktop._workspace_ensure_browser()
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.exception("启动浏览器工作区异常")
                    error = str(e)
                self.context.lifecycle._startup_state["browser_error"] = error or ""
                if not error:
                    self.context.lifecycle._startup_state["browser_ready"] = True
                    break
                await asyncio.sleep(5)
        else:
            self.context.lifecycle._startup_state["browser_ready"] = True
        while not self.context.lifecycle._startup_state["ready"]:
            if self.context.lifecycle._startup_status()["all_ready"]:
                self.context.lifecycle._startup_state["ready"] = True
                logger.info("控制台全链路就绪，加载页自动进入主页面")
                return
            await asyncio.sleep(1)

    async def _tv_show_symbol(self, symbol: str) -> dict:
        """切第一个 pane 到该标的 + 读回校验 + 窗口置前。
        前置：CDP 不在线先启动浏览器并定位到 TradingView（_tv_ensure_browser）。"""
        from controller.tv_cdp import TradingViewCDPController

        not_ready = await self.context.desktop._tv_ensure_browser()
        if not_ready:
            logger.warning("TV 不可用: %s", not_ready)
            return {"ok": False, "reason": not_ready}
        report = {}
        for attempt in range(3):
            try:
                async with TradingViewCDPController() as tv:
                    report = json.loads(
                        await asyncio.wait_for(
                            tv.evaluate(
                                self.context.desktop._TV_SHOW_JS.replace(
                                    "%SYMBOL%", json.dumps(symbol)
                                )
                            ),
                            timeout=10,
                        )
                    )
            except Exception as e:
                logger.info("TV 切标的不可用（静默降级）: %s", e)
                return {"ok": False, "reason": str(e)}
            if report.get("fatal") or report.get("ok"):
                break
            if attempt < 2:
                await asyncio.sleep(2)
        if report.get("fatal"):
            logger.info("TV 切标的失败: %s", report["fatal"])
            return {"ok": False, "reason": report["fatal"]}
        if report.get("ok"):
            logger.info(
                "TV 第一个 pane 已切到 %s（%s → %s）",
                symbol,
                report.get("before"),
                report.get("after"),
            )
            await self.context.desktop._tv_activate_window()
            return {"ok": True, "shown": report.get("after")}
        logger.warning(
            "TV 切标的读回不一致: 目标 %s 读回 %s", symbol, report.get("after")
        )
        return {
            "ok": False,
            "reason": f"读回不一致: {report.get('after')}",
            "shown": report.get("after"),
        }

    async def api_tv_show(self, req: TvShowRequest, trader: str = None) -> dict:
        """选股点选联动：本机 TV 桌面端视觉左上角第一个 pane 切到该标的。
        幂等快速（多客户端同时点各自切同一台 TV，单机单屏本质）；失败静默。"""
        symbol = req.symbol.strip()
        if not symbol:
            raise HTTPException(400, "symbol 不能为空")
        return await self.context.desktop._tv_show_symbol(symbol)

    def _tv_load_pane_map(self) -> dict:
        """trader → {pane, last_used} 映射；缺文件/损坏返回 {}。"""
        try:
            payload = json.loads(
                self.context.desktop._TV_PANES_PATH.read_text(encoding="utf-8")
            )
            if isinstance(payload, dict):
                return payload
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as e:
            logger.warning("tv_panes.json 加载失败: %s", e)
        return {}

    def _tv_save_pane_map(self, m: dict) -> None:
        try:
            self.context.desktop._TV_PANES_PATH.parent.mkdir(
                parents=True, exist_ok=True
            )
            tmp = self.context.desktop._TV_PANES_PATH.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps(m, ensure_ascii=False, indent=1), encoding="utf-8"
            )
            os.replace(tmp, self.context.desktop._TV_PANES_PATH)
        except OSError:
            logger.exception("tv_panes.json 落盘失败")

    def _tv_assign_pane(self, trader: str, pane_count: int) -> Optional[int]:
        """给客户端分配画面位：已有映射且仍有效 → 复用；否则取最小空位
        （pane 0 是 master 的）；全满时先回收两小时未用的旧映射，仍无位 → None。"""
        now = time.time()
        m = self.context.desktop._tv_load_pane_map()
        row = m.get(trader)
        if isinstance(row, dict) and 0 < int(row.get("pane", 0)) < pane_count:
            row["last_used"] = now
            self.context.desktop._tv_save_pane_map(m)
            return int(row["pane"])
        m = {
            k: v
            for k, v in m.items()
            if isinstance(v, dict) and 0 < int(v.get("pane", 0)) < pane_count
        }
        used = {int(v["pane"]) for v in m.values()}
        for idx in range(1, pane_count):
            if idx not in used:
                m[trader] = {"pane": idx, "last_used": now}
                self.context.desktop._tv_save_pane_map(m)
                return idx
        stale = sorted(m.items(), key=lambda kv: kv[1].get("last_used", 0))
        if (
            stale
            and now - stale[0][1].get("last_used", 0)
            > self.context.desktop._TV_PANE_STALE_S
        ):
            old_trader, old = stale[0]
            idx = int(old["pane"])
            del m[old_trader]
            m[trader] = {"pane": idx, "last_used": now}
            self.context.desktop._tv_save_pane_map(m)
            logger.info("TV 画面位回收：%s 的 pane %d 转给 %s", old_trader, idx, trader)
            return idx
        self.context.desktop._tv_save_pane_map(m)
        return None

    async def _tv_list_panes(self) -> Optional[list]:
        """读当前可见 pane 列表 [{symbol, rect}]（视觉行优先）；失败返回 None。"""
        from controller.tv_cdp import TradingViewCDPController

        try:
            async with TradingViewCDPController() as tv:
                return json.loads(
                    await asyncio.wait_for(
                        tv.evaluate(self.context.desktop._TV_PANES_JS), timeout=10
                    )
                )
        except Exception as e:
            logger.info("TV pane 列表读取失败（静默）: %s", e)
            return None

    async def _tv_view_for_trader(self, symbol: str, trader: str) -> dict:
        """客户端看盘主流程：确保浏览器 → 分配画面位 → 切该 pane → 等数据就绪
        → 按 pane rect 裁剪截图回传。每 pane 一把锁防并发切换同一格。"""
        from controller.tv_cdp import TradingViewCDPController

        not_ready = await self.context.desktop._tv_ensure_browser()
        if not_ready:
            return {"ok": False, "reason": not_ready}
        panes = await self.context.desktop._tv_list_panes()
        if not panes:
            return {"ok": False, "reason": "TV 无可用画面（图表未加载或宫格为空）"}
        idx = self.context.desktop._tv_assign_pane(trader, len(panes))
        if idx is None:
            return {
                "ok": False,
                "code": "pane_full",
                "reason": f"画面位已满（TV 当前 {len(panes)} 格，master 占 1 格，客户端画面位已全部分完）",
            }
        lock = self.context.desktop._tv_pane_locks.setdefault(idx, asyncio.Lock())
        async with lock:
            report = {}
            try:
                async with TradingViewCDPController() as tv:
                    show_js = self.context.desktop._TV_PANE_SHOW_JS.replace(
                        "%PANE%", str(idx)
                    ).replace("%SYMBOL%", json.dumps(symbol))
                    for attempt in range(3):
                        report = json.loads(
                            await asyncio.wait_for(tv.evaluate(show_js), timeout=10)
                        )
                        if report.get("fatal") or report.get("ok"):
                            break
                        if attempt < 2:
                            await asyncio.sleep(2)
                    if report.get("fatal"):
                        return {"ok": False, "reason": report["fatal"]}
                    if not report.get("ok"):
                        return {
                            "ok": False,
                            "reason": f"读回不一致: {report.get('after')}",
                        }
                    if report.get("switched"):
                        ticker = symbol.split(":")[-1].upper()
                        ready_js = self.context.desktop._TV_PANE_READY_JS.replace(
                            "%PANE%", str(idx)
                        ).replace("%TICKER%", json.dumps(ticker))
                        for _ in range(12):
                            ok = await asyncio.wait_for(
                                tv.evaluate(ready_js), timeout=5
                            )
                            if str(ok) == "true":
                                break
                            await asyncio.sleep(1)
                        await asyncio.sleep(1.2)
                    rect = report["rect"]
                    shot = await asyncio.wait_for(
                        tv.screenshot(
                            quality=70,
                            clip={
                                "x": rect["x"],
                                "y": rect["y"],
                                "width": rect["w"],
                                "height": rect["h"],
                            },
                        ),
                        timeout=15,
                    )
            except Exception as e:
                logger.info("TV 客户端看盘失败（%s pane %d）: %s", trader, idx, e)
                return {"ok": False, "reason": str(e)}
        logger.info(
            "TV 看盘：%s → pane %d 切到 %s（截图 %d KB）",
            trader,
            idx,
            report.get("after"),
            len(shot) // 1024,
        )
        return {
            "ok": True,
            "pane": idx,
            "shown": report.get("after"),
            "image": base64.b64encode(shot).decode("ascii"),
        }

    async def api_tv_view(self, req: TvShowRequest, trader: str = None) -> dict:
        """看盘统一入口：master → 只切 pane 0 不截图（面前就是屏幕）；
        已审批客户端 → 专属 pane 切标的 + 截图回传。"""
        symbol = req.symbol.strip()
        if not symbol:
            raise HTTPException(400, "symbol 不能为空")
        if trader == "master":
            return {
                **await self.context.desktop._tv_show_symbol(symbol),
                "master": True,
            }
        return await self.context.desktop._tv_view_for_trader(symbol, trader)

    async def _tv_view_startup_check(self):
        """启动校验 TV 画面位（布局由人工保证，这里只盘点并在快照里展示）。"""

        async def _check():
            if not await self.context.desktop._cdp_alive():
                self.context.desktop._tv_view_info = {
                    "panes": 0,
                    "slots": 0,
                    "checked_at": time.time(),
                    "note": "CDP 离线",
                }
                return
            panes = await self.context.desktop._tv_list_panes()
            n = len(panes or [])
            self.context.desktop._tv_view_info = {
                "panes": n,
                "slots": max(0, n - 1),
                "checked_at": time.time(),
            }
            logger.info(
                "TV 画面位校验：%d 格（master 1 + 客户端位 %d）", n, max(0, n - 1)
            )

        self.context.tasks.start("check", lambda: _check())
