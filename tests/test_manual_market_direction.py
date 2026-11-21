"""手动震荡方向仅放行市价单同向检查，隔离行情、账户和券商。"""

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from pydantic import ValidationError

import config
from console.schemas import LimitStopOrderRequest
from console.services import trading
from .conftest import run


@pytest.fixture
def manual_market(console_app, monkeypatch):
    stamp = int(datetime(2026, 11, 17, 15, 15, 5, tzinfo=timezone.utc).timestamp())
    minute = stamp - 5
    symbol = "NASDAQ:NVDA"

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.fromtimestamp(stamp, tz)

    data = {
        "bar": {"time": minute, "open": 100, "close": 100, "low": 95, "high": 105},
        "ts_c": {"atr": 2, "direction": 1},
        "market_1m": {"bar": {"time": minute}, "bar_c": {"time": minute - 60}},
        "vwap_pullback": {"time": minute, "direction": 1},
        "vwap_pullback_c": {"time": minute - 60, "direction": 1},
    }
    monkeypatch.setattr(trading, "datetime", Clock)
    monkeypatch.setattr(config, "ALLOW_SHORT", True)
    monkeypatch.setattr(console_app.collector, "latest", lambda: {symbol: data})
    monkeypatch.setattr(console_app.accounts, "_equity_cache", {"equity": 100000})
    place = AsyncMock(return_value=(SimpleNamespace(lot_id="isolated", to_dict=lambda: {}), ""))
    monkeypatch.setattr(console_app.order_mgr, "place_limit_stop_lot", place)
    events = Mock()
    monkeypatch.setattr(console_app.events, "record_event", events)

    def submit(side="short", **kwargs):
        args = dict(symbol=symbol, side=side, limit_type="market", limit_px=100, stop_px=96,
                    market_direction_mode="manual", range_direction_confirmed=True)
        args.update(kwargs)
        return run(console_app.trading.api_place_limit_stop(LimitStopOrderRequest(**args)))

    return SimpleNamespace(app=console_app, data=data, submit=submit, place=place, events=events)


@pytest.mark.parametrize("side,st,stop", [("long", -1, 94.99), ("short", 1, 105.01)])
def test_manual_direction_mirrors_stop_and_retains_real_st(manual_market, side, st, stop):
    case = manual_market
    case.data["vwap_pullback_c"]["direction"] = st
    assert case.submit(side)["ok"]
    kwargs = case.place.await_args.kwargs
    assert kwargs["side"] == side
    assert kwargs["stop_px"] == stop
    assert kwargs["qty"] == min(int(config.RISK_PER_TRADE_USD / 5.01), config.RISK_MAX_ORDER_QTY)
    assert kwargs["risk_usd"] == config.RISK_PER_TRADE_USD
    assert kwargs["manual_entry"] is True
    snapshot = kwargs["signal_snapshot"]
    assert snapshot["market_direction_mode"] == "manual"
    assert snapshot["range_direction_confirmed"] is True
    assert snapshot["manual_market_side"] == side
    assert snapshot["market_direction_st_1m"]["direction"] == st
    assert snapshot["stop_source"] == "market_2atr_outside_bar"
    decision = next(call.kwargs for call in case.events.call_args_list
                    if call.args[0] == "manual_entry_decision")
    assert decision["range_direction_confirmed"] is True
    assert decision["market_direction_st_1m"]["direction"] == st


@pytest.mark.parametrize("changes,message", [
    ({"range_direction_confirmed": False}, "请先确认"),
    ({"limit_type": "ema"}, "仅用于手动市价单"),
    ({"force": True}, "不能同时使用执意开仓"),
    ({"market_direction_mode": "st"}, "必须与手动方向同时提交"),
])
def test_manual_direction_rejects_missing_confirmation_or_other_entry_modes(manual_market, changes, message):
    with pytest.raises(trading.HTTPException, match=message):
        manual_market.submit(**changes)
    manual_market.place.assert_not_awaited()


def test_manual_direction_cannot_apply_to_opening_breakout_adapter(manual_market):
    req = LimitStopOrderRequest(symbol="NASDAQ:NVDA", side="short", limit_type="market",
                               limit_px=100, stop_px=105, market_direction_mode="manual",
                               range_direction_confirmed=True)
    with pytest.raises(trading.HTTPException, match="仅用于手动市价单"):
        run(manual_market.app.trading.api_place_limit_stop(req, opening_breakout={"id": "test"}))
    manual_market.place.assert_not_awaited()


@pytest.mark.parametrize("fault,message", [
    ("minute", "1 分钟 ST 未就绪"),
    ("direction", "1 分钟 ST 方向未就绪"),
    ("bar", "当前 5 分钟 K 线未就绪"),
    ("atr", "ATR 数据缺失"),
    ("short_disabled", "空头已屏蔽"),
])
def test_manual_direction_preserves_readiness_and_short_permission(manual_market, monkeypatch, fault, message):
    data = manual_market.data
    if fault == "minute":
        data["market_1m"]["bar"]["time"] -= 60
    elif fault == "direction":
        data["vwap_pullback"]["direction"] = data["vwap_pullback_c"]["direction"] = 0
    elif fault == "bar":
        data["bar"]["time"] -= 300
    elif fault == "atr":
        data["ts_c"]["atr"] = None
    else:
        monkeypatch.setattr(config, "ALLOW_SHORT", False)
    with pytest.raises(trading.HTTPException, match=message):
        manual_market.submit()
    manual_market.place.assert_not_awaited()


def test_manual_direction_preserves_order_manager_risk_rejection(manual_market):
    manual_market.place.return_value = (None, "总风险敞口超限")
    with pytest.raises(trading.HTTPException, match="总风险敞口超限"):
        manual_market.submit()
    assert manual_market.place.await_count == 1


def test_omitted_mode_preserves_st_filter_and_invalid_modes_fail_schema(manual_market):
    req = LimitStopOrderRequest(symbol="NASDAQ:NVDA", side="short", limit_type="market",
                               limit_px=100, stop_px=105)
    assert req.market_direction_mode == "st"
    with pytest.raises(trading.HTTPException) as error:
        run(manual_market.app.trading.api_place_limit_stop(req))
    assert error.value.detail["code"] == "not_long_candidate"
    with pytest.raises(ValidationError):
        LimitStopOrderRequest(symbol="NASDAQ:NVDA", limit_px=100, stop_px=95,
                              market_direction_mode="invalid")
    manual_market.place.assert_not_awaited()
