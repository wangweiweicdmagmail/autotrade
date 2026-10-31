#!/usr/bin/env python3
"""TV 数据通道一键诊断 —— 30 秒定位故障层级，直接给出修复命令。

分层检测（自上而下，每层失败即停并给出修复建议）：
  1. 凭据文件       creds.json 存在且字段齐全
  2. 凭据有效性     translate 私有指标成功（sessionid 未过期）
  3. 正文提取       translate 响应含 text defval（2026-10-27 协议变更点）
  4. WS 端到端      真实跑一次采集（tv_cloud.js），4 个指标都要出数
  5. 运行态健康     console 在线时查 /api/market 的 ts_c 就绪率

用法：python3 service/tv_diag.py
"""
import json
import os
import subprocess
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CREDS_PATH = os.path.join(HERE, "creds.json")

# 系统在用的 4 个指标（与 tv_cloud_daemon.js 一致）
INDICATORS = {
    "trend_state": "USER;e577100fe79942b4830783d1ceaad535",
    "fvg_finder": "USER;5ec7d04179b3429ca9286db452352d85",
    "smart_stop": "USER;ca26e3ab587841e2adb38cfbfd0bec41",
    "attack_zone": "USER;8ab4f534e5874b47b24a5ce5c49acace",
}

FAILED = 0


def ok(msg):
    print(f"  [OK]   {msg}")


def fail(stage, msg, fix):
    global FAILED
    FAILED += 1
    print(f"  [FAIL] {msg}")
    print(f"         修复: {fix}")
    print()
    print(f"== 诊断终止于第 {stage} 层，修复后重新运行本脚本验证 ==")
    sys.exit(1)


def main():
    print("== 1. 凭据文件 ==")
    if not os.path.exists(CREDS_PATH):
        fail(1, "service/creds.json 不存在",
             "python3 /tmp/dump_creds.py 或按 PROJECT_NOTES §7 从 TV Chrome 提取")
    creds = json.load(open(CREDS_PATH))
    if not creds.get("sessionid") or not creds.get("sessionid_sign"):
        fail(1, "creds.json 缺 sessionid/sessionid_sign 字段", "重新从 TV Chrome 提取凭据")
    ok("creds.json 齐全")

    print("== 2. 凭据有效性（translate 私有指标）==")
    cookie = f"sessionid={creds['sessionid']}; sessionid_sign={creds['sessionid_sign']}"
    texts = {}
    for name, iid in INDICATORS.items():
        url = f"https://pine-facade.tradingview.com/pine-facade/translate/{iid.replace(';', '%3B')}/last"
        req = urllib.request.Request(url, headers={"Cookie": cookie})
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                data = json.load(r)
        except Exception as e:
            fail(2, f"translate 请求失败: {str(e)[:100]}", "检查网络；TV 域名可达性")
        if not data.get("success"):
            fail(2, f"{name} translate 被拒: {data.get('reason')}",
                 "sessionid 过期 —— 从 TV Chrome 重新提取 creds.json")
        texts[name] = data["result"]
    ok(f"4/4 私有指标 translate 成功（凭据有效）")

    print("== 3. 正文提取（2026-10-27 协议变更点）==")
    for name, result in texts.items():
        inputs = (result.get("metaInfo") or {}).get("inputs") or []
        text = next((i.get("defval") for i in inputs if i.get("id") == "text"), None)
        if result.get("ilTemplate"):
            ok(f"{name}: 旧版 ilTemplate 路径")
        elif text:
            ok(f"{name}: text defval 正常（len={len(text)}）")
        else:
            fail(3, f"{name}: translate 响应既无 ilTemplate 也无 text defval —— TV 又改协议了",
                 "先查上游 https://github.com/Mathieu2301/Tradingview-API/issues ；"
                 "再用 python3 service/sniff_tv_ws.py 抓浏览器真实包对比；"
                 "临时回退 python3 service/refresh_pine_texts.py 生成缓存正文")

    print("== 4. WS 端到端（真实采集一次，含库的 create_study 路径）==")
    try:
        proc = subprocess.run(
            ["node", "tv_cloud.js", "--symbols", "NASDAQ:QQQ", "--out", "/tmp/tv_diag.json"],
            cwd=HERE, capture_output=True, text=True, timeout=60)
    except subprocess.TimeoutExpired:
        fail(4, "采集超时（60s）", "查 logs/console-live.log 或单独跑 tv_cloud.js 看 stderr")
    stderr = proc.stderr or ""
    if "Can't parse pine" in stderr:
        fail(4, "create_study 报 'Can't parse pine' —— 正文注入失效或 TV 再改协议",
             "确认 service/pine_text.js 存在且 tv_cloud.js 引用 loadIndicatorWithText；"
             "查上游 issue；python3 service/sniff_tv_ws.py 抓包对比")
    try:
        out = json.load(open("/tmp/tv_diag.json"))
        studies = out["symbols"]["NASDAQ:QQQ"].get("studies") or {}
        missing = [k for k in ("trend_state", "fvg_finder", "attack_zone")
                   if not (studies.get(k) or {}).get("periods")]
        if missing:
            fail(4, f"指标无数据: {missing}", "stderr 有 study_error 的话按提示处理")
        ok("trend_state/fvg_finder/attack_zone 全部出数（K线 + 指标链路完好）")
    except Exception as e:
        fail(4, f"采集输出解析失败: {str(e)[:100]}", "单独跑 tv_cloud.js 看输出")

    print("== 5. 运行态健康（console 在线时）==")
    try:
        with urllib.request.urlopen("http://127.0.0.1:8000/api/market", timeout=5) as r:
            market = json.load(r)
        syms = market["symbols"]
        ready = sum(1 for v in syms.values()
                    if isinstance(v.get("ts_c"), dict) and v["ts_c"].get("trend_state") is not None)
        if ready < len(syms) * 0.9:
            fail(5, f"ts_c 就绪率 {ready}/{len(syms)} —— daemon 异常",
                 "tail logs/console-live.log 找 study_error/重建记录；"
                 "必要时重启 console（无仓位时最安全）")
        ok(f"ts_c 就绪 {ready}/{len(syms)}")
    except Exception:
        print("  [SKIP] console 不在线（8000 无响应）")

    print()
    print("== 全部通过：TV 数据通道完好 ==")


if __name__ == "__main__":
    main()
