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

    # 自选表是虚拟列表（只渲染可见行），必须边滚动边收集取并集，
    # 否则只能读到前 ~44 行（2026-10-16 实测 scrollHeight 1644 只渲染 44/52）。
    js = """(async () => {
      const root = document.querySelector('.layout__area--right');
      if (!root) return '[]';
      const rows = root.querySelectorAll('[data-symbol-full]');
      if (!rows.length) return '[]';
      let sc = rows[0].parentElement;
      while (sc && sc.scrollHeight <= sc.clientHeight + 5) sc = sc.parentElement;
      const seen = new Set();
      const collect = () => root.querySelectorAll('[data-symbol-full]')
        .forEach(el => seen.add(el.getAttribute('data-symbol-full')));
      if (!sc) { collect(); return JSON.stringify([...seen]); }
      sc.scrollTop = 0;
      await new Promise(r => setTimeout(r, 400));
      let stable = 0, last = -1, iter = 0;
      while (stable < 3 && iter < 60) {
        collect();
        if (seen.size === last) stable++; else { stable = 0; last = seen.size; }
        sc.scrollTop += Math.max(200, sc.clientHeight - 100);
        await new Promise(r => setTimeout(r, 250));
        iter++;
      }
      sc.scrollTop = sc.scrollHeight;
      await new Promise(r => setTimeout(r, 400));
      collect();
      sc.scrollTop = 0;
      return JSON.stringify([...seen]);
    })()"""

    async with connect(chart["webSocketDebuggerUrl"], max_size=16*1024*1024) as ws:
        await ws.send(json.dumps({"id": 1, "method": "Runtime.evaluate",
                                  "params": {"expression": js, "returnByValue": True,
                                             "awaitPromise": True}}))
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
