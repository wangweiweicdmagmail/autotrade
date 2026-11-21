# -*- coding: utf-8 -*-
"""止损 ATR 倍数全局设置回归（console/app.py settings + 定价函数）。

守的口径：stop_atr_mult 默认回退 config.STOP_ATR_MULT；POST /api/settings
改后 midpoint/market/chase 定价立即按新倍数；快照 status 同步；0.2~5.0
越界 400 且不污染当前值；落盘持久化（tmp 路径）；lot 固化 atr_mult
（突破成交补挂止损沿用下单口径）；策略 params 缺省回退全局默认。
"""
from console import schemas
import json

import pytest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import config
from .conftest import run

NOW = datetime.fromtimestamp(1700000600, timezone.utc)

DATA = {
    "bar": {"time": NOW.timestamp(), "close": 100.0, "high": 100.5, "low": 99.5},
    "bar_c": {"high": 101.0, "low": 99.0, "time": 1700000000},
    "bar_c2": {"high": 100.8, "low": 98.8, "time": 1700000300},
    "ts": {"atr": 2.0, "direction": 1},
    "ts_c": {"atr": 2.0, "direction": 1},
}


@pytest.fixture
def app(console_app, monkeypatch):
    console_app.settings._settings.clear()          # 缺键 → 回退 config 默认
    console_app.signals._fvg_events.clear()
    monkeypatch.setattr(console_app.accounts, "_equity_cache",
                        {"equity": 100000.0, "daily_pnl": 0.0, "ts": 9e18})
    yield console_app
    console_app.signals._fvg_events.clear()


def test_default_matches_config(app):
    """① 默认 1.0 时各定价函数与原 1×ATR 口径一致。"""
    assert app.settings._stop_atr_mult() == config.STOP_ATR_MULT
    limit, stop, breakout, bar, atr = app.trading._midpoint_prices(DATA, "long")
    assert (limit, stop) == (100.0, 98.0)
    limit, stop = app.trading._market_prices(DATA, "long", now=NOW)
    assert stop == 96.0
    limit, stop = app.trading._market_prices(DATA, "short", now=NOW)
    assert stop == 104.0
    app.signals._fvg_events.append({"symbol": "NASDAQ:NVDA", "side": "long",
                            "top": 100.0, "bottom": 99.0})
    limit, stop = app.trading._fvg_chase_prices(DATA, "long", "NASDAQ:NVDA")
    assert stop == 98.0


def test_post_1_5x_reprices_immediately(app):
    """全局倍数调整影响中点和追价，手动市价单固定用 2×ATR 加 K 线边界。"""
    data2 = {
        "bar": {"time": NOW.timestamp(), "close": 100.0, "high": 100.5, "low": 99.5},
        "bar_c": {"high": 101.0, "low": 96.0, "time": 1700000000},
        "bar_c2": {"high": 102.8, "low": 98.8, "time": 1700000300},
        "ts": {"atr": 2.0, "direction": 1},
        "ts_c": {"atr": 2.0, "direction": 1},
    }
    app.signals._fvg_events.append({"symbol": "NASDAQ:NVDA", "side": "long",
                            "top": 100.0, "bottom": 99.0})
    # 默认 1.0× 基线
    assert app.trading._midpoint_prices(data2, "long")[1] == 96.5
    assert app.trading._market_prices(data2, "long", now=NOW)[1] == 96.0
    assert app.trading._market_prices(data2, "short", now=NOW)[1] == 104.0
    assert app.trading._fvg_chase_prices(data2, "long", "NASDAQ:NVDA")[1] == 98.0

    r = run(app.settings.api_update_settings(schemas.SettingsRequest(stop_atr_mult=1.5),
                                    trader="master"))
    assert r["ok"] and r["stop_atr_mult"] == 1.5
    assert run(app.settings.api_get_settings())["stop_atr_mult"] == 1.5
    assert app.trading._midpoint_prices(data2, "long")[1] == 96.0    # 钳制上限 3.0
    assert app.trading._market_prices(data2, "long", now=NOW)[1] == 96.0
    assert app.trading._market_prices(data2, "short", now=NOW)[1] == 104.0
    assert app.trading._fvg_chase_prices(data2, "long", "NASDAQ:NVDA")[1] == 97.0


def test_snapshot_carries_value(app):
    """③ 快照 status 带新值。"""
    run(app.settings.api_update_settings(schemas.SettingsRequest(stop_atr_mult=1.5),
                                trader="master"))
    snap = run(app.snapshot.build_snapshot())
    assert snap["status"]["stop_atr_mult"] == 1.5
    assert snap["status"]["manual_market_st_interval_seconds"] == 60
    assert snap["status"]["risk_per_trade_usd"] == config.RISK_PER_TRADE_USD
    assert app.risk_gate.cfg.per_trade_usd == config.RISK_PER_TRADE_USD


def test_manual_market_order_uses_current_one_minute_st(app, monkeypatch):
    from console.services import trading

    stamp = int(datetime(2026, 11, 17, 15, 15, 5, tzinfo=timezone.utc).timestamp())
    minute = stamp - 5
    symbol = "NASDAQ:NVDA"

    class FixedClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.fromtimestamp(stamp, tz)

    data = {
        **DATA,
        "bar": {**DATA["bar"], "time": minute},
        "ts_c": {"atr": 2.0, "direction": -1},
        "market_1m": {"bar": {"time": minute}, "bar_c": {"time": minute - 60}},
        "vwap_pullback": {"time": minute, "direction": -1, "candidate_direction": 0},
        "vwap_pullback_c": {"time": minute - 60, "direction": 1, "candidate_direction": 0},
    }
    monkeypatch.setattr(trading, "datetime", FixedClock)
    monkeypatch.setattr(app.collector, "latest", lambda: {symbol: data})
    lot = SimpleNamespace(lot_id="test", to_dict=lambda: {"lot_id": "test"})
    place = AsyncMock(return_value=(lot, ""))
    monkeypatch.setattr(app.order_mgr, "place_limit_stop_lot", place)

    def submit(side, force=False):
        req = schemas.LimitStopOrderRequest(symbol=symbol, side=side, limit_type="market",
                                            limit_px=100.0, stop_px=96.0, force=force, reason="test")
        return run(app.trading.api_place_limit_stop(req))

    with pytest.raises(app.HTTPException) as mismatch:
        submit("short")
    assert "1 分钟 ST方向=1" in mismatch.value.detail["msg"]
    assert submit("long")["ok"]
    kwargs = place.await_args.kwargs
    assert kwargs["stop_px"] == 96.0
    assert kwargs["signal_snapshot"]["market_direction_st_1m"] == {
        "time": minute - 60, "direction": 1, "confirmed": True,
    }

    data["vwap_pullback_c"]["time"] -= 60
    assert submit("short")["ok"]
    kwargs = place.await_args.kwargs
    assert kwargs["stop_px"] == 104.0
    assert kwargs["signal_snapshot"]["market_direction_st_1m"] == {
        "time": minute, "direction": -1, "confirmed": False,
    }

    data["market_1m"]["bar"]["time"] -= 60
    with pytest.raises(app.HTTPException, match="1 分钟 ST 未就绪"):
        submit("short", force=True)
    assert place.await_count == 2


def test_range_guard(app):
    """④ 越界 0.1 / 6 → 400，且不污染当前值。"""
    run(app.settings.api_update_settings(schemas.SettingsRequest(stop_atr_mult=1.5),
                                trader="master"))
    for bad in (0.1, 6.0):
        with pytest.raises(app.HTTPException) as ei:
            run(app.settings.api_update_settings(schemas.SettingsRequest(stop_atr_mult=bad),
                                        trader="master"))
        assert ei.value.status_code == 400
    assert app.settings._stop_atr_mult() == 1.5


def test_persist_and_reload(app):
    """⑤ 落盘后重载（模拟重启）值保持。"""
    run(app.settings.api_update_settings(schemas.SettingsRequest(stop_atr_mult=1.5),
                                trader="master"))
    payload = json.loads(app.settings._SETTINGS_PATH.read_text(encoding="utf-8"))
    assert payload.get("stop_atr_mult") == 1.5
    app.settings._settings.clear()
    app.settings._settings.update(app.settings._load_settings())   # 模拟重启重载
    assert app.settings._stop_atr_mult() == 1.5


@pytest.mark.parametrize('manual_mode',[None,'atr_2','vwap_sigma'])
def test_lot_atr_mult_frozen(app, tmp_path, manual_mode):
    """⑥ 中点单 lot 固化 atr_mult=2.0：序列化/旧状态兼容/突破补挂止损口径。"""
    from execution.order_lot import OrderLot, OrderManager, PENDING

    class FakeGW:
        enabled = False
        _connected = False

        def __init__(self):
            self.stop_only = []

        async def realized_pnl(self):
            return 0.0

        async def place_midpoint_oca(self, symbol, side, qty, limit_px,
                                     breakout_px, stop_px, oca_group):
            return {"ok": True, "dry_run": True, "entry_order_id": 1,
                    "stop_order_id": 2, "breakout_order_id": 3,
                    "trade": None, "stop_trade": None, "breakout_trade": None}

        async def place_stop_only(self, symbol, side, qty, stop_px):
            self.stop_only.append(stop_px)
            return {"ok": True, "stop_order_id": 9, "trade": None}

    class FakeRisk:
        def check_new_order(self, *a, **k):
            return True, ""

    gw = FakeGW()
    om = OrderManager(gw, FakeRisk(), state_path=tmp_path / "lots_state.json")
    lot, err = run(om.place_limit_stop_lot(
        symbol="NASDAQ:NVDA", side="long", qty=10, limit_px=100.0, stop_px=96.0,
        limit_type="midpoint", breakout_px=101.01, atr_mult=2.0))
    assert lot is not None and lot.atr_mult == 2.0, err

    d = lot.to_dict()
    assert d["atr_mult"] == 2.0
    d.pop("atr_mult")
    assert OrderLot.from_dict(d).atr_mult == 1.0   # 旧状态文件兼容

    lot.state = PENDING
    lot.breakout_filled = True
    lot.trail_trade = None
    lot.signal_snapshot = {"ts_c": {"atr": 2.0}}
    expected=96.0
    if manual_mode:
        lot.signal_snapshot['manual_stop_mode']=manual_mode
        lot.stop_px=expected=94.0  # Preserve the explicitly chosen initial protective price.
    ok = run(om._activate_filled_entry(lot, "Filled"))
    assert ok and gw.stop_only == [expected]
    assert lot.stop_px == expected


def test_strategy_param_override():
    """⑦ trend_pullback：params.stop_atr_mult=1.5 止损变宽；缺省回退全局默认。"""
    from engine.strategies import build_strategy

    ctx = {"ts_c": {"direction": 1, "atr": 2.0, "ema_fast": 100.0},
           "fvg_c": {}}
    st = build_strategy("trend_pullback")
    plan = st.entry_trigger(ctx, "NASDAQ:NVDA", 99.0)
    assert plan is not None and plan.stop_px == 98.0
    st15 = build_strategy("trend_pullback", stop_atr_mult=1.5)
    plan = st15.entry_trigger(ctx, "NASDAQ:NVDA", 99.0)
    assert plan is not None and plan.stop_px == 97.0
    st.params.pop("stop_atr_mult", None)
    plan = st.entry_trigger(ctx, "NASDAQ:NVDA", 99.0)
    assert plan is not None and plan.stop_px == round(
        100.0 - 2.0 * config.STOP_ATR_MULT, 2)
