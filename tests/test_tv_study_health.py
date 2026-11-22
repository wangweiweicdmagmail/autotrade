# -*- coding: utf-8 -*-
"""TV study 通道健康监测回归（engine/realtime_collector.py study_health）。

守的口径（2026-10-27 协议故障教训：K线正常、指标全灭，data_stale 检测不到）：
- 错误风暴：60s 内 local_indicator_error > 20 条 → systemic（协议级故障是全体刷屏，
  单标的自愈重建每分钟几条不会误触）；61s 前的旧错误自动滑出窗口。
- 持续死亡：快照已建立但 ts_c 就绪率 < 50% 且持续 > 120s 才 systemic
  （daemon 冷启动 54 标的加载约 60-90s，宽限期内不误报）。
- 就绪率恢复 ≥50% 立即回到健康（_ts_dead_since 清零）。
"""
import time

from engine.realtime_collector import RealtimeCollector


def _make(symbols=("NASDAQ:QQQ", "NASDAQ:NVDA", "NASDAQ:AAPL", "NASDAQ:TSLA")):
    return RealtimeCollector(list(symbols))


def _fill_snapshot(c, with_ts: int):
    """with_ts 只标的给 ts_c.trend_state，其余给空 ts_c。"""
    snap = {}
    for i, sym in enumerate(c.symbols):
        snap[sym] = {"ts_c": {"trend_state": 1} if i < with_ts else {}}
    c._snapshot = snap


def test_no_data_no_alert():
    c = _make()
    h = c.study_health()
    assert h["systemic"] is False
    assert h["study_errors_60s"] == 0
    assert h["ts_ready"] == 0


def test_healthy_snapshot():
    c = _make()
    _fill_snapshot(c, with_ts=4)
    h = c.study_health()
    assert h["systemic"] is False
    assert h["ts_ready"] == 4 and h["ts_total"] == 4
    assert h["ts_dead_for"] == 0.0


def test_error_storm_immediate():
    c = _make()
    _fill_snapshot(c, with_ts=4)   # 快照健康也照报（错误风暴独立触发）
    now = time.time()
    for _ in range(21):
        c._study_errors.append(now)
    assert c.study_health()["systemic"] is True


def test_old_errors_expire_from_window():
    c = _make()
    _fill_snapshot(c, with_ts=4)
    old = time.time() - 61
    for _ in range(30):
        c._study_errors.append(old)
    h = c.study_health()
    assert h["study_errors_60s"] == 0
    assert h["systemic"] is False


def test_dead_ratio_needs_persistence():
    c = _make()
    _fill_snapshot(c, with_ts=1)   # 1/4 就绪 < 50%
    first = c.study_health()
    assert first["systemic"] is False          # 宽限期内
    assert first["ts_dead_for"] >= 0
    c._ts_dead_since = time.time() - 121       # 模拟已持续 121s
    assert c.study_health()["systemic"] is True


def test_recovery_clears_dead_state():
    c = _make()
    _fill_snapshot(c, with_ts=1)
    c._ts_dead_since = time.time() - 300       # 一度死亡
    _fill_snapshot(c, with_ts=4)               # 恢复
    h = c.study_health()
    assert h["systemic"] is False
    assert h["ts_dead_for"] == 0.0
    # 恢复后再次跌破要重新计时（不沿用旧起点）
    _fill_snapshot(c, with_ts=0)
    assert c.study_health()["systemic"] is False


def test_stderr_counting():
    """Only local computation failures contribute; legacy protocol text is ignored."""
    import asyncio
    from types import SimpleNamespace
    c = _make()
    async def exercise():
        reader = asyncio.StreamReader()
        reader.feed_data(b"local_indicator_error: calculation failed\nstudy_error: retired\n")
        reader.feed_eof()
        await c._stderr_drain(SimpleNamespace(stderr=reader))
    asyncio.run(exercise())
    assert len(c._study_errors) == 1
