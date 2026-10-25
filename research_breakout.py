"""盘前区间突破 + FVG 确认研究脚本（research_breakout.py）
==============================================================
全读本地研究库（research_db / logs/research.db），不依赖 TV 在线。

筛选口径（2026-10-24 与用户逐条定稿）：
1. 盘前区间 = 目标日 04:00–09:25 ET 的 5M 高低点
2. 从 09:30 ET（常规时段第一根 5M）起：收盘从区间内【越界】到区间外
   = 一次突破信号（站上：前收 ≤ 盘前高 & 本收 > 盘前高；跌破镜像）。
   **每一次突破都出信号**（收回来再越界算新的一次），不设延迟窗口
3. 突破的下一根 K 线：产生【同向】FVG（站上→需求区 fvg_event=1 /
   跌破→供应区 fvg_event=-1）且该根为【同向 K 线】（多=收>开 /
   空=收<开）
4. 动态升级：同一确认时刻信号数 ≤3 → gap 过滤即够；>3 → 再等一根，
   要求【三连阳】（突破根+FVG根+第三根全阳、高点依次抬高；空头镜像：
   三连阴、低点依次降低）
5. 输出的确认时间 = 突破的【下一根】（产生 gap 的那根）的 bar 时间

用法：
    python3 research_breakout.py --day 2026-10-23
    python3 research_breakout.py --day 2026-10-23 --json
    python3 research_breakout.py --day 2026-10-23 --min-hm 585   # 自定义起算点（分钟）
"""

import argparse
import json
from datetime import datetime
from zoneinfo import ZoneInfo

import research_db

ET = ZoneInfo("America/New_York")

# 期货/CFD 排除前缀（BLACKBULL:WTI 原油、CAPITALCOM:NAS100/SPX500 指数），
# 研究只看股票——2026-10-24 用户定稿「期货去除」
_EXCLUDE_PREFIXES = ("BLACKBULL:", "CAPITALCOM:")


def _hm(b):
    d = datetime.fromtimestamp(b["time"], ET)
    return d.hour * 60 + d.minute


def analyze_day(day: str, min_hm: int = 570) -> list:
    """返回通过全部过滤的突破信号列表（按确认时间排序）。

    两阶段：① 候选 = 收盘越界 + 下一根同向 FVG + 同向 K 线；
    ② 按确认时刻分组：组内 ≤3 只直接通过；>3 只升级要求三连阳/三连阴
    （动态强度：信号拥挤时才加严）。
    记录字段：symbol/ticker/side/confirm_hm/confirm_time（=下一根时间）/
    break_time/close/premarket_high/premarket_low/zone_width/
    crowd（同确认时刻候选数）/triple（是否走了三连确认）。
    """
    cands = []
    for sym in research_db.symbols():
        if sym.startswith(_EXCLUDE_PREFIXES):
            continue                               # 期货/CFD 不参与（只看股票）
        bars = research_db.load_day(sym, day)
        if not bars:
            continue
        pre = [b for b in bars if _hm(b) < 570]          # 09:30 前 = 盘前
        if not pre:
            continue
        pmh = max(b["high"] for b in pre)
        pml = min(b["low"] for b in pre)
        if pmh - pml <= 0:
            continue                                     # 盘前区间失真不参与
        for i, b in enumerate(bars):
            if _hm(b) < min_hm or i == 0:
                continue
            prev_close = bars[i - 1]["close"]
            # 越界判定：前收在区间内（或下方），本收越出 = 一次新突破
            if prev_close <= pmh and b["close"] > pmh:
                side, want_fvg = "站上高点", 1
            elif prev_close >= pml and b["close"] < pml:
                side, want_fvg = "跌破低点", -1
            else:
                continue
            nxt = bars[i + 1] if i + 1 < len(bars) else None
            if not nxt or nxt.get("fvg_event") != want_fvg:
                continue                                 # 无同向 FVG：不算候选
            # 同向 K 线（FVG 根本身）
            if side == "站上高点" and not nxt["close"] > nxt["open"]:
                continue
            if side == "跌破低点" and not nxt["close"] < nxt["open"]:
                continue
            # 三连确认（升级用，先算好备用）：突破根+FVG根+第三根
            third = bars[i + 2] if i + 2 < len(bars) else None
            triple = False
            if third is not None:
                if side == "站上高点":
                    triple = (b["close"] > b["open"] and nxt["close"] > nxt["open"]
                              and third["close"] > third["open"]
                              and b["high"] < nxt["high"] < third["high"])
                else:
                    triple = (b["close"] < b["open"] and nxt["close"] < nxt["open"]
                              and third["close"] < third["open"]
                              and b["low"] > nxt["low"] > third["low"])
            d = datetime.fromtimestamp(nxt["time"], ET)  # 确认时间 = 下一根
            cands.append({
                "symbol": sym, "ticker": sym.split(":")[-1], "side": side,
                "confirm_hm": _hm(nxt), "confirm_time": d.strftime("%H:%M"),
                "break_time": datetime.fromtimestamp(b["time"], ET)
                .strftime("%H:%M"),
                "close": b["close"],
                "premarket_high": round(pmh, 2), "premarket_low": round(pml, 2),
                "zone_width": round(pmh - pml, 2),
                "_triple": triple,
                "_third_time": (datetime.fromtimestamp(third["time"], ET)
                                .strftime("%H:%M") if third is not None else None),
                "_third_hm": _hm(third) if third is not None else None,
            })
    # 动态升级：同确认时刻候选 >3 只 → 只保留三连阳/阴通过的；≤3 全通过
    # 注意时间口径：走了三连确认的信号，确认时间延后到第三根（多等一根才知道结果）
    from collections import Counter
    crowd = Counter(c["confirm_hm"] for c in cands)
    rows = []
    for c in cands:
        n = crowd[c["confirm_hm"]]
        c["crowd"] = n
        triple = c.pop("_triple")
        third_time = c.pop("_third_time")
        third_hm = c.pop("_third_hm")
        if n > 3:
            if not triple:
                continue
            c["confirm_time"] = third_time    # 三连确认：延后到第三根
            c["confirm_hm"] = third_hm
            c["triple"] = True
        else:
            c["triple"] = False
        rows.append(c)
    rows.sort(key=lambda r: (r["confirm_hm"], r["ticker"]))
    return rows


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--day", required=True, help="美东交易日，如 2026-10-23")
    ap.add_argument("--min-hm", type=int, default=585,
                    help="起算点（分钟，默认 585=09:45 ET——2026-10-25 用户定：窗口 09:45–10:30）")
    ap.add_argument("--max-hm", type=int, default=630,
                    help="确认时间上限（分钟，默认 630=10:30 ET，之后的信号全部过滤）")
    ap.add_argument("--side", default="long", choices=["long", "short", "both"],
                    help="方向过滤：long=只显示站上高点（多头突破，默认——本方法只处理多头）/ short=只显示跌破低点 / both=全部")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    rows = analyze_day(args.day, args.min_hm)
    rows = [r for r in rows if r["confirm_hm"] <= args.max_hm]   # 时间窗口上限
    if args.side == "long":
        rows = [r for r in rows if r["side"] == "站上高点"]
    elif args.side == "short":
        rows = [r for r in rows if r["side"] == "跌破低点"]
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=1))
        return
    print(f"{'确认时间':<8}{'标的':<8}{'方向':<9}{'突破收盘':>9}"
          f"{'盘前高':>9}{'盘前低':>9}{'区间宽':>8}")
    print("-" * 60)
    for r in rows:
        print(f"{r['confirm_time']:<8}{r['ticker']:<8}{r['side']:<9}"
              f"{r['close']:>9}{r['premarket_high']:>9}"
              f"{r['premarket_low']:>9}{r['zone_width']:>8}")
    up = sum(1 for r in rows if r["side"] == "站上高点")
    print(f"\n真信号 {len(rows)} 条（站上 {up} / 跌破 {len(rows) - up}），"
          f"起算 {args.min_hm // 60:02d}:{args.min_hm % 60:02d} ET"
          f"（确认时间 = 突破的下一根 K 线）")


if __name__ == "__main__":
    main()
