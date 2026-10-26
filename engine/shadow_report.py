# -*- coding: utf-8 -*-
"""
每日复盘报告 (shadow_report)
============================

收盘后生成当日影子复盘报告，回答三类问题：
1. 分组统计：信号类型 × 方向（笔数/成交率/胜率/平均R/期望值/reached_2R/
   hit_stop 占比）；breakout 组按 meta.crowd 细分（≤3 直接确认 vs >3 三连
   升级）——回答「三连升级有没有用」。
2. 抓到 vs 漏掉（突破类）：用 bars 表重算当日 RTH 大行情（开盘→收盘
   ≥+3%），对照当日 breakout 影子单：抓住 / 漏掉（原因分类：未越界 /
   无FVG / FVG根反向 / 拥挤三连失败 / 超时间窗）。候选重推口径与
   research_breakout.analyze_day 一致（多空混算拥挤）。
3. 止损松紧：hit_stop 单中「止损距离 < 1×ATR」占比；hit_stop 单的 MFE
   分布（曾浮盈 >0.5R / >1R 的比例——「曾经赚过」被打回止损的比例）。

数据全从 research.db 读（shadow_signals + bars），纯逻辑无 IO 副作用。

用法：
    from engine.shadow_report import generate_report
    report = generate_report("2026-10-23")
"""

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

import research_db

ET = ZoneInfo("America/New_York")

MIN_HM = 9 * 60 + 45              # 突破检测窗口起点 09:45 ET（同 tracker）
MAX_HM = 10 * 60 + 30             # 确认时刻上限 10:30 ET（含三连延后）
PM_END_HM = 9 * 60 + 30           # 盘前 = hm < 570
RTH_END_HM = 16 * 60              # RTH 收盘 16:00 ET
BIG_MOVE_PCT = 3.0                # 大行情阈值：RTH 开盘→收盘 ≥ +3%
EXCLUDE_PREFIXES = ("BLACKBULL:", "CAPITALCOM:")


def _hm(ts: int) -> int:
    d = datetime.fromtimestamp(ts, ET)
    return d.hour * 60 + d.minute


def _day_range(day: str):
    d0 = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=ET)
    d1 = d0 + timedelta(days=1)
    return int(d0.timestamp()), int(d1.timestamp())


# ----------------------------------------------------------------------
# 1) 分组统计
# ----------------------------------------------------------------------

def _group_stats(rows: list) -> dict:
    """一组影子单的统计。胜率/平均R/期望值：期望值分母含未成交（每信号期望），
    胜率/平均R 只算已成交已结算。"""
    n = len(rows)
    filled = [r for r in rows if r["filled"]]
    traded = [r for r in filled if r["settled"]]
    wins = [r for r in traded if (r["outcome_r"] or 0) > 0]
    return {
        "count": n,
        "filled": len(filled),
        "fill_rate": round(len(filled) / n * 100, 1) if n else 0.0,
        "traded": len(traded),
        "win_rate": round(len(wins) / len(traded) * 100, 1) if traded else 0.0,
        "avg_r": (round(sum(r["outcome_r"] or 0 for r in traded)
                        / len(traded), 3) if traded else 0.0),
        "expectancy": (round(sum(r["outcome_r"] or 0 for r in rows) / n, 3)
                       if n else 0.0),
        "reached_2r_pct": (round(sum(1 for r in traded if r["reached_2r"])
                                 / len(traded) * 100, 1) if traded else 0.0),
        "hit_stop_pct": (round(sum(1 for r in traded if r["hit_stop"])
                               / len(traded) * 100, 1) if traded else 0.0),
    }


def _groups(signals: list) -> list:
    out = []
    keys = sorted({(r["type"], r["side"]) for r in signals})
    for t, side in keys:
        rows = [r for r in signals if r["type"] == t and r["side"] == side]
        out.append({"type": t, "side": side, **_group_stats(rows)})
    return out


def _breakout_crowd_split(signals: list) -> list:
    """breakout 组按拥挤度细分：crowd<=3 直接确认 vs crowd>3 三连升级。"""
    out = []
    for label, pred in (("直接确认(crowd≤3)", lambda c: c <= 3),
                        ("三连升级(crowd>3)", lambda c: c > 3)):
        rows = []
        for r in signals:
            if r["type"] != "breakout":
                continue
            meta = json.loads(r["meta"]) if r.get("meta") else {}
            crowd = meta.get("crowd") or 1
            if pred(crowd):
                rows.append(r)
        if rows:
            out.append({"bucket": label, **_group_stats(rows)})
    return out


# ----------------------------------------------------------------------
# 2) 抓到 vs 漏掉（突破类大行情对照）
# ----------------------------------------------------------------------

def _raw_candidates(all_bars: dict):
    """重推当日全部突破候选（多空都含，拥挤混算口径与
    research_breakout.analyze_day 一致，但保留每只候选的拒绝点信息）。
    返回 (cands, long_crossed)：long_crossed = 窗口内（hm≥585）站上
    盘前高点的 symbol 集合（区分「未越界」与「超时间窗」用）。"""
    cands = []
    long_crossed = set()
    for sym, bars in all_bars.items():
        if sym.startswith(EXCLUDE_PREFIXES):
            continue
        pre = [b for b in bars if _hm(b["time"]) < PM_END_HM]
        if not pre:
            continue
        pmh = max(b["high"] for b in pre)
        pml = min(b["low"] for b in pre)
        if pmh - pml <= 0:
            continue
        for i, b in enumerate(bars):
            hm = _hm(b["time"])
            if hm < MIN_HM or i == 0:
                continue
            prev_close = bars[i - 1]["close"]
            if prev_close <= pmh and b["close"] > pmh:
                long_crossed.add(sym)
                side, want = "long", 1
            elif prev_close >= pml and b["close"] < pml:
                side, want = "short", -1
            else:
                continue
            nxt = bars[i + 1] if i + 1 < len(bars) else None
            third = bars[i + 2] if i + 2 < len(bars) else None
            if not nxt:
                continue                        # 与批量口径一致：无下一根不算候选
            reject = None
            if nxt.get("fvg_event") != want:
                reject = "无FVG"
            elif side == "long" and not nxt["close"] > nxt["open"]:
                reject = "FVG根反向"
            elif side == "short" and not nxt["close"] < nxt["open"]:
                reject = "FVG根反向"
            triple = False
            if third is not None and reject is None:
                if side == "long":
                    triple = (b["close"] > b["open"] and nxt["close"] > nxt["open"]
                              and third["close"] > third["open"]
                              and b["high"] < nxt["high"] < third["high"])
                else:
                    triple = (b["close"] < b["open"] and nxt["close"] < nxt["open"]
                              and third["close"] < third["open"]
                              and b["low"] > nxt["low"] > third["low"])
            confirm_hm = _hm(nxt["time"])
            cands.append({
                "symbol": sym, "side": side, "reject": reject,
                "confirm_hm": confirm_hm, "triple": triple,
                "third_hm": _hm(third["time"]) if third is not None else None,
            })
    return cands, long_crossed


def _big_moves(day: str, signals: list, db_path: Path) -> dict:
    """RTH ≥+3% 大行情 × 当日 breakout 影子单对照。"""
    t0, t1 = _day_range(day)
    all_bars = {}
    for sym in research_db.symbols(db_path):
        bars = research_db.load_bars(sym, t0, t1, db_path)
        if bars:
            all_bars[sym] = bars
    cands, long_crossed = _raw_candidates(all_bars)
    from collections import Counter
    crowd = Counter(c["confirm_hm"] for c in cands if c["reject"] is None)

    caught_syms = {r["symbol"] for r in signals
                   if r["type"] == "breakout" and r["side"] == "long"
                   and r["filled"]}
    caught_rows = {r["symbol"]: r for r in signals
                   if r["type"] == "breakout" and r["side"] == "long"
                   and r["filled"]}

    caught, missed = [], []
    for sym, bars in all_bars.items():
        if sym.startswith(EXCLUDE_PREFIXES):
            continue
        rth = [b for b in bars if PM_END_HM <= _hm(b["time"]) < RTH_END_HM]
        if len(rth) < 2:
            continue
        open0, close_last = rth[0]["open"], rth[-1]["close"]
        if not open0 or not close_last or open0 <= 0:
            continue
        pct = round((close_last / open0 - 1) * 100, 2)
        if pct < BIG_MOVE_PCT:
            continue
        if sym in caught_syms:
            r = caught_rows[sym]
            caught.append({"symbol": sym, "pct": pct, "entry": r["trigger_px"],
                           "outcome": r["outcome"], "outcome_r": r["outcome_r"],
                           "mfe_r": r["mfe_r"]})
            continue
        # ---- 漏掉原因分类 ----
        my = [c for c in cands if c["symbol"] == sym and c["side"] == "long"]
        valid = [c for c in my if c["reject"] is None]
        if not my:
            # 无候选：窗口内越界过但无有效确认（如收盘后才越界）→ 超时间窗；
            # 从未越界 → 未越界
            reason = "超时间窗" if sym in long_crossed else "未越界"
        elif not valid:
            reason = my[0]["reject"]              # 无FVG / FVG根反向
        else:
            c = valid[0]
            final_hm = c["third_hm"] if crowd[c["confirm_hm"]] > 3 \
                else c["confirm_hm"]
            if final_hm is None or final_hm > MAX_HM:
                reason = "超时间窗"
            elif crowd[c["confirm_hm"]] > 3 and not c["triple"]:
                reason = "拥挤三连失败"
            else:
                reason = "已出信号但未建档"        # 兜底（理论上不该出现）
        missed.append({"symbol": sym, "pct": pct, "reason": reason})
    caught.sort(key=lambda r: -r["pct"])
    missed.sort(key=lambda r: -r["pct"])
    return {
        "threshold_pct": BIG_MOVE_PCT,
        "total_big_moves": len(caught) + len(missed),
        "caught_count": len(caught),
        "missed_count": len(missed),
        "caught": caught,
        "missed": missed,
    }


# ----------------------------------------------------------------------
# 3) 止损松紧分析
# ----------------------------------------------------------------------

def _stop_analysis(signals: list) -> dict:
    stopped = [r for r in signals if r["filled"] and r["hit_stop"]]
    n = len(stopped)
    tight = [r for r in stopped
             if r["atr"] and abs(r["trigger_px"] - r["stop_px"]) < r["atr"]]
    had_05 = [r for r in stopped if (r["mfe_r"] or 0) > 0.5]
    had_1 = [r for r in stopped if (r["mfe_r"] or 0) > 1.0]
    return {
        "hit_stop_count": n,
        "tighter_than_1atr_count": len(tight),
        "tighter_than_1atr_pct": round(len(tight) / n * 100, 1) if n else 0.0,
        "had_05r_count": len(had_05),          # 曾浮盈 >0.5R 后被打回止损
        "had_05r_pct": round(len(had_05) / n * 100, 1) if n else 0.0,
        "had_1r_count": len(had_1),            # 曾浮盈 >1R 后被打回止损
        "had_1r_pct": round(len(had_1) / n * 100, 1) if n else 0.0,
    }


# ----------------------------------------------------------------------
# 报告主入口
# ----------------------------------------------------------------------

def generate_report(day: str, db_path: Optional[Path] = None) -> dict:
    """生成某日影子复盘报告（day = YYYY-MM-DD ET）。"""
    db = Path(db_path) if db_path else research_db.DEFAULT_DB
    signals = research_db.shadow_load_day(day, db_path=db)
    return {
        "day": day,
        "generated_at": datetime.now(ET).isoformat(timespec="seconds"),
        "total_signals": len(signals),
        "groups": _groups(signals),
        "breakout_crowd": _breakout_crowd_split(signals),
        "big_moves": _big_moves(day, signals, db),
        "stop_analysis": _stop_analysis(signals),
    }


def summary_lines(report: dict) -> list:
    """iMessage 摘要：各类型×方向一行一条（胜率/期望值）。"""
    lines = [f"复盘 {report['day']}：影子单 {report['total_signals']} 笔"]
    for g in report["groups"]:
        lines.append(f"{g['type']} {'多' if g['side'] == 'long' else '空'} "
                     f"{g['count']}笔 胜率{g['win_rate']}% 期望{g['expectancy']}R")
    bm = report["big_moves"]
    lines.append(f"大行情(≥{bm['threshold_pct']}%) {bm['total_big_moves']} 只："
                 f"抓住 {bm['caught_count']} / 漏掉 {bm['missed_count']}")
    sa = report["stop_analysis"]
    lines.append(f"止损 {sa['hit_stop_count']} 笔：紧于1ATR "
                 f"{sa['tighter_than_1atr_pct']}%，曾赚>1R被打回 "
                 f"{sa['had_1r_pct']}%")
    return lines
