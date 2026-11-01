# -*- coding: utf-8 -*-
"""console 突破接线链回归（console/app.py _breakout_loop 的核心路径）。

守的口径：伪造 daemon 快照（{bar_c, fvg_c, attack_c}）逐拍推进 2026-10-23
全天（research.db 只读，缺失即 skip），经 _feed_breakout + flush 产出的
信号必须精确等于当日 9 条（多空双出：4 多 + 5 空，crowd 多空混算），且
进攻区域注入路径实际走通。不依赖 8000 端口活着（只调函数，不起服务）。
"""
import datetime
from pathlib import Path

import pytest

import research_db
from engine.breakout_tracker import BreakoutTracker, _hm as _thm

DAY = "2026-10-23"

_DB = Path(__file__).resolve().parent.parent / "logs" / "research.db"
pytestmark = pytest.mark.skipif(not _DB.exists(),
                                reason="logs/research.db 缺失，跳过接线回放")

# (symbol, side, confirm, break, close, pmh, pml)——按 confirm_hm+ticker 排序
EXPECTED = [
    ("NASDAQ:CRWD", "站上高点", "09:50", "09:45", 240.63, 240.48, 237.34),
    ("NASDAQ:PANW", "站上高点", "09:50", "09:45", 358.39, 356.97, 350.93),
    ("NASDAQ:TSLA", "跌破低点", "09:50", "09:45", 369.52, 380.50, 372.00),
    ("NASDAQ:MSFT", "站上高点", "09:55", "09:50", 520.25, 520.00, 516.40),
    ("NASDAQ:MU", "站上高点", "10:00", "09:55", 885.51, 885.00, 860.00),
    ("NYSE:UBER", "跌破低点", "10:05", "10:00", 68.18, 68.84, 68.38),
    ("AMEX:XLB", "跌破低点", "10:05", "10:00", 51.17, 51.64, 51.34),
    ("NASDAQ:COIN", "跌破低点", "10:10", "10:05", 167.62, 171.62, 168.00),
    ("NASDAQ:PLTR", "跌破低点", "10:20", "10:15", 193.97, 196.68, 194.54),
]


@pytest.fixture
def app(console_app, monkeypatch):
    """console.app + 突破子系统状态全部换成临时实例（结束自动还原）。"""
    import collections
    monkeypatch.setattr(console_app, "breakout_tracker", BreakoutTracker())
    monkeypatch.setattr(console_app.signals, "_breakout_last_bar", {})
    monkeypatch.setattr(console_app.signals, "_breakout_pending", {})
    monkeypatch.setattr(console_app.signals, "_breakout_injected", {})
    monkeypatch.setattr(console_app.signals, "_breakout_signals", collections.deque())
    return console_app


def test_breakout_wiring_end_to_end(app):
    """伪造快照逐拍驱动 console 接线（含 attack_c 注入），产出精确 9 条。"""
    # 伪造 market：sym -> (bars, pmh, pml) 逐拍推进指针
    # attack_c = 进攻区域指标模拟：库内盘前高低点，每根 bar 都 plot 同组值
    syms = {}
    times = set()
    for sym in research_db.symbols():
        bars = research_db.load_day(sym, DAY)
        if not bars:
            continue
        pre = [b for b in bars if _thm(b["time"]) < 570]
        pmh = max(b["high"] for b in pre) if pre else None
        pml = min(b["low"] for b in pre) if pre else None
        syms[sym] = (bars, pmh, pml)
        times.update(b["time"] for b in bars)

    for sym in syms:   # 首次建基线（模拟 loop 里 prev is None）
        app.signals._breakout_last_bar[sym] = -1

    from zoneinfo import ZoneInfo
    et = ZoneInfo("America/New_York")
    for t in sorted(times):
        market = {}
        for sym, (bars, pmh, pml) in syms.items():
            upto = [b for b in bars if b["time"] <= t]
            if not upto:
                continue
            bar_c = upto[-1]
            market[sym] = {
                "bar_c": {"time": bar_c["time"], "open": bar_c["open"],
                          "high": bar_c["high"], "low": bar_c["low"],
                          "close": bar_c["close"]},
                "fvg_c": {"time": bar_c["time"],
                          "fvg_event": bar_c.get("fvg_event") or 0},
                "attack_c": {"time": bar_c["time"], "pmh": pmh, "pml": pml},
            }
        # ---- 复刻 _breakout_loop 单拍（注入 + pending 对齐 + feed + flush）----
        fed_times = set()
        for sym, data in market.items():
            bar_c = data.get("bar_c") or {}
            bt = bar_c.get("time") or 0
            if not bt:
                continue
            atk = data.get("attack_c") or {}
            pmh, pml = atk.get("pmh"), atk.get("pml")
            dt_et = datetime.datetime.fromtimestamp(bt, et)
            if (atk.get("time") == bt and pmh is not None
                    and pml is not None and pmh > pml > 0
                    and dt_et.hour * 60 + dt_et.minute >= 570):
                day = dt_et.strftime("%Y-%m-%d")
                if app.signals._breakout_injected.get(sym) != day:
                    if app.breakout_tracker.set_premarket(sym, pmh, pml, day):
                        app.signals._breakout_injected[sym] = day
            pend = app.signals._breakout_pending.get(sym)
            if pend is not None:
                p_bt, p_bar, retries = pend
                fvg_c = data.get("fvg_c") or {}
                if fvg_c.get("time") == p_bt:
                    app.signals._breakout_pending.pop(sym, None)
                    app.signals._feed_breakout(sym, p_bar, data, fed_times)
                elif retries <= 0:
                    app.signals._breakout_pending.pop(sym, None)
                else:
                    app.signals._breakout_pending[sym] = (p_bt, p_bar, retries - 1)
            prev = app.signals._breakout_last_bar.get(sym)
            if prev is None:
                app.signals._breakout_last_bar[sym] = bt
                continue
            if bt <= prev:
                continue
            app.signals._breakout_last_bar[sym] = bt
            fvg_c = data.get("fvg_c") or {}
            fvg_t = fvg_c.get("time")
            if fvg_t is not None and fvg_t != bt:
                app.signals._breakout_pending[sym] = (bt, dict(bar_c), 5)
                continue
            app.signals._feed_breakout(sym, bar_c, data, fed_times)
        for ft in sorted(fed_times):
            for sig in app.breakout_tracker.flush(ft):
                app.signals._emit_breakout(sig)

    sigs = sorted(app.signals._breakout_signals,
                  key=lambda s: (s["confirm_hm"], s["ticker"]))
    assert len(sigs) == len(EXPECTED), [s["symbol"] for s in sigs]
    for s, (sym, side, ctm, btm, close, pmh, pml) in zip(sigs, EXPECTED):
        assert s["symbol"] == sym and s["side"] == side
        assert (s["confirm_time"], s["break_time"]) == (ctm, btm)
        assert s["close"] == close
        assert s["premarket_high"] == pmh and s["premarket_low"] == pml
    # 注入路径确实走通（有 symbol 被注入过）
    assert app.signals._breakout_injected, "attack_c 注入未发生"
    # 信号 id/ts 出口字段完整（前端信号栏依赖）
    for s in sigs:
        assert s["id"].endswith(":breakout") and s["ts"] > 0
