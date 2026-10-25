# -*- coding: utf-8 -*-
"""金标准：盘前突破信号对拍回放 + 构造单测。

守的口径：BreakoutTracker 流式输出必须与 research_breakout.analyze_day
（批量基准，窗口 09:45–10:30 ET，min_hm=585，confirm_hm<=630，多空双出，
拥挤计数多空混算）在 10-13/19/20/21/22/23 六天逐条一致，注入/不注入
权威盘前区间（set_premarket）两种模式都必须一致。
logs/research.db 缺失时 skip（只读，绝不写入）。
"""
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

import research_db
from engine.breakout_tracker import BreakoutTracker, _hm as _thm

ET = ZoneInfo("America/New_York")
DB = Path(__file__).resolve().parent.parent / "logs" / "research.db"
DAYS = ["2026-10-13", "2026-10-19", "2026-10-20",
        "2026-10-21", "2026-10-22", "2026-10-23"]

KEYS = ("symbol", "ticker", "side", "confirm_hm", "confirm_time",
        "break_time", "close", "premarket_high", "premarket_low",
        "zone_width", "crowd", "triple")

_needs_db = pytest.mark.skipif(not DB.exists(),
                               reason="logs/research.db 缺失，跳过回放金标准")


def replay_day(day: str, inject: bool = False) -> list:
    """从 research_db 读全天 bars，按 time 排序逐根喂 tracker（每根 bar
    对所有 symbol 喂完后 flush）。inject=True 模拟「5M 进攻区域」指标：
    喂 bar 前用库内盘前数据（hm<570 高低点）调 set_premarket。"""
    per_sym = {}
    times = set()
    for sym in research_db.symbols():
        bars = research_db.load_day(sym, day)
        if not bars:
            continue
        idx = {}
        for b in bars:
            idx[b["time"]] = {
                "time": b["time"], "open": b["open"], "high": b["high"],
                "low": b["low"], "close": b["close"],
                "fvg_event": b.get("fvg_event") or 0,
            }
        per_sym[sym] = idx
        times.update(idx)
    tracker = BreakoutTracker()
    if inject:
        for sym, idx in per_sym.items():
            pre = [b for b in idx.values() if _thm(b["time"]) < 570]
            if pre:
                tracker.set_premarket(sym, max(b["high"] for b in pre),
                                      min(b["low"] for b in pre), day)
    out = []
    for t in sorted(times):
        for sym in sorted(per_sym):
            bar = per_sym[sym].get(t)
            if bar is None:
                continue
            sig = tracker.on_bar(sym, bar)
            if sig:
                out.append(sig)
        out.extend(tracker.flush(t))
    out.sort(key=lambda r: (r["confirm_hm"], r["ticker"]))
    return out


def batch_day(day: str, min_hm: int = 585, max_hm: int = 630) -> list:
    """批量基准：analyze_day + main() 的 max-hm 过滤，side=both 多空全量。"""
    import research_breakout
    rows = research_breakout.analyze_day(day, min_hm)
    return [r for r in rows if r["confirm_hm"] <= max_hm]


@_needs_db
@pytest.mark.parametrize("day", DAYS)
@pytest.mark.parametrize("inject", [False, True])
def test_replay_matches_batch(day, inject):
    """六天 × 注入双模式：tracker 流式输出与批量基准逐条一致。"""
    got = [{k: r.get(k) for k in KEYS} for r in replay_day(day, inject=inject)]
    want = [{k: r.get(k) for k in KEYS} for r in batch_day(day)]
    assert got == want, (
        f"{day} inject={inject} 不一致:\n"
        f"  tracker={got}\n  batch  ={want}")


# ----------------------------------------------------------------------
# 构造数据单测（合成 bar 序列，不依赖 research.db）
# ----------------------------------------------------------------------

def _ts(day: str, hm: int) -> int:
    d = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=ET)
    d += timedelta(minutes=hm)
    return int(d.timestamp())


def _bar(day: str, hm: int, o, h, l, c, fvg=0) -> dict:
    return {"time": _ts(day, hm), "open": o, "high": h, "low": l,
            "close": c, "fvg_event": fvg}


def feed(tracker, seqs):
    """seqs: {symbol: [bar,...]}；按 bar 时间逐拍喂入并 flush，收集信号。"""
    times = sorted({b["time"] for bars in seqs.values() for b in bars})
    out = []
    for t in times:
        for sym in sorted(seqs):
            for b in seqs[sym]:
                if b["time"] == t:
                    sig = tracker.on_bar(sym, b)
                    if sig:
                        out.append(sig)
        out.extend(tracker.flush(t))
    return out


def test_premarket_lock():
    """盘前累积与锁定：区间由 04:00–09:25 高低点决定，锁定后突破出信号。"""
    tr = BreakoutTracker()
    out = feed(tr, {"NASDAQ:TST": [
        _bar("2026-10-26", 240, 100, 101, 99, 100),      # 04:00 盘前
        _bar("2026-10-26", 545, 100, 102, 98, 100),      # 09:05 盘前
        _bar("2026-10-26", 570, 100, 101, 99, 100),      # 09:30 锁定 pmh102/pml98
        _bar("2026-10-26", 585, 100, 103, 99, 102.5),    # 09:45 越界 102.5>102
        _bar("2026-10-26", 590, 102, 104, 101.5, 103, 1),  # FVG+阳线确认
    ]})
    assert len(out) == 1, out
    s = out[0]
    assert s["premarket_high"] == 102 and s["premarket_low"] == 98
    assert s["confirm_time"] == "09:50" and s["break_time"] == "09:45"
    assert s["crowd"] == 1 and s["triple"] is False


def test_no_break_before_585():
    """09:30–09:40 的 bar 只更新 prev_close，不产生候选（窗口下沿门控）。"""
    tr = BreakoutTracker()
    out = feed(tr, {"NASDAQ:TST": [
        _bar("2026-10-26", 540, 100, 101, 99, 100),
        _bar("2026-10-26", 570, 100, 101, 99, 100),      # 锁定
        _bar("2026-10-26", 575, 100, 103, 99, 102.5),    # 越界但 <585 → 无候选
        _bar("2026-10-26", 580, 102, 104, 101.5, 103, 1),  # FVG 也不该有信号
        _bar("2026-10-26", 585, 103, 104, 100.5, 101),   # 收回区间内（pmh=102）
        _bar("2026-10-26", 590, 101, 103, 100, 102.5),   # 585 后再次越界 → 候选
        _bar("2026-10-26", 595, 102.5, 104, 101, 103, 1),  # FVG 确认
    ]})
    assert len(out) == 1, out
    assert out[0]["break_time"] == "09:50" and out[0]["confirm_time"] == "09:55"


def test_recross_new_candidate():
    """越界后收回再越界 = 新候选（两次都出信号）。"""
    tr = BreakoutTracker()
    out = feed(tr, {"NASDAQ:TST": [
        _bar("2026-10-26", 540, 100, 101, 99, 100),
        _bar("2026-10-26", 570, 100, 101, 99, 100),
        _bar("2026-10-26", 585, 100, 102, 99, 101.5),    # 第一次越界
        _bar("2026-10-26", 590, 101.5, 102, 100, 101.8, 1),  # FVG确认#1
        _bar("2026-10-26", 595, 101.8, 102, 99.5, 100),  # 收回区间内
        _bar("2026-10-26", 600, 100, 102, 99, 101.6),    # 再次越界
        _bar("2026-10-26", 605, 101.6, 102, 100, 101.9, 1),  # FVG确认#2
    ]})
    assert len(out) == 2, out
    assert out[0]["confirm_time"] == "09:50" and out[1]["confirm_time"] == "10:05"


def test_no_fvg_discard():
    """下一根无同向 FVG / 非同向 K 线 → 候选作废。"""
    tr = BreakoutTracker()
    bars = [
        _bar("2026-10-26", 540, 100, 101, 99, 100),
        _bar("2026-10-26", 570, 100, 101, 99, 100),
        _bar("2026-10-26", 585, 100, 102, 99, 101.5),
        _bar("2026-10-26", 590, 101.5, 102, 100, 101.8, 0),  # 无 FVG
    ]
    assert not feed(tr, {"NASDAQ:TST": bars})
    tr = BreakoutTracker()
    bars[-1]["fvg_event"] = 1
    bars[-1]["open"] = 102
    bars[-1]["close"] = 101.2   # fvg=1 但阴线 → 作废
    assert not feed(tr, {"NASDAQ:TST": bars})


def test_crowd_triple_upgrade():
    """同确认时刻 4 只 > 3 → 升级三连阳；三连不成立的作废，确认时刻延后。"""
    tr = BreakoutTracker()
    seqs = {}
    for t in ("AAA", "BBB", "CCC", "DDD"):
        bars = [
            _bar("2026-10-26", 540, 100, 101, 99, 100),
            _bar("2026-10-26", 570, 100, 101, 99, 100),
            _bar("2026-10-26", 585, 100, 102, 99, 101.5),      # 同时越界
            _bar("2026-10-26", 590, 101.5, 102.5, 100, 101.8, 1),  # 同时确认
        ]
        if t != "DDD":
            bars.append(_bar("2026-10-26", 595, 101.8, 103, 101, 102.5))
        else:
            bars.append(_bar("2026-10-26", 595, 102.5, 103, 100.5, 101.2))  # 阴线作废
        seqs[f"NASDAQ:{t}"] = bars
    out = feed(tr, seqs)
    assert len(out) == 3, out
    for s in out:
        assert s["triple"] is True and s["crowd"] == 4
        assert s["confirm_time"] == "09:55"   # 延后到第三根
    assert {s["ticker"] for s in out} == {"AAA", "BBB", "CCC"}


def test_short_signal():
    """跌破低点 + 同向 FVG(-1) + 阴线 → 空头信号（字段结构同多头）。"""
    tr = BreakoutTracker()
    out = feed(tr, {"NASDAQ:TST": [
        _bar("2026-10-26", 540, 100, 101, 99, 100),       # pmh=101 pml=99
        _bar("2026-10-26", 570, 100, 101, 99, 100),
        _bar("2026-10-26", 585, 100, 100.5, 98.5, 98.8),  # 跌破 98.8<99
        _bar("2026-10-26", 590, 98.8, 99, 97.5, 98, -1),  # FVG(-1)+阴线确认
    ]})
    assert len(out) == 1, out
    s = out[0]
    assert s["side"] == "跌破低点" and s["triple"] is False
    assert s["premarket_high"] == 101 and s["premarket_low"] == 99
    assert s["confirm_time"] == "09:50" and s["break_time"] == "09:45"


def test_short_crowd_triple():
    """空头拥挤升级：>3 → 三连阴镜像（三根全阴且低点依次降低）才出信号。"""
    tr = BreakoutTracker()
    seqs = {}
    for t in ("AAA", "BBB", "CCC", "DDD"):
        bars = [
            _bar("2026-10-26", 540, 100, 101, 99, 100),
            _bar("2026-10-26", 570, 100, 101, 99, 100),
            _bar("2026-10-26", 585, 100, 100.5, 98.5, 98.8),   # 同时跌破
            _bar("2026-10-26", 590, 98.8, 99, 97.5, 98, -1),   # 同时确认
        ]
        if t != "DDD":
            bars.append(_bar("2026-10-26", 595, 98, 98.5, 97.0, 97.3))  # 三连阴
        else:
            bars.append(_bar("2026-10-26", 595, 97, 99, 96.5, 98))  # 阳线作废
        seqs[f"NASDAQ:{t}"] = bars
    out = feed(tr, seqs)
    assert len(out) == 3, out
    for s in out:
        assert s["side"] == "跌破低点" and s["triple"] is True
        assert s["crowd"] == 4 and s["confirm_time"] == "09:55"
    assert {s["ticker"] for s in out} == {"AAA", "BBB", "CCC"}


def test_mixed_crowd_counts_both_sides():
    """拥挤计数多空混算（对拍批量 Counter 的关键口径）：同确认时刻
    2 多 + 2 空 = 4 >3 → 全部升级三连确认（不多空分开数）。"""
    tr = BreakoutTracker()
    seqs = {}
    for t in ("LA", "LB"):
        seqs[f"NASDAQ:{t}"] = [
            _bar("2026-10-26", 540, 100, 101, 99, 100),
            _bar("2026-10-26", 570, 100, 101, 99, 100),
            _bar("2026-10-26", 585, 100, 102, 99, 101.5),
            _bar("2026-10-26", 590, 101.5, 102.5, 100, 101.8, 1),
            _bar("2026-10-26", 595, 101.8, 103, 101, 102.5),   # 三连阳成立
        ]
    seqs["NASDAQ:SC"] = [
        _bar("2026-10-26", 540, 100, 101, 99, 100),
        _bar("2026-10-26", 570, 100, 101, 99, 100),
        _bar("2026-10-26", 585, 100, 100.5, 98.5, 98.8),
        _bar("2026-10-26", 590, 98.8, 99, 97.5, 98, -1),
        _bar("2026-10-26", 595, 98, 98.5, 97.0, 97.3),         # 三连阴成立
    ]
    seqs["NASDAQ:SD"] = [
        _bar("2026-10-26", 540, 100, 101, 99, 100),
        _bar("2026-10-26", 570, 100, 101, 99, 100),
        _bar("2026-10-26", 585, 100, 100.5, 98.5, 98.8),
        _bar("2026-10-26", 590, 98.8, 99, 97.5, 98, -1),
        _bar("2026-10-26", 595, 97, 99, 96.5, 98),             # 阳线作废
    ]
    out = feed(tr, seqs)
    assert len(out) == 3, out   # 若多空分开数（各2只≤3）会出4条立即信号
    for s in out:
        assert s["triple"] is True and s["crowd"] == 4
    assert {s["ticker"] for s in out} == {"LA", "LB", "SC"}


def test_confirm_after_1030_dropped():
    """确认时刻 >10:30 ET 丢弃（10:30 边界保留；含三连延后超限）。"""
    tr = BreakoutTracker()
    bars = [
        _bar("2026-10-26", 540, 100, 101, 99, 100),
        _bar("2026-10-26", 570, 100, 101, 99, 100),
        _bar("2026-10-26", 625, 100, 102, 99, 101.5),     # 10:25 越界
        _bar("2026-10-26", 630, 101.5, 102, 100, 101.8, 1),  # 10:30 确认 OK
    ]
    out = feed(tr, {"NASDAQ:TST": bars})
    assert len(out) == 1 and out[0]["confirm_time"] == "10:30", out
    tr = BreakoutTracker()
    bars[2] = _bar("2026-10-26", 630, 100, 102, 99, 101.5)
    bars[3] = _bar("2026-10-26", 635, 101.5, 102, 100, 101.8, 1)
    assert not feed(tr, {"NASDAQ:TST": bars})   # 10:35 确认 → 丢弃


def test_inject_priority():
    """注入优先于自累积：注入值与自累积不同，信号按注入区间判定。"""
    tr = BreakoutTracker()
    sym = "NASDAQ:TST"
    assert tr.set_premarket(sym, 210.0, 200.0, "2026-10-26") is True
    out = feed(tr, {sym: [
        _bar("2026-10-26", 540, 100, 101, 99, 100),       # 盘前（自累积被跳过）
        _bar("2026-10-26", 570, 100, 101, 99, 100),
        _bar("2026-10-26", 585, 100, 211, 99, 210.5),     # >210 越界（按注入值）
        _bar("2026-10-26", 590, 210.5, 212, 209, 211, 1),  # FVG+阳线确认
    ]})
    assert len(out) == 1, out
    assert out[0]["premarket_high"] == 210 and out[0]["premarket_low"] == 200


def test_inject_invalid_fallback():
    """注入无效值（None / pmh<=pml / pml<=0）→ 返回 False，回落自累积。"""
    for bad in ((None, 100.0), (100.0, None), (100.0, 100.0), (99.0, 100.0),
                (100.0, 0.0), (100.0, -1.0)):
        tr = BreakoutTracker()
        assert tr.set_premarket("NASDAQ:TST", bad[0], bad[1], "2026-10-26") is False, bad
        out = feed(tr, {"NASDAQ:TST": [
            _bar("2026-10-26", 540, 100, 101, 99, 100),
            _bar("2026-10-26", 570, 100, 101, 99, 100),
            _bar("2026-10-26", 585, 100, 102, 99, 101.5),
            _bar("2026-10-26", 590, 101.5, 102.5, 100, 101.8, 1),
        ]})
        assert len(out) == 1 and out[0]["premarket_high"] == 101, (bad, out)


def test_inject_after_lock_restart():
    """盘中重启场景：控制台 09:50 才起来（无盘前 bar），锁定时无累积→当日
    不参与；attack_c 对齐后注入 → 改按注入区间出信号。"""
    tr = BreakoutTracker()
    sym = "NASDAQ:TST"
    out = feed(tr, {sym: [
        _bar("2026-10-26", 570, 100, 101, 99, 100),       # 重启后首根，无盘前累积
        _bar("2026-10-26", 585, 100, 101.5, 99, 101),
    ]})
    assert not out   # 未注入：锁定 pmh=None → 当日不参与
    assert tr.set_premarket(sym, 101.0, 99.0, "2026-10-26") is True
    out = feed(tr, {sym: [
        _bar("2026-10-26", 590, 101, 102, 99.5, 101.5),   # >101 越界
        _bar("2026-10-26", 595, 101.5, 102.5, 100, 101.8, 1),
    ]})
    assert len(out) == 1 and out[0]["premarket_high"] == 101, out
