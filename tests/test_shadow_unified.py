# -*- coding: utf-8 -*-
"""统一影子跟踪器（engine/shadow_tracker.py）回归。

守的口径：每类信号单独建档（字段完整：类型/方向/触发价/smart_stop 止损
位/ATR/bar 时间/meta）；市价型立即成交、限价型触及才成交；MFE/MAE/R 数学
（R=|入场−止损|，止损缺失回退 1×ATR）；四种结局 hit_stop/reached_2R/
eod_flat/unfilled；SQLite 持久化重载恢复；跨天（bar 日期前滚）强制结算
不隔夜；现有影子 tab 兼容（stats 原有键 + events 原有字段 + pullback 自检
行为不变）。SQLite 全部走 tmp_path 库，绝不写 logs/research.db。
"""
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

import research_db
from engine.shadow_tracker import ShadowTracker, SIGNAL_TYPES, OUTCOMES

ET = ZoneInfo("America/New_York")
DAY = "2026-10-23"   # 周五（真实交易日语义）


def _ts(day: str, hm: int) -> int:
    d = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=ET)
    d += timedelta(minutes=hm)
    return int(d.timestamp())


def _bar(hm: int, o, h, l, c, day: str = DAY) -> dict:
    return {"time": _ts(day, hm), "open": o, "high": h, "low": l, "close": c}


def _mkt(sym, bar, ts_c=None):
    return {sym: {"bar_c": bar, "ts_c": ts_c or {}}}


@pytest.fixture
def tracker():
    return ShadowTracker(persist=False)   # 纯内存，不写库


# ----------------------------------------------------------------------
# ① 各类型建档字段完整
# ----------------------------------------------------------------------

def test_record_fields_all_types(tracker):
    """六类信号建档：字段完整（类型/方向/触发价/止损/ATR/bar时间/meta）。"""
    bt = _ts(DAY, 600)   # 10:00 ET
    for i, st in enumerate(SIGNAL_TYPES):
        tr = tracker.record(
            st, "NASDAQ:TST", "long", trigger_px=100.0 + i, stop_px=99.0 + i,
            atr=2.0, bar_time=bt + i,
            meta={"crowd": 3, "note": st},
            fill_mode="limit" if st in ("proposal", "fvg_retest") else "market",
            limit_px=100.0 + i if st in ("proposal", "fvg_retest") else 0.0,
            dedup_key=f"{st}:{i}")
        assert tr is not None, st
        assert tr.signal_type == st and tr.side == "long"
        assert tr.atr == 2.0 and tr.bar_time == bt + i
        assert tr.meta["crowd"] == 3 and tr.meta["note"] == st
        if st in ("proposal", "fvg_retest"):
            assert tr.fill_mode == "limit" and not tr.filled
            assert tr.limit_px == 100.0 + i == tr.entry
        else:
            assert tr.fill_mode == "market" and tr.filled
            assert tr.fill_px == 100.0 + i == tr.entry
        # 止损方向正确（多头止损在入场下方）
        assert tr.stop < tr.entry
    assert len(tracker.events()) == len(SIGNAL_TYPES)


def test_record_dedup_and_stop_fallback(tracker):
    """去重（同 key 不重复建档）；止损缺失/非法回退 1×ATR；非法方向/类型拒绝。"""
    bt = _ts(DAY, 600)
    tr = tracker.record("fvg_event", "NASDAQ:TST", "long", 100.0, 0.0, 2.0,
                        bt, dedup_key="k1")
    assert tr.stop == 98.0                      # 1×ATR 兜底
    assert tracker.record("fvg_event", "NASDAQ:TST", "long", 100.0, 98.0,
                          2.0, bt, dedup_key="k1") is None   # 去重
    # 多头止损在入场上方（非法）→ 也回退 1×ATR
    tr2 = tracker.record("fvg_event", "NASDAQ:TST", "long", 100.0, 101.0,
                         2.0, bt + 300, dedup_key="k2")
    assert tr2.stop == 98.0
    with pytest.raises(AssertionError):
        tracker.record("nope", "NASDAQ:TST", "long", 100.0, 99.0, 2.0, bt)
    with pytest.raises(AssertionError):
        tracker.record("fvg_event", "NASDAQ:TST", "up", 100.0, 99.0, 2.0, bt)


# ----------------------------------------------------------------------
# ② 市价立即成交 / 限价触及才成交；③ MFE/MAE/R 数学
# ----------------------------------------------------------------------

def test_limit_fills_only_on_touch(tracker):
    """限价型：未触及不成交（等成交状态）；触及当根按挂单价成交并开始推演。"""
    bt = _ts(DAY, 600)
    tr = tracker.record("proposal", "NASDAQ:TST", "long", 0.0, 0.0, 1.0, bt,
                        fill_mode="limit", limit_px=100.0, dedup_key="p1")
    assert tr and not tr.filled
    # 10:05 未触及（low 100.5 > 100）
    ev, st = tracker.process(_mkt("NASDAQ:TST", _bar(605, 101, 102, 100.5, 101.5)))
    assert not tr.filled and not tr.settled
    # 10:10 触及（low 99.5 <= 100）→ 按 100 成交（成交当根不推演，与市价单
    # 「信号 bar 不推演」同口径）
    ev, st = tracker.process(_mkt("NASDAQ:TST", _bar(610, 101.5, 102, 99.5, 101)))
    assert tr.filled and tr.fill_px == 100.0 and tr.fill_time == _ts(DAY, 610)
    assert tr.mfe_rr == 0.0
    # 10:15 起推演：R=|100−99|=1（止损回退 1×ATR=99）；high 102 → MFE 2R，
    # low 99.5 → MAE 0.5R
    tracker.process(_mkt("NASDAQ:TST", _bar(615, 101, 102, 99.5, 101.5)))
    assert tr.mfe_rr == 2.0 and tr.mae_rr == 0.5


def test_mfe_mae_r_math_long(tracker):
    """MFE/MAE/R 数学（多头）：R=|入场−止损|；止损前口径止损后冻结，
    收盘前口径继续跟踪。"""
    bt = _ts(DAY, 600)
    tr = tracker.record("fvg_event", "NASDAQ:TST", "long", 100.0, 98.0, 2.0,
                        bt, dedup_key="m1")
    assert tr.filled and tr.fill_time == bt   # 市价立即成交
    # 信号 bar 本身不推演（下一根才开始）
    tracker.process(_mkt("NASDAQ:TST", _bar(600, 100, 105, 99, 104)))
    assert tr.mfe_rr == 0.0
    # 10:05：high 103 → MFE (103-100)/2=1.5R；low 99 → MAE 0.5R；到 1R 未到 2R
    tracker.process(_mkt("NASDAQ:TST", _bar(605, 104, 103, 99, 102)))
    assert tr.mfe_rr == 1.5 and tr.mae_rr == 0.5
    assert tr.reached_1r and not tr.reached_2r
    # 10:10：high 105 → 2.5R（到 2R）
    tracker.process(_mkt("NASDAQ:TST", _bar(610, 102, 105, 101.5, 104)))
    assert tr.mfe_rr == 2.5 and tr.reached_2r
    # 10:15：触止损 low 97.5 ≤ 98 → stopped=-1R，止损前口径冻结在 2.5R
    tracker.process(_mkt("NASDAQ:TST", _bar(615, 104, 104.5, 97.5, 98.5)))
    assert tr.stopped and tr.outcome_rr == -1.0 and tr.mfe_rr == 2.5
    # 10:20：止损后收盘前口径仍跟踪（high 106 → mfe_eod 3R）
    tracker.process(_mkt("NASDAQ:TST", _bar(620, 98.5, 106, 98, 105)))
    assert tr.mfe_rr == 2.5 and tr.mfe_rr_eod == 3.0
    # EOD 15:55 结算：hit_stop 优先
    ev, st = tracker.process(_mkt("NASDAQ:TST", _bar(955, 105, 106, 104, 105)))
    assert st == [tr] and tr.outcome == "hit_stop" and tr.outcome_rr == -1.0


def test_short_mirror(tracker):
    """空头镜像：MFE 看 low、MAE 看 high、止损 high>=stop。"""
    bt = _ts(DAY, 600)
    tr = tracker.record("breakout", "NASDAQ:TST", "short", 100.0, 102.0, 2.0,
                        bt, dedup_key="s1")
    tracker.process(_mkt("NASDAQ:TST", _bar(605, 99, 100.5, 97, 98)))
    assert tr.mfe_rr == 1.5 and tr.mae_rr == 0.25   # low 97→1.5R；high 100.5→0.25R
    tracker.process(_mkt("NASDAQ:TST", _bar(610, 98, 102.5, 97.5, 102)))
    assert tr.stopped and tr.outcome_rr == -1.0     # high 102.5 ≥ 102


# ----------------------------------------------------------------------
# ④ 四种结局
# ----------------------------------------------------------------------

def test_outcome_reached_2r(tracker):
    """reached_2R：到过 2R 未止损，EOD 按收盘价结算但结局记 reached_2R。"""
    bt = _ts(DAY, 600)
    tr = tracker.record("fvg_event", "NASDAQ:TST", "long", 100.0, 98.0, 2.0,
                        bt, dedup_key="r2")
    tracker.process(_mkt("NASDAQ:TST", _bar(605, 100, 105, 99.5, 104)))  # 2.5R
    ev, st = tracker.process(_mkt("NASDAQ:TST", _bar(955, 104, 104.5, 102, 103)))
    assert st == [tr]
    assert tr.outcome == "reached_2R"
    assert tr.outcome_rr == 1.5          # 结算 RR 按收盘 (103-100)/2
    assert tr.reached_2r and not tr.stopped


def test_outcome_eod_flat(tracker):
    """eod_flat：未到 2R 未止损，15:55 按收盘价了结（跨天不隔夜）。"""
    bt = _ts(DAY, 600)
    tr = tracker.record("fvg_warn", "NASDAQ:TST", "long", 100.0, 98.0, 2.0,
                        bt, dedup_key="e1")
    ev, st = tracker.process(_mkt("NASDAQ:TST", _bar(955, 100, 101, 99, 100.5)))
    assert st == [tr] and tr.outcome == "eod_flat"
    assert tr.outcome_rr == 0.25         # (100.5-100)/2


def test_outcome_unfilled(tracker):
    """unfilled：限价挂单纯未触及，EOD 结算 outcome_r=0。"""
    bt = _ts(DAY, 600)
    tr = tracker.record("proposal", "NASDAQ:TST", "long", 0.0, 0.0, 1.0, bt,
                        fill_mode="limit", limit_px=95.0, dedup_key="u1")
    tracker.process(_mkt("NASDAQ:TST", _bar(605, 100, 101, 99, 100)))
    tracker.process(_mkt("NASDAQ:TST", _bar(610, 100, 101, 99, 100)))
    ev, st = tracker.process(_mkt("NASDAQ:TST", _bar(955, 100, 101, 99, 100)))
    assert st == [tr] and not tr.filled
    assert tr.outcome == "unfilled" and tr.outcome_rr == 0.0


# ----------------------------------------------------------------------
# ⑤ SQLite 持久化重载恢复
# ----------------------------------------------------------------------

def test_sqlite_persist_and_reload(tmp_path):
    """建档/逐 bar 更新/定稿全部落库（tmp 库）；新实例重载恢复未结算单
    继续推演（重启不丢），已定稿的不重复结算。"""
    db = tmp_path / "research.db"
    tr1 = ShadowTracker(persist=True, db_path=db)
    bt = _ts(DAY, 600)
    tr = tr1.record("breakout", "NASDAQ:TST", "long", 100.0, 98.0, 2.0, bt,
                    meta={"crowd": 3, "triple": False}, dedup_key="b1")
    tr1.process(_mkt("NASDAQ:TST", _bar(605, 100, 103, 99, 102)))
    rows = research_db.shadow_load_day(DAY, db_path=db)
    assert len(rows) == 1
    r = rows[0]
    assert r["type"] == "breakout" and r["symbol"] == "NASDAQ:TST"
    assert r["filled"] == 1 and r["mfe_r"] == 1.5 and r["settled"] == 0
    assert '"crowd": 3' in r["meta"]
    # 模拟重启：新实例恢复（load_day 指定回放日），继续推演到 EOD 定稿
    tr2 = ShadowTracker(persist=True, db_path=db, load_day=DAY)
    assert len(tr2.events()) == 1
    ev, st = tr2.process(_mkt("NASDAQ:TST", _bar(955, 102, 102.5, 100.5, 101)))
    assert len(st) == 1 and st[0].outcome == "eod_flat"
    rows = research_db.shadow_load_day(DAY, db_path=db)
    assert rows[0]["settled"] == 1 and rows[0]["outcome"] == "eod_flat"
    assert rows[0]["outcome_r"] == 0.5


# ----------------------------------------------------------------------
# ⑥ 跨天强制结算
# ----------------------------------------------------------------------

def test_rollover_day_force_settles(tracker):
    """跨天（bar 日期前滚）：未结算单按最近收盘价 eod_flat 强平，
    未成交限价记 unfilled；新一天重新建档。"""
    bt = _ts(DAY, 620)
    tr1 = tracker.record("fvg_event", "NASDAQ:TST", "long", 100.0, 98.0, 2.0,
                         bt, dedup_key="d1")
    tr2 = tracker.record("proposal", "NASDAQ:TST", "long", 0.0, 0.0, 1.0, bt,
                         fill_mode="limit", limit_px=90.0, dedup_key="d2")
    tracker.process(_mkt("NASDAQ:TST", _bar(625, 100, 101, 99.5, 100.5)))
    # 下周一第一根 bar → 跨天强平（tr1 按 100.5 结算 0.25R；tr2 unfilled）
    ev, st = tracker.process(_mkt("NASDAQ:TST",
                                  _bar(570, 101, 102, 100, 101,
                                       day="2026-10-26")))
    assert {t.shadow_id for t in st} == {tr1.shadow_id, tr2.shadow_id}
    assert tr1.outcome == "eod_flat" and tr1.outcome_rr == 0.25
    assert tr2.outcome == "unfilled"
    assert len(tracker.events()) == 0       # 新一天内存清空


# ----------------------------------------------------------------------
# ⑦ 影子 tab 兼容（原有 stats 键 / events 字段 / pullback 自检行为）
# ----------------------------------------------------------------------

def test_compat_shadow_tab(tracker):
    """stats() 原有键一个不少；events() 原有字段都在；pullback 自检
    （刺穿收回线值）行为不变。"""
    # pullback 自检：多头趋势 + low 刺穿 ema_fast 收回线上 → 建档
    bt = _ts(DAY, 600)
    ts_c = {"trend_state": 1, "atr": 2.0, "ema_fast": 100.0,
            "channel_edge": 95.0}
    ev, st = tracker.process(_mkt("NASDAQ:TST",
                                  _bar(600, 101, 101.5, 99.5, 100.5), ts_c))
    assert len(ev) == 1 and ev[0].signal_type == "pullback"
    assert ev[0].line_type == "ema" and ev[0].entry == 100.0
    assert ev[0].stop == 98.0                  # 入场 −1×ATR（原口径）
    ev, st = tracker.process(_mkt("NASDAQ:TST",
                                  _bar(955, 100.5, 102, 100, 101.5), ts_c))
    stats = tracker.stats()
    for k in ("day", "total_events", "active", "settled", "stopped",
              "dist_before_stop", "dist_eod", "no_1r_count", "no_1r_pct",
              "avg_mfe_before_stop", "avg_mfe_eod", "max_mfe_before_stop",
              "max_mfe_eod", "avg_outcome_rr", "by_line", "by_side"):
        assert k in stats, k
    for k in ("by_type", "outcomes"):          # 新增键
        assert k in stats
    row = tracker.events()[0]
    for k in ("shadow_id", "symbol", "ticker", "side", "line_type",
              "bar_time", "entry", "stop", "atr", "mfe_rr", "mfe_rr_eod",
              "stopped", "settled", "outcome_rr", "close_px"):
        assert k in row, k
    assert stats["by_type"]["pullback"]["count"] == 1
    assert stats["outcomes"] == {"eod_flat": 1}
