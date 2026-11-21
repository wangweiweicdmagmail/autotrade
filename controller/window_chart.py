"""Select an explicitly bound chart, otherwise the originating Chrome window."""

import asyncio
import json
from contextlib import AsyncExitStack, asynccontextmanager
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, build_opener

from controller.tv_cdp import TradingViewCDPController


def _pages(endpoint):
    opener = build_opener(ProxyHandler({}))
    with opener.open(f"{endpoint}/json/list", timeout=3) as response:
        return [page for page in json.load(response) if page.get("type") == "page" and page.get("id")]


def _chart_address(url):
    address = urlsplit(url)
    if (address.scheme == "https" and address.hostname in {"www.tradingview.com", "cn.tradingview.com"}
            and address.path.startswith("/chart/")):
        return f"{address.scheme}://{address.netloc}{address.path}"
    return None


@asynccontextmanager
async def _console_chart(endpoint, console_origin, source_token, *, bindings=None, requested_target=None, requested_address=None):
    if urlsplit(endpoint).hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise RuntimeError("同窗口联动需要本机专用 Chrome")
    pages = await asyncio.to_thread(_pages, endpoint)
    marker = (f"location.origin === {json.dumps(console_origin)} && "
              f"document.documentElement.dataset.autotradeTvViewToken === {json.dumps(source_token)}")
    async with AsyncExitStack() as stack:
        async def connect(page):
            return await stack.enter_async_context(TradingViewCDPController(endpoint, target_id=page["id"]))

        sources = []
        for page in pages:
            url = urlsplit(page.get("url", ""))
            if f"{url.scheme}://{url.netloc}" != console_origin or url.path != "/":
                continue
            source = await connect(page)
            if await source.evaluate(marker):
                sources.append((source,page["id"]))
        if len(sources) != 1:
            raise RuntimeError("未能定位当前 AutoTrade 标签页，请在专用 Chrome 窗口打开控制台")
        source, source_id = sources[0]
        source_window = await source.window_id()
        if source_window is None:
            raise RuntimeError("未能识别当前 AutoTrade 所在的 Chrome 窗口")

        bound = (bindings or {}).get(source_id) if requested_address is None else None
        target = requested_target or (bound or {}).get('chart_id')
        charts = []
        for page in pages:
            address = _chart_address(page.get("url", ""))
            if not address:
                continue
            if requested_address and address != requested_address:
                continue
            if target and page['id'] != target:
                continue
            if bound and not requested_target and bound.get('address') != address:
                raise RuntimeError('已绑定的TV图表地址已变化，请重新绑定')
            chart = await connect(page)
            if target or requested_address or await chart.window_id() == source_window:
                charts.append((chart, address, page['id']))
        if not charts:
            if requested_address:
                raise RuntimeError('指定TV布局未打开，请打开 '+requested_address)
            if target:
                raise RuntimeError('已绑定的TV图表已关闭或变化，请重新绑定')
            raise RuntimeError("请在当前 AutoTrade 的 Chrome 窗口打开 TradingView 图表")
        if len(charts) > 1:
            if requested_address:
                raise RuntimeError('指定TV布局打开了多个副本，请只保留一个独立窗口')
            charts = [(chart, address, identity) for chart, address, identity in charts
                      if await chart.evaluate("document.visibilityState === 'visible'")]
            if len(charts) != 1:
                raise RuntimeError("当前窗口有多个 TradingView 图表，请将目标图表与 AutoTrade 分屏")
        chart, address, chart_id = charts[0]
        if (not await source.evaluate(marker) or await source.window_id() != source_window
                or (not target and not requested_address and await chart.window_id() != source_window)):
            raise RuntimeError("标签页归属已变化，请重新点击标的")
        if requested_address and await chart.evaluate('location.origin + location.pathname') != requested_address:
            raise RuntimeError('指定TV布局已导航，未切换任何标的')
        yield chart, address, source_id, chart_id


@asynccontextmanager
async def same_window_chart(endpoint, console_origin, source_token, *, bindings=None):
    async with _console_chart(endpoint,console_origin,source_token,bindings=bindings) as (chart,address,_,__):
        yield chart, address


@asynccontextmanager
async def layout_chart(endpoint, console_origin, source_token, address):
    if _chart_address(address) != address:
        raise RuntimeError('TV布局地址配置无效')
    async with _console_chart(endpoint, console_origin, source_token, requested_address=address) as (chart, address, _, __):
        yield chart, address


async def bind_console_chart(endpoint, console_origin, source_token, target_id):
    async with _console_chart(endpoint,console_origin,source_token,requested_target=target_id) as (chart,address,source_id,chart_id):
        if await chart.evaluate('location.origin + location.pathname') != address:
            raise RuntimeError('TV图表已导航，请重新绑定')
        return {'source_id':source_id,'chart_id':chart_id,'address':address}
