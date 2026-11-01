# -*- coding: utf-8 -*-
"""FVG 首次回踩事件 + chase 追价下单回归（console/app.py）。

守的口径：FVG 事件固化（_fvg_record_event）→ 实时 bar 首次触区才报回踩
（多 low<=top / 空 high>=bottom，终身只报一次）→ 快照 retests/retested 字段；
chase 定价（_fvg_chase_prices）：偏离 FVG 边沿 >0.5×ATR 拒绝、无同向事件
拒绝、ATR 缺失拒绝、正常限价=现价×1.005/止损=现价−ATR（全局倍数）。
collector/order_mgr 全 mock，不落盘。
"""
from console import schemas
import asyncio
import time

import pytest

from fastapi import HTTPException
from .conftest import run

SYM_L = "NASDAQ:TESTL"
SYM_S = "NASDAQ:TESTS"

# 真实 epoch bar 时间（TTL 清理按 time.time() 裁剪，伪造小时间戳会被淘汰）
T0 = int(time.time() // 300 * 300) - 600   # FVG 事件 bar_time
T1 = T0 + 300                              # 未触区实时 bar
T2 = T0 + 600                              # 首次触区实时 bar
T3 = T0 + 900                              # 再次触区实时 bar

market = {}


class FakeCollector:
    def latest(self):
        return market


class FakeLot:
    lot_id = "lot-chase-1"

    def to_dict(self):
        return {"lot_id": self.lot_id}


class FakeOrderMgr:
    def __init__(self):
        self.place_calls = []

    async def place_limit_stop_lot(self, **kw):
        self.place_calls.append(kw)
        return FakeLot(), None


@pytest.fixture
def app(console_app, monkeypatch):
    monkeypatch.setattr(console_app, "collector", FakeCollector())
    om = FakeOrderMgr()
    monkeypatch.setattr(console_app, "order_mgr", om)
    console_app.signals._fvg_events.clear()
    console_app.signals._fvg_retests.clear()
    console_app.signals._fvg_last_bar.clear()
    console_app.signals._fvg_pending.clear()
    console_app.signals._fvg_warnings.clear()
    console_app.signals._fvg_warned.clear()
    yield console_app
    console_app.signals._fvg_events.clear()
    console_app.signals._fvg_retests.clear()
    console_app.signals._fvg_last_bar.clear()
    console_app.signals._fvg_pending.clear()
    console_app.signals._fvg_warnings.clear()
    console_app.signals._fvg_warned.clear()


def seed_fvg(app, sym, bt, side, top, bottom, atr):
    """用真实 _fvg_record_event 固化一个 FVG 事件。"""
    data = {
        "fvg_c": {"time": bt, "fvg_event": 1 if side == "long" else -1,
                  "fvg_last_top": top, "fvg_last_bottom": bottom},
        "ts_c": {"atr": atr},
    }
    assert app.signals._fvg_record_event(sym, bt, data)
    app.signals._fvg_last_bar[sym] = bt   # 防止监控循环重复固化


def test_first_retest_once_per_fvg(app):
    """首次回踩上报（多头/空头镜像）、终身只报一次、快照字段完整。"""
    async def go():
        seed_fvg(app, SYM_L, T0, "long", top=105.0, bottom=104.0, atr=1.0)
        seed_fvg(app, SYM_S, T0, "short", top=96.0, bottom=95.0, atr=1.0)
        market[SYM_L] = {"bar_c": {"time": T0},
                         "bar": {"time": T1, "low": 105.5, "high": 106.5,
                                 "close": 106.0}}
        market[SYM_S] = {"bar_c": {"time": T0},
                         "bar": {"time": T1, "low": 93.5, "high": 94.5,
                                 "close": 94.0}}

        task = asyncio.create_task(app.signals._fvg_monitor_loop())
        try:
            await asyncio.sleep(1.4)
            assert len(app.signals._fvg_retests) == 0, "未触区不应有回踩记录"

            # 多头 bar.low 首次 <= top / 空头 bar.high 首次 >= bottom
            market[SYM_L]["bar"] = {"time": T2, "low": 104.8, "high": 106.0,
                                    "close": 105.2}
            market[SYM_S]["bar"] = {"time": T2, "low": 93.8, "high": 95.3,
                                    "close": 94.8}
            await asyncio.sleep(1.4)
            assert len(app.signals._fvg_retests) == 2, list(app.signals._fvg_retests)
            rt_l = next(r for r in app.signals._fvg_retests if r["side"] == "long")
            rt_s = next(r for r in app.signals._fvg_retests if r["side"] == "short")
            assert rt_l["id"] == f"{SYM_L}:{T0}:long:rt" and rt_l["retest_px"] == 104.8
            assert rt_l["retest_time"] == T2 and rt_l["bar_time"] == T0
            assert rt_l["top"] == 105.0 and rt_l["bottom"] == 104.0
            assert rt_s["id"] == f"{SYM_S}:{T0}:short:rt" and rt_s["retest_px"] == 95.3
            ev_l = next(e for e in app.signals._fvg_events if e["side"] == "long")
            ev_s = next(e for e in app.signals._fvg_events if e["side"] == "short")
            assert ev_l["retested"] is True and ev_s["retested"] is True

            # 再次回踩不再报（终身一次）
            market[SYM_L]["bar"] = {"time": T3, "low": 104.2, "high": 105.5,
                                    "close": 104.6}
            market[SYM_S]["bar"] = {"time": T3, "low": 94.0, "high": 95.8,
                                    "close": 95.5}
            await asyncio.sleep(1.4)
            assert len(app.signals._fvg_retests) == 2, "终身只报一次"

            # 快照：retests 倒序 + slots 事件带 retested
            market[SYM_L]["bar_c"] = {"time": T3}
            market[SYM_S]["bar_c"] = {"time": T3}
            snap = app.signals._fvg_monitor_snapshot()
            assert len(snap["retests"]) == 2
            assert snap["retests"][0]["retest_time"] >= snap["retests"][1]["retest_time"]
            slot = next(s for s in snap["slots"] if s["bar_time"] == T0)
            assert slot["long"][0]["retested"] is True
            assert slot["short"][0]["retested"] is True
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
    run(go())


def chase_market(close, atr, side="long"):
    market.clear()
    market[SYM_L] = {"bar": {"time": 2000, "low": close - 0.3,
                             "high": close + 0.3, "close": close},
                     "bar_c": {"time": 1700},
                     "ts_c": {"atr": atr, "direction": 1 if side == "long" else -1,
                              "trend_state": 1 if side == "long" else -1}}


async def chase_req(app, side="long", force=False):
    req = schemas.LimitStopOrderRequest(
        symbol=SYM_L, side=side, qty=10, limit_px=1.0, stop_px=1.0,
        limit_type="chase", force=force, reason="t")
    return await app.trading.api_place_limit_stop(req, trader="master")


def test_chase_normal_pricing(app):
    """chase 正常定价：limit=现价×1.005；止损=智能止损口径（逻辑止损 0.3
    被钳到下限 1×ATR=2.0 → stop=98.0）；一次定价（无改价/突破腿）。"""
    async def go():
        seed_fvg(app, SYM_L, 1700, "long", top=100.2, bottom=99.0, atr=2.0)
        chase_market(close=100.0, atr=2.0)
        res = await chase_req(app)
        assert res["ok"] is True
        call = app.order_mgr.place_calls[-1]
        assert call["limit_type"] == "chase"
        assert call["limit_px"] == 100.5     # 100 × 1.005
        assert call["stop_px"] == 98.0       # 智能止损：max(逻辑0.3, 1×ATR)
        assert call["reprice_bar_time"] == 0 and call["breakout_px"] == 0
    run(go())


def test_chase_no_fvg_event_rejected(app):
    """无对应方向 FVG 事件 → 400。"""
    async def go():
        chase_market(close=100.0, atr=2.0)
        with pytest.raises(HTTPException) as ei:
            await chase_req(app, side="short", force=True)
        assert ei.value.status_code == 400
        assert "无可用 FVG 事件" in str(ei.value.detail)
    run(go())


def test_chase_too_far_from_edge_rejected(app):
    """距离超阈值：close=105, top=100, atr=2 → 2.50×ATR > 0.5×ATR → 400。"""
    async def go():
        seed_fvg(app, SYM_L, 1700, "long", top=100.0, bottom=99.0, atr=2.0)
        chase_market(close=105.0, atr=2.0)
        with pytest.raises(HTTPException) as ei:
            await chase_req(app)
        assert ei.value.status_code == 400
        assert "偏离 FVG 边沿过远" in str(ei.value.detail)
        assert "2.50×ATR" in str(ei.value.detail)
    run(go())


def test_chase_atr_missing_rejected(app):
    """ATR 缺失拒绝（force 跳过趋势闸门，由 chase 定价自身拦截）。"""
    async def go():
        seed_fvg(app, SYM_L, 1700, "long", top=100.2, bottom=99.0, atr=2.0)
        chase_market(close=100.0, atr=None)
        with pytest.raises(HTTPException) as ei:
            await chase_req(app, force=True)
        assert ei.value.status_code == 400 and "ATR" in str(ei.value.detail)
    run(go())
