#!/usr/bin/env python3
"""TV WS 协议抓包工具 —— 协议类故障的终极诊断手段。

原理：TV 浏览器端永远是对的（官方前端），库发错包时，抓浏览器真实流量
逐字段对比即可定位（2026-10-27 的 text 缺失就是这样发现的，~10 分钟定位）。

用法：
  python3 service/sniff_tv_ws.py                # 抓上行帧里的关键方法包，打全量
  python3 service/sniff_tv_ws.py create_study   # 只看指定方法（可多个，空格分隔）

前提：TV Chrome（9222，已登录）开着图表页。脚本会重载该页面（布局云端同步，
重载无损），抓 75 秒后自动结束，输出写 /tmp/tv_ws_sniff.txt。
"""
import asyncio
import json
import re
import sys
import urllib.request

from websockets.legacy.client import connect

OUT = "/tmp/tv_ws_sniff.txt"
METHODS = sys.argv[1:] or ["set_auth_token", "create_study", "create_series", "resolve_symbol"]


async def main():
    with urllib.request.urlopen("http://localhost:9222/json/list", timeout=5) as r:
        targets = json.load(r)
    page = next(
        (t for t in targets
         if t.get("type") == "page" and "tradingview.com/chart" in t.get("url", "")),
        None,
    )
    if not page:
        sys.exit("未找到 TV 图表页（9222）。先启动 TV Chrome。")

    out = open(OUT, "w")
    async with connect(page["webSocketDebuggerUrl"], max_size=64 * 1024 * 1024) as ws:
        mid = 0

        async def send(method, params=None):
            nonlocal mid
            mid += 1
            await ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))

        await send("Network.enable")
        await send("Page.enable")
        await send("Page.reload", {"ignoreCache": True})
        print(f"重载页面，抓取 {METHODS} ...（75 秒）", flush=True)
        deadline = asyncio.get_event_loop().time() + 75
        while asyncio.get_event_loop().time() < deadline:
            try:
                msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
            except asyncio.TimeoutError:
                continue
            if msg.get("method") != "Network.webSocketFrameSent":
                continue
            payload = msg["params"]["response"].get("payloadData", "")
            for m in METHODS:
                if f'"{m}"' in payload:
                    # 一个帧可能含多个包，逐个拆
                    for pkt in re.findall(r'\{"m":"[a-z_]+","p":\[.*?\]\}(?=~m~|$)', payload):
                        if f'"{m}"' in pkt:
                            out.write(pkt + "\n\n")
                            print(f"[{m}] {pkt[:160]}", flush=True)
    out.close()
    print(f"完成，全量包已写 {OUT}", flush=True)


asyncio.run(main())
