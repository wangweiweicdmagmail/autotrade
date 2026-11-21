"""Arrange the dedicated macOS Chrome workspace without trading dependencies."""

import asyncio
import json
import sys
from pathlib import Path
from urllib.parse import quote, urlsplit
from urllib.request import ProxyHandler, build_opener


async def ensure_independent_windows(endpoint: str, urls: list[str]) -> dict[str, str]:
    """Reuse exact pages; create missing pages in new windows, never close/reload.

    Restored tabs sharing a window are reported rather than discarding unsaved
    chart edits or console order drafts to recreate them.
    """
    from controller.window_chart import _pages
    from controller.tv_cdp import TradingViewCDPController

    if urlsplit(endpoint).hostname not in {'localhost', '127.0.0.1', '::1'}:
        raise RuntimeError('独立工作区只支持本机专用 Chrome')
    if len(set(urls)) != 3:
        raise RuntimeError('工作区需要两个不同TV布局和一个控制台地址')
    targets = {}
    for url in urls:
        pages = await asyncio.to_thread(_pages, endpoint)
        matches = [p for p in pages if p.get('url', '').split('?', 1)[0] == url]
        if len(matches) > 1:
            raise RuntimeError('工作区页面存在多个副本，请只保留一个：'+url)
        if matches:
            targets[url] = matches[0]['id']
            continue
        if not pages:
            raise RuntimeError('专用 Chrome 尚无可连接的页面')
        async with TradingViewCDPController(endpoint, target_id=pages[0]['id']) as browser:
            created = await browser._send_cdp('Target.createTarget', {'url': url, 'newWindow': True, 'background': True})
        if not created.get('targetId'):
            raise RuntimeError('Chrome未确认新窗口：'+url)
        targets[url] = created['targetId']
    windows = []
    for target in targets.values():
        async with TradingViewCDPController(endpoint, target_id=target) as page:
            windows.append(await page.window_id())
    if None in windows or len(set(windows)) != 3:
        raise RuntimeError('请把 auto_trade、auto_trade2 和控制台分别移到三个独立窗口；现有页面已保留')
    return targets


def workspace_pages(endpoint: str, console_url: str) -> tuple[dict, dict]:
    """Read only tab metadata; no account, credential or page body access."""
    opener = build_opener(ProxyHandler({}))
    with opener.open(f"{endpoint}/json", timeout=3) as response:
        pages = [p for p in json.load(response) if p.get("type") == "page"]
    console = next((p for p in pages if p.get("url", "").startswith(console_url)), None)
    chart = next((p for p in pages if
                  (urlsplit(p.get("url", "")).hostname or "").endswith(".tradingview.com")
                  and urlsplit(p.get("url", "")).path.startswith("/chart/")), None)
    if not console or not chart:
        raise RuntimeError("控制台或 TradingView 图表标签页尚未就绪")
    return console, chart


def activate_page(endpoint: str, page_id: str) -> None:
    """Activate the existing chart tab before opening the split-view picker."""
    opener = build_opener(ProxyHandler({}))
    with opener.open(f"{endpoint}/json/activate/{quote(page_id, safe='')}", timeout=3) as response:
        response.read()


async def arrange_split(endpoint: str, console_url: str) -> None:
    """Confirm native split ordering, or raise a visible startup warning.

    UI automation is confined to the CDP listener PID, never ordinary Chrome
    selected by application name. Existing correctly ordered splits are reused.
    """
    endpoint_parts = urlsplit(endpoint)
    if endpoint_parts.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise RuntimeError("自动分屏只支持本机专用 Chrome")
    port = endpoint_parts.port or 9222
    console, chart = await asyncio.to_thread(workspace_pages, endpoint, console_url)
    proc = await asyncio.create_subprocess_exec(
        "lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    out, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
    pids = set(out.decode().split())
    if len(pids) != 1 or not next(iter(pids)).isdigit():
        raise RuntimeError("无法唯一确定交易工作区浏览器进程")
    if not chart.get("id"):
        raise RuntimeError("TradingView 图表标签页标识缺失，未调整分屏")
    await asyncio.to_thread(activate_page, endpoint, chart["id"])
    script = Path(__file__).with_name("mac_workspace.py")
    proc = await asyncio.create_subprocess_exec(
        sys.executable, str(script), next(iter(pids)), console_url, chart["url"],
        console.get("title") or "autotrade", urlsplit(console_url).netloc,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=30)
    except asyncio.TimeoutError as exc:
        proc.kill()
        await proc.communicate()
        raise RuntimeError("自动分屏超过 30 秒未完成；请检查 Chrome 窗口响应及 macOS 辅助功能权限") from exc
    except asyncio.CancelledError:
        proc.kill()
        await proc.communicate()
        raise
    if proc.returncode or out.decode().strip() != "split-ready":
        reason = err.decode(errors="replace").strip()
        if any(key in reason.lower() for key in ("assistive", "辅助", "-1743", "not authorized")):
            raise RuntimeError("自动分屏需要给运行控制台的应用开启 macOS 辅助功能权限")
        raise RuntimeError(reason or "Chrome 未确认左侧 TradingView、右侧控制台")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="仅整理浏览器分屏，不启动交易服务")
    parser.add_argument("--endpoint", default="http://localhost:9222")
    parser.add_argument("--console-url", default="http://127.0.0.1:8000/")
    args = parser.parse_args()
    asyncio.run(arrange_split(args.endpoint, args.console_url))
    print("左侧 TradingView · 右侧 autotrade 分屏已确认")
