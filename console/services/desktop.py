"""desktop application functions and owned state. No console.app dependency."""

from fastapi import HTTPException
from typing import Optional
import asyncio
import config
import json
import time
from pathlib import Path
from contextlib import asynccontextmanager
from console.shared import logger
from console.schemas import TvShowRequest, TvViewRequest


class DesktopService:
    def __init__(self, context):
        self.context = context
        self._tv_bindings_path = context.paths.data_dir / 'tv_chart_bindings.json'
        self._tv_bindings = {}
        self._tv_binding_error = ''
        try:
            if self._tv_bindings_path.exists():
                saved=json.loads(self._tv_bindings_path.read_text())
                if saved.get('version')!=1 or not isinstance(saved.get('bindings'),dict):
                    raise ValueError('图表绑定配置无效')
                from controller.window_chart import _chart_address
                for source,binding in saved['bindings'].items():
                    if (not isinstance(source,str) or not isinstance(binding,dict)
                            or not isinstance(binding.get('chart_id'),str)
                            or not isinstance(binding.get('address'),str)
                            or _chart_address(binding.get('address',''))!=binding.get('address')):
                        raise ValueError('图表绑定配置无效')
                self._tv_bindings=saved['bindings']
        except (OSError,ValueError,TypeError) as error:
            self._tv_binding_error=str(error)

    @asynccontextmanager
    async def _chart_scope(self, origin, token, *, layout=None):
        from controller.window_chart import same_window_chart, layout_chart
        if layout is not None:
            address = self._TV_FOCUS_CHART_URL if layout == 'focus' else self._TV_CHART_URL
            async with layout_chart(config.CDP_HTTP_ENDPOINT, origin, token, address) as selected:
                yield selected
            return
        if self._tv_binding_error:
            raise RuntimeError(self._tv_binding_error+'，请重新绑定图表')
        options={'bindings':self._tv_bindings} if self._tv_bindings else {}
        async with same_window_chart(config.CDP_HTTP_ENDPOINT,origin,token,**options) as selected:
            yield selected

    async def api_tv_charts(self, trader=None):
        if trader!='master':
            raise HTTPException(403,'请使用本机扩展绑定自己的图表')
        from controller.window_chart import _pages,_chart_address
        pages=await asyncio.to_thread(_pages,config.CDP_HTTP_ENDPOINT)
        return {'charts':[{'id':p['id'],'title':p.get('title') or _chart_address(p.get('url','')),
                           'address':_chart_address(p.get('url',''))}
                          for p in pages if _chart_address(p.get('url',''))]}

    async def api_tv_bind(self, req, trader=None, *, origin):
        if trader!='master':
            raise HTTPException(403,'请使用本机扩展绑定自己的图表')
        from controller.window_chart import bind_console_chart
        try:
            async with asyncio.timeout(10):
                binding=await bind_console_chart(config.CDP_HTTP_ENDPOINT,origin,req.source_token,req.target_id)
            updated={**self._tv_bindings,binding['source_id']:{'chart_id':binding['chart_id'],'address':binding['address']}}
            self._save_tv_bindings(updated)
            return {'ok':True,'binding':binding}
        except Exception as error:
            return {'ok':False,'reason':str(error) or type(error).__name__}

    def _save_tv_bindings(self, bindings):
        self._tv_bindings_path.parent.mkdir(parents=True, exist_ok=True)
        pending = self._tv_bindings_path.with_suffix('.tmp')
        pending.write_text(json.dumps({'version': 1, 'bindings': bindings}, ensure_ascii=False))
        pending.replace(self._tv_bindings_path)
        self._tv_bindings = bindings
        self._tv_binding_error = ''

    async def api_tv_entry_markers(self, req, trader=None, *, origin):
        from console.services.chart_entries import entry_markers
        if trader != "master":
            raise HTTPException(403, "请使用本机Chrome扩展显示自己的图表标记")
        entries = entry_markers(self.context.order_mgr.list_lots(), now=time.time())
        source = (Path(__file__).resolve().parents[2] / "extensions/autotrade-tv/chart-grid.js").read_text()
        results = []
        for layout in ('candidates', 'focus'):
            try:
                async with asyncio.timeout(10):
                    async with self._chart_scope(origin, req.source_token, layout=layout) as (tv, address):
                        script = "(async()=>{" + source + "\nreturn await markTradingViewEntries(" + json.dumps(entries) + "," + json.dumps(address) + ");})()"
                        result = await asyncio.wait_for(tv.evaluate(script), timeout=8)
                        results.append({'layout': layout, **result})
            except Exception as error:
                results.append({'layout': layout, 'ok': False, 'reason': str(error) or 'TV仓位线段响应超时'})
        return {'ok': all(r.get('ok') for r in results), 'marked': sum(r.get('marked', 0) for r in results),
                'layouts': results, 'reason': '；'.join(r.get('reason') or '仓位线段未全部确认' for r in results if not r.get('ok'))}

    async def api_tv_grid(self, req, trader=None, *, origin):
        """Mirror ranks only in the originating console's resolved chart."""
        import re
        if trader != "master":
            raise HTTPException(403, "请使用本机 Chrome 扩展控制自己的 TradingView 图表")
        symbols = [s.strip().upper() for s in req.symbols]
        if any(not re.fullmatch(r"[A-Z0-9_.-]{1,24}:[A-Z0-9_.!+/-]{1,32}", s) for s in symbols):
            raise HTTPException(400, "宫格标的代码无效")
        count = 6 if req.layout == 'focus' else 8
        label = 'auto_trade2 六宫格' if req.layout == 'focus' else 'auto_trade 八宫格'
        source = (Path(__file__).resolve().parents[2] / "extensions/autotrade-tv/chart-grid.js").read_text()
        try:
            async with asyncio.timeout(10):
                async with self._chart_scope(origin, req.source_token, layout=req.layout) as (tv, address):
                    for attempt in range(16):
                        script = "(()=>{" + source + "\nreturn inspectTradingViewGrid(" + json.dumps(symbols) + "," + json.dumps(address) + "," + ("true" if attempt == 0 else "false") + ",null," + json.dumps(req.layout) + ");})()"
                        result = await asyncio.wait_for(tv.evaluate(script), timeout=3)
                        if not isinstance(result, dict):
                            return {"ok": False, "reason": label+"未返回可核对的状态"}
                        if not result.get("pending"):
                            if result.get('ok') and (len(result.get('panes', [])) != count or not all(p.get('ok') for p in result['panes'])):
                                return {'ok': False, 'reason': label+'未逐格确认'}
                            return {**result, 'layout': req.layout}
                        await asyncio.sleep(.2)
                    return {**result, "ok": False, "reason": label+"未全部确认切换，请检查标的同步分组后重试"}
        except TimeoutError:
            return {"ok": False, "reason": "TV图表响应超时，请切到TV确认后点同步重试"}
        except Exception as error:
            return {"ok": False, "reason": str(error)}

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
                    "指标数据异常",
                    f"启动检查发现指标计算故障（60 秒内错误 {health['study_errors_60s']} 条，ts_c 就绪 {health['ts_ready']}/{health['ts_total']}），请检查行情采集日志",
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
        """Prepare three independent windows without reloading existing pages."""
        if not await self._cdp_alive():
            logger.info('启动交易工作区浏览器（auto_trade 八格 / auto_trade2 六格 / 控制台）...')
            proc = await asyncio.create_subprocess_exec(
                'open', '-na', 'Google Chrome', '--args',
                '--remote-debugging-port=9222',
                f'--user-data-dir={self._TV_CHROME_PROFILE}',
                '--no-first-run', '--new-window', self._TV_CHART_URL,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            )
            await proc.wait()
            for _ in range(15):
                await asyncio.sleep(2)
                if await self._cdp_alive():
                    break
            else:
                return '调试 Chrome 的 9222 端口 30s 未就绪'
        try:
            from controller.browser_workspace import ensure_independent_windows
            if not self._CONSOLE_AUTO_ARRANGE_WINDOWS:
                return None
            async with asyncio.timeout(20):
                targets = await ensure_independent_windows(config.CDP_HTTP_ENDPOINT,
                    [self._TV_CHART_URL, self._TV_FOCUS_CHART_URL, self._CONSOLE_URL])
            # Chrome can restore new target IDs after restart. Bind only the
            # exact dedicated console and known eight-pane layout.
            source = targets[self._CONSOLE_URL]
            updated = {source: {'chart_id': targets[self._TV_CHART_URL], 'address': self._TV_CHART_URL}}
            self._save_tv_bindings(updated)
            self.context.lifecycle._startup_state['split_ready'] = True
            self.context.lifecycle._startup_state['split_error'] = ''
        except asyncio.CancelledError:
            raise
        except Exception as error:
            # Window arrangement is display-only; never hold trading readiness.
            reason = str(error).strip() or type(error).__name__
            self.context.lifecycle._startup_state['split_ready'] = False
            self.context.lifecycle._startup_state['split_error'] = reason
            logger.warning('独立图表工作区未完成: %s', reason)
        try:
            await self._activate_console_tab()
        except Exception as error:
            logger.info('控制台标签页置前失败: %s', error)
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
        """打开工作区并等待必需服务首次就绪。"""
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
            if self.context.lifecycle._startup_status()["workspace_ready"]:
                self.context.lifecycle._startup_state["ready"] = True
                logger.info("控制台工作区就绪，加载页自动进入主页面")
                return
            await asyncio.sleep(1)

    async def _tv_show_symbol(self, symbol: str, *, origin=None, source_token=None) -> dict:
        """Switch only visual cell 6, using the same geometry as full-grid sync."""
        import re
        from controller.tv_cdp import TradingViewCDPController
        from controller.window_chart import same_window_chart

        symbol = symbol.strip().upper()
        if not re.fullmatch(r"(?:[A-Z0-9_.-]{1,24}:)?[A-Z0-9_.!+/-]{1,32}", symbol):
            return {"ok": False, "reason": "标的代码无效"}
        if source_token is None:
            not_ready = await self.context.desktop._tv_ensure_browser()
            if not_ready:
                logger.warning("TV 不可用: %s", not_ready)
                return {"ok": False, "reason": not_ready}
        source = (Path(__file__).resolve().parents[2] / "extensions/autotrade-tv/chart-grid.js").read_text()

        async def switch(tv, address):
            expected = None
            for attempt in range(16):
                script = "(()=>{" + source + "\nreturn inspectTradingViewGrid(" + ",".join((
                    json.dumps(symbol), json.dumps(address),
                    "true" if attempt == 0 else "false", json.dumps(expected),
                )) + ");})()"
                result = await asyncio.wait_for(tv.evaluate(script), timeout=3)
                if not isinstance(result, dict):
                    return {"ok": False, "reason": "TradingView 未返回可核对的第六格状态"}
                if not result.get("pending"):
                    return result
                if expected is None:
                    expected = [pane["symbol"] for pane in result["panes"]]
                await asyncio.sleep(.2)
            return {**result, "ok": False, "pending": False,
                    "reason": "TradingView 第六格未确认切换到目标标的"}

        try:
            async with asyncio.timeout(10):
                if source_token is None:
                    from controller.window_chart import _pages, _chart_address
                    pages = await asyncio.to_thread(_pages, config.CDP_HTTP_ENDPOINT)
                    candidates = [p for p in pages if _chart_address(p.get('url', '')) == self._TV_CHART_URL]
                    if len(candidates) != 1:
                        return {'ok': False, 'reason': '请只打开一个 auto_trade 八宫格窗口'}
                    target_id = candidates[0]['id']
                    async with TradingViewCDPController(config.CDP_HTTP_ENDPOINT, target_id=target_id) as tv:
                        address = await tv.evaluate("location.origin + location.pathname")
                        if address != self._TV_CHART_URL:
                            return {'ok': False, 'reason': '八宫格地址已变化，未切换标的'}
                        report = await switch(tv, address)
                else:
                    async with self._chart_scope(origin, source_token) as (tv, address):
                        report = await switch(tv, address)
        except TimeoutError:
            return {"ok": False, "reason": "TV 第六格响应超时，请重试"}
        except Exception as error:
            logger.info("TV 第六格切换失败: %s", error)
            return {"ok": False, "reason": str(error)}
        if report.get("ok"):
            logger.info("TV 第六格已切到 %s，读回 %s", symbol, report.get("shown"))
            if source_token is None:
                from controller.browser_workspace import activate_page
                await asyncio.to_thread(activate_page, config.CDP_HTTP_ENDPOINT, target_id)
        return report

    async def api_tv_show(self, req: TvShowRequest, trader: str = None) -> dict:
        """Control only the host's chart, for its local console."""
        if trader != "master":
            raise HTTPException(403, "请使用本机 Chrome 扩展控制自己的 TradingView 图表")
        symbol = req.symbol.strip()
        if not symbol:
            raise HTTPException(400, "symbol 不能为空")
        return await self.context.desktop._tv_show_symbol(symbol)

    async def api_tv_view(self, req: TvViewRequest, trader: str = None, *, origin: str) -> dict:
        if trader != "master":
            raise HTTPException(403, "请使用本机 Chrome 扩展控制自己的 TradingView 图表")
        symbol = req.symbol.strip()
        if not symbol or not req.source_token:
            raise HTTPException(400, "缺少标的或当前标签页标识，请刷新 AutoTrade 后重试")
        result = await self.context.desktop._tv_show_symbol(symbol, origin=origin, source_token=req.source_token)
        return {**result, "master": True}
