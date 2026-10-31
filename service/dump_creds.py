#!/usr/bin/env python3
"""从 TV Chrome (9222，已登录) 提取 sessionid/sessionid_sign 写入 service/creds.json（600 权限，原子写）。"""
import asyncio, json, os, sys, urllib.request

from websockets.legacy.client import connect

CREDS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "creds.json")

async def main():
    with urllib.request.urlopen("http://localhost:9222/json/list", timeout=5) as r:
        targets = json.load(r)
    page = next(
        (t for t in targets
         if t.get("type") == "page" and "tradingview.com/chart" in t.get("url", "")),
        None,
    )
    if not page:
        sys.exit("no tradingview chart page found on 9222")
    async with connect(page["webSocketDebuggerUrl"], max_size=16 * 1024 * 1024) as ws:
        await ws.send(json.dumps({"id": 1, "method": "Network.getAllCookies"}))
        while True:
            msg = json.loads(await ws.recv())
            if msg.get("id") == 1:
                cookies = msg["result"]["cookies"]
                break
    tv = [c for c in cookies if "tradingview.com" in c.get("domain", "")]
    sid = next((c["value"] for c in tv if c["name"] == "sessionid"), None)
    sign = next((c["value"] for c in tv if c["name"] == "sessionid_sign"), None)
    if not sid or not sign:
        sys.exit(f"sessionid/sign not found (sessionid={'yes' if sid else 'no'})")
    tmp = CREDS_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"sessionid": sid, "sessionid_sign": sign}, f)
    os.chmod(tmp, 0o600)
    os.replace(tmp, CREDS_PATH)
    print(f"ok: wrote {CREDS_PATH} (sessionid len={len(sid)}, sign len={len(sign)})")

asyncio.run(main())
