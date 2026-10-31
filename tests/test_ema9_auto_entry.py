# -*- coding: utf-8 -*-
"""EMA9 当前 bar 自动成交与 2R 后重新武装。"""
from types import SimpleNamespace

from engine.auto_engine import AWAITING_CONFIRM, DONE, PENDING, WATCHING, StrategyEngine
from engine.strategies import build_strategy
from .conftest import run


class FakeBroker:
    def __init__(self):
        self.calls = []

    async def place_limit_stop(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(lot_id=f"lot-{len(self.calls)}")


class FakeRisk:
    def size_position(self, _entry, _stop, _equity):
        return 10


def _engine(broker):
    return StrategyEngine(broker, risk_gate=FakeRisk(),
                          require_entry_confirmation=True)


def _ctx(ema_fast=100.0, ema_9=105.0):
    return {"direction": 1, "atr": 2.0, "ema_fast": ema_fast,
            "ema_9": ema_9, "channel_edge": 90.0}


def test_ema9_touch_places_order_without_waiting_for_confirmation():
    broker = FakeBroker()
    engine = _engine(broker)
    strategy = build_strategy("trend_pullback", use_fvg_line=False)
    engine.start(strategy, ["NASDAQ:NVDA"])

    run(engine.on_price("NASDAQ:NVDA", 104.0, _ctx(), {"time": 1000}, 600))

    inst = engine._instances["NASDAQ:NVDA"]
    assert inst.state == PENDING
    assert engine.proposals() == []
    assert len(broker.calls) == 1
    order = broker.calls[0]
    assert order["limit_type"] == "auto_trend_pullback"
    assert order["limit_px"] == 105.0 and order["stop_px"] == 103.0


def test_other_entry_lines_still_require_confirmation():
    broker = FakeBroker()
    engine = _engine(broker)
    strategy = build_strategy("trend_pullback", use_fvg_line=False)
    engine.start(strategy, ["NASDAQ:NVDA"])

    run(engine.on_price("NASDAQ:NVDA", 104.0,
                        _ctx(ema_fast=105.0, ema_9=106.0), {"time": 1000}, 600))

    inst = engine._instances["NASDAQ:NVDA"]
    assert inst.state == AWAITING_CONFIRM
    assert len(engine.proposals()) == 1
    assert broker.calls == []


def test_ema9_reentry_requires_2r_mfe_after_activation():
    engine = _engine(FakeBroker())
    strategy = build_strategy("trend_pullback")
    engine.start(strategy, ["NASDAQ:NVDA"])
    inst = engine._instances["NASDAQ:NVDA"]
    inst.state = DONE
    inst.entries_today = 1
    inst.activated = True
    inst.line = "ema9"
    inst.mfe_r = 1.99

    engine._try_rearm(inst.symbol, inst)
    assert inst.state == DONE

    inst.mfe_r = 2.0
    engine._try_rearm(inst.symbol, inst)
    assert inst.state == WATCHING
    assert inst.activated is False


def test_unfilled_cancellation_does_not_trigger_2r_lock():
    engine = _engine(FakeBroker())
    strategy = build_strategy("trend_pullback")
    engine.start(strategy, ["NASDAQ:NVDA"])
    inst = engine._instances["NASDAQ:NVDA"]
    inst.state = DONE
    inst.entries_today = 1
    inst.activated = False
    inst.line = "ema9"
    inst.mfe_r = 0.0

    engine._try_rearm(inst.symbol, inst)
    assert inst.state == WATCHING
