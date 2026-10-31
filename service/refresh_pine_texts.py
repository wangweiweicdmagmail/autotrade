#!/usr/bin/env python3
"""一键刷新 TradingView 私有指标正文缓存。

流程：translate 获取最新版本和正文 -> 缺正文时抓官方浏览器 create_study
-> 版本匹配后原子更新 -> 真实订阅验证；验证失败自动回滚。

用法：
  python3 service/refresh_pine_texts.py
  python3 service/refresh_pine_texts.py --restart   # 验证后重启并核验实盘
"""
import argparse
import asyncio
import json
import os
import re
import selectors
import shutil
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

from websockets.legacy.client import connect

ROOT = Path(__file__).resolve().parent.parent
SERVICE = ROOT / "service"
CREDS_PATH = SERVICE / "creds.json"
OUT_PATH = SERVICE / "pine_texts.json"
LAYOUT_URL = "https://cn.tradingview.com/chart/I2KwX1s0/"
CDP_URL = "http://localhost:9222"
INDICATORS = {
    "trend_state": "USER;e577100fe79942b4830783d1ceaad535",
    "fvg_finder": "USER;5ec7d04179b3429ca9286db452352d85",
    "smart_stop": "USER;ca26e3ab587841e2adb38cfbfd0bec41",
    "ema9_autotrade": "USER;a17a52ae5b6642e0a3ddbba23edc9ff3",
    "attack_zone": "USER;8ab4f534e5874b47b24a5ce5c49acace",
}
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def same_version(a, b):
    def normalize(value):
        parts = str(value or "").strip().lstrip("vV").split(".")
        while len(parts) > 1 and parts[-1] == "0":
            parts.pop()
        return ".".join(parts)
    left = normalize(a)
    return bool(left) and left == normalize(b)


def parse_study_frames(payload, required):
    found = {}
    for raw in re.findall(r'\{"m":"[a-z_]+","p":\[.*?\]\}(?=~m~|$)', payload):
        try:
            msg = json.loads(raw)
            params = msg.get("p") or []
            if msg.get("m") != "create_study" or len(params) < 6:
                continue
            options = params[5]
            if not isinstance(options, dict):
                continue
            pine_id = options.get("pineId")
            version = options.get("pineVersion")
            text = options.get("text")
            if pine_id in required and text and same_version(version, required[pine_id]):
                found[pine_id] = {"version": required[pine_id], "text": text}
        except (ValueError, TypeError, AttributeError):
            continue
    return found


def load_creds():
    return json.loads(CREDS_PATH.read_text(encoding="utf-8"))


def remote_indicator(pine_id, creds):
    url = ("https://pine-facade.tradingview.com/pine-facade/translate/"
           + pine_id.replace(" ", "%25").replace("%", "%25") + "/last")
    req = urllib.request.Request(url, headers={
        "Cookie": f"sessionid={creds['sessionid']};sessionid_sign={creds['sessionid_sign']}",
        "User-Agent": "Mozilla/5.0",
    })
    with OPENER.open(req, timeout=20) as response:
        data = json.load(response)
    if not data.get("success") or not data.get("result"):
        raise RuntimeError(f"translate 失败: {pine_id}: {data.get('reason') or 'unknown'}")
    meta = data["result"].get("metaInfo") or {}
    version = (meta.get("pine") or {}).get("version")
    if not version:
        raise RuntimeError(f"远端版本缺失: {pine_id}")
    text = ""
    for item in meta.get("inputs") or []:
        if item.get("id") == "text" and isinstance(item.get("defval"), str):
            text = item["defval"]
            break
    if not text and isinstance(data["result"].get("ilTemplate"), str):
        text = data["result"]["ilTemplate"]
    return {"version": version, "text": text}


def list_targets():
    with OPENER.open(CDP_URL + "/json/list", timeout=5) as response:
        return json.load(response)


def find_chart_page():
    return next((t for t in list_targets() if t.get("type") == "page"
                 and "tradingview.com/chart" in t.get("url", "")), None)


def ensure_chart_page():
    try:
        page = find_chart_page()
    except Exception:
        subprocess.Popen([
            "open", "-na", "Google Chrome", "--args",
            "--remote-debugging-port=9222",
            "--user-data-dir=" + str(Path.home() / ".tv-chrome-profile"),
            "--no-first-run", LAYOUT_URL,
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        page = None
    deadline = time.time() + 30
    while time.time() < deadline:
        try:
            page = page or find_chart_page()
            if page:
                return page
            req = urllib.request.Request(
                CDP_URL + "/json/new?" + urllib.parse.quote(LAYOUT_URL, safe=":/?"),
                method="PUT")
            with OPENER.open(req, timeout=5) as response:
                page = json.load(response)
        except Exception:
            time.sleep(1)
    raise RuntimeError("无法打开 TV 图表页（9222）")


async def capture_official_texts(required):
    page = ensure_chart_page()
    found = {}
    seen_versions = {}
    async with connect(page["webSocketDebuggerUrl"], max_size=64 * 1024 * 1024) as ws:
        mid = 0

        async def send(method, params=None):
            nonlocal mid
            mid += 1
            await ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))

        await send("Network.enable")
        await send("Page.enable")
        await send("Page.reload", {"ignoreCache": True})
        print("已重载 TV 图表，等待官方 create_study 帧...", flush=True)
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline and len(found) < len(required):
            try:
                msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
            except asyncio.TimeoutError:
                continue
            if msg.get("method") != "Network.webSocketFrameSent":
                continue
            payload = msg["params"]["response"].get("payloadData", "")
            for raw in re.findall(r'\{"m":"[a-z_]+","p":\[.*?\]\}(?=~m~|$)', payload):
                try:
                    packet = json.loads(raw)
                    params = packet.get("p") or []
                    if len(params) < 6 or not isinstance(params[5], dict):
                        continue
                    options = params[5]
                    pine_id = options.get("pineId")
                    if pine_id in required:
                        seen_versions[pine_id] = options.get("pineVersion")
                except (ValueError, TypeError, AttributeError):
                    continue
            found.update(parse_study_frames(payload, required))
    missing = set(required) - set(found)
    if missing:
        details = ", ".join(f"{pid}(布局v{seen_versions.get(pid) or '未知'}"
                            f"/远端v{required[pid]})" for pid in sorted(missing))
        raise RuntimeError("官方帧正文不完整或布局版本落后: " + details)
    return found


def atomic_write(payload):
    tmp = OUT_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, OUT_PATH)


def validate_subscription():
    proc = subprocess.Popen(
        ["node", "tv_cloud_daemon.js", "--symbols", "NASDAQ:QQQ"],
        cwd=SERVICE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, bufsize=1)
    sel = selectors.DefaultSelector()
    sel.register(proc.stdout, selectors.EVENT_READ, "stdout")
    sel.register(proc.stderr, selectors.EVENT_READ, "stderr")
    logs = []
    deadline = time.monotonic() + 90
    try:
        while time.monotonic() < deadline:
            for key, _ in sel.select(timeout=1):
                line = key.fileobj.readline()
                if not line:
                    continue
                if key.data == "stderr":
                    logs.append(line.rstrip())
                    logs[:] = logs[-20:]
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if msg.get("type") != "tick":
                    continue
                data = (msg.get("symbols") or {}).get("NASDAQ:QQQ") or {}
                ts_c = data.get("ts_c") or {}
                attack = data.get("attack_c") or {}
                checks = {
                    "bar": bool(data.get("bar")),
                    "trend": ts_c.get("trend_state") is not None,
                    "fvg": bool(data.get("fvg_c")),
                    "smart_stop": (ts_c.get("smart_stop_long") is not None
                                   or ts_c.get("smart_stop_short") is not None),
                    "attack": attack.get("pmh") is not None and attack.get("pml") is not None,
                }
                if all(checks.values()):
                    return {"pmh": attack["pmh"], "pml": attack["pml"]}
            if proc.poll() is not None:
                break
        raise RuntimeError("真实订阅验证失败: " + " | ".join(logs[-5:]))
    finally:
        sel.close()
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()


def verify_live():
    sys.path.insert(0, str(ROOT))
    from config import WATCHLIST
    with OPENER.open("http://127.0.0.1:8000/api/market", timeout=10) as response:
        symbols = json.load(response).get("symbols", {})
    ready = sum(1 for data in symbols.values()
                if (data.get("attack_c") or {}).get("pmh") is not None)
    if len(symbols) < len(WATCHLIST) or ready < len(WATCHLIST):
        raise RuntimeError(f"实盘 attack 就绪不足: {ready}/{len(WATCHLIST)}")
    print(f"实盘核验通过: attack {ready}/{len(WATCHLIST)}", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--restart", action="store_true", help="验证后重启并核验实盘")
    args = parser.parse_args()
    creds = load_creds()
    candidates = {}
    for name, pine_id in INDICATORS.items():
        remote = remote_indicator(pine_id, creds)
        candidates[pine_id] = remote
        print(f"{name}: 远端 v{remote['version']}，translate 正文 {len(remote['text'])} 字节", flush=True)
    missing = {pid: meta["version"] for pid, meta in candidates.items() if not meta["text"]}
    if missing:
        captured = asyncio.run(capture_official_texts(missing))
        for pine_id, meta in captured.items():
            candidates[pine_id] = meta
    for name, pine_id in INDICATORS.items():
        meta = candidates[pine_id]
        if not meta["text"] or not same_version(meta["version"], remote_indicator(pine_id, creds)["version"]):
            raise RuntimeError(f"正文版本校验失败: {name}")
        print(f"{name}: 使用 v{meta['version']} 正文 {len(meta['text'])} 字节", flush=True)

    old = json.loads(OUT_PATH.read_text(encoding="utf-8")) if OUT_PATH.exists() else {}
    updated = dict(old)
    for pine_id, meta in candidates.items():
        updated[pine_id] = {"version": meta["version"], "text": meta["text"]}
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = ROOT / "logs" / f"pine_texts.backup-{stamp}.json"
    if OUT_PATH.exists():
        shutil.copy2(OUT_PATH, backup)
    atomic_write(updated)
    try:
        result = validate_subscription()
    except Exception:
        atomic_write(old)
        raise
    print(f"真实订阅验证通过: 盘前区间 {result['pmh']}~{result['pml']}；备份 {backup}", flush=True)
    if args.restart:
        subprocess.run([str(ROOT / "restart_live.sh")], cwd=ROOT, check=True)
        verify_live()


if __name__ == "__main__":
    main()
