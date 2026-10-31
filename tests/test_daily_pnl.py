# -*- coding: utf-8 -*-
"""日盈亏展示时段：周末/开盘前归零，常规时段保持 IBKR 净值差口径。"""
from datetime import datetime
from zoneinfo import ZoneInfo

import execution.ibkr as ibkr_module
from execution.ibkr import IBKRGateway
from .conftest import run

ET = ZoneInfo("America/New_York")


def _freeze(monkeypatch, value):
    class FrozenDateTime:
        @classmethod
        def now(cls, _tz):
            return value
    monkeypatch.setattr(ibkr_module, "datetime", FrozenDateTime)


def test_weekend_is_not_daily_pnl_session():
    assert IBKRGateway._daily_pnl_session_active(datetime(2026, 10, 31, 12, 0, tzinfo=ET)) is False
    assert IBKRGateway._daily_pnl_session_active(datetime(2026, 11, 1, 12, 0, tzinfo=ET)) is False


def test_regular_session_boundary_and_after_close():
    assert IBKRGateway._daily_pnl_session_active(datetime(2026, 10, 30, 9, 29, tzinfo=ET)) is False
    assert IBKRGateway._daily_pnl_session_active(datetime(2026, 10, 30, 9, 30, tzinfo=ET)) is True
    assert IBKRGateway._daily_pnl_session_active(datetime(2026, 10, 30, 16, 30, tzinfo=ET)) is True


def test_saturday_returns_zero_without_calling_ibkr(monkeypatch):
    _freeze(monkeypatch, datetime(2026, 10, 31, 12, 0, tzinfo=ET))
    gw = IBKRGateway.__new__(IBKRGateway)

    async def must_not_query():
        raise AssertionError("周末不应再查询净值并沿用上一交易日差值")

    gw.net_liquidation = must_not_query
    gw.prev_day_equity = must_not_query
    assert run(gw.daily_pnl()) == 0.0


def test_premarket_returns_zero_without_calling_ibkr(monkeypatch):
    _freeze(monkeypatch, datetime(2026, 10, 30, 9, 29, tzinfo=ET))
    gw = IBKRGateway.__new__(IBKRGateway)

    async def must_not_query():
        raise AssertionError("开盘前不应展示 IBKR 前一日权益差值")

    gw.net_liquidation = must_not_query
    gw.prev_day_equity = must_not_query
    assert run(gw.daily_pnl()) == 0.0


def test_regular_session_keeps_equity_difference(monkeypatch):
    _freeze(monkeypatch, datetime(2026, 10, 30, 10, 0, tzinfo=ET))
    gw = IBKRGateway.__new__(IBKRGateway)

    async def current():
        return 217797.61

    async def previous():
        return 217476.36

    gw.net_liquidation = current
    gw.prev_day_equity = previous
    assert run(gw.daily_pnl()) == 321.25
