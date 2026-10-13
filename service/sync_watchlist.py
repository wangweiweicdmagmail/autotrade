#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
从 TradingView 图表右侧自选表，同步股票池到 config.py 的 WATCHLIST。

用法：:

    python3 service/sync_watchlist.py

前置：TradingView Chrome 以 --remote-debugging-port=9222 运行且图表页已打开
（右侧自选表面板可见）。
"""

import asyncio
import json
import re
import urllib.request
from pathlib import Path

from websockets.asyncio.client import connect

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_ROOT / "config.py"


async def fetch_watchlist() -> list:
    targets = json.loads(
        urllib.request.urlopen("http://localhost:9222/json", timeout=5).read())
    chart = next((t for t in targets if t.get("type") == "page"
                  and "/chart/" in (t.get("url") or "")), None)
    if not chart:
        raise RuntimeError("未找到 TV 图表页：请先启动 TradingView Chrome(9222) 并打开图表")

    js = """(() => {
      const items = document.querySelectorAll('.layout__area--right [data-symbol-full]');
      return JSON.stringify([...items].map(el => el.getAttribute('data-symbol-full')));
    })()"""

    async with connect(chart["webSocketDebuggerUrl"], max_size=16*1024*1024) as ws:
        await ws.send(json.dumps({"id": 1, "method": "Runtime.evaluate",
                                  "params": {"expression": js, "returnByValue": True}}))
        while True:
            msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=15))
            if msg.get("id") == 1:
                return json.loads(msg["result"]["result"].get("value") or "[]")


def write_config(symbols: list) -> None:
    text = CONFIG_PATH.read_text(encoding="utf-8")
    lines = []
    for i, s in enumerate(symbols):
        comma = "," if i < len(symbols) - 1 else ""
        lines.append(f'    "{s}"{comma}')
    block = "WATCHLIST: List[str] = [\n" + "\n".join(lines) + "\n]"
    new_text, n = re.subn(
        r"WATCHLIST: List\[str\] = \[.*?\n\]", block, text, flags=re.DOTALL)
    if n != 1:
        raise RuntimeError("config.py 中未找到 WATCHLIST 定义")
    CONFIG_PATH.write_text(new_text, encoding="utf-8")


if __name__ == "__main__":
    syms = asyncio.run(fetch_watchlist())
    print(f"自选列表 {len(syms)} 只：")
    for s in syms:
        print("  ", s)
    write_config(syms)
    print("已写回 config.py 的 WATCHLIST")
