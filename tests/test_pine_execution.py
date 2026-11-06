"""模型映射与公共执行链路；第二模型仅为隔离测试字段，不实现 Pine 策略。"""

from dataclasses import replace
from types import SimpleNamespace

import pytest

from engine.broker_protocols import TradeBroker, SimulatedPriceFeed
from engine.brokers import SimBroker, LiveBroker
from engine.pine_models import PineModel, PineModelRegistry
from engine.pine_execution import PineExecutionEngine
from console.services.auto import AutoService, AutoServiceError
from console.runtime.strategy import StrategyRuntime
from .test_pyramid_safety import StateBroker, row_for
from .test_pyramid_engine import _e9
from .conftest import run

SYM = "NASDAQ:NVDA"


def fixture_model():
    return PineModel(
        "fixture_pullback",
        "测试 Pine 字段映射",
        "USER;fixture",
        "fixture",
        "fixture_c",
        {
            "entry_signal": "go",
            "direction": "bias",
            "entry_price": "trigger",
            "long_stop": "protect_long",
            "short_stop": "protect_short",
        },
    )


def fixture_output(time=1000, direction=1, signal=1):
    return {
        "time": time,
        "go": signal,
        "bias": direction,
        "trigger": 100,
        "protect_long": 99,
        "protect_short": 101,
    }


def registry():
    result = PineModelRegistry.load()
    result.register(fixture_model())
    return result


def test_legacy_broker_imports_are_same_objects():
    from engine.auto_engine import (
        SimBroker as OldSimBroker,
        LiveBroker as OldLiveBroker,
    )

    assert OldSimBroker is SimBroker and OldLiveBroker is LiveBroker
    assert isinstance(SimBroker(), TradeBroker)
    assert isinstance(SimBroker(), SimulatedPriceFeed)
    assert not isinstance(LiveBroker(None), SimulatedPriceFeed)


def test_indicator_identity_and_values_come_from_ema9_st_atr():
    model = PineModelRegistry.load().get("ema9_pyramid")
    assert model.name == "EMA9-ST-ATR"
    assert model.indicator_id == "USER;a17a52ae5b6642e0a3ddbba23edc9ff3"
    raw = _e9(100, 1, direction=-1)
    output = model.decode(raw)
    assert output.entry_signal and output.direction == -1
    assert output.entry_price == 100 and output.stop_px == 101


@pytest.mark.parametrize(
    "overrides",
    [
        {"trigger": float("nan")},
        {"protect_long": 101},
        {"bias": 0},
        {"time": None},
        {"protect_long": float("inf")},
    ],
)
def test_invalid_pine_values_fail_closed(overrides):
    output = fixture_model().decode(fixture_output() | overrides)
    assert output.errors
    assert output.execution_values()["entry_signal"] == 0


def test_price_does_not_synthesize_signal_or_direction():
    broker = StateBroker()
    engine = PineExecutionEngine(broker, model=fixture_model())
    engine.start([SYM], 100, {SYM: 1})
    run(engine.on_price(SYM, 50, fixture_output(signal=0), 600))
    run(engine.on_price(SYM, 200, fixture_output(direction=0), 600))
    assert broker.calls == []


def test_second_mapping_uses_same_executor_and_own_pine_stop():
    broker = StateBroker()
    engine = PineExecutionEngine(broker, model=fixture_model())
    engine.start([SYM], 100, {SYM: 1})
    run(engine.on_price(SYM, 100, fixture_output(), 600))
    order = broker.calls[0]
    assert order["limit_px"] == 100 and order["stop_px"] == 99 and order["qty"] == 100
    assert not engine.breakeven_enabled
    run(engine.on_price(SYM, 110, fixture_output(time=1300), 600))
    assert len(broker.calls) == 1 and broker.lots["lot-1"].stop_px == 99
    run(engine.on_bar(SYM, {"close": 101}, {"bias": 1, "protect_long": 99.5}))
    assert broker.lots["lot-1"].stop_px == 99.5
    run(engine.close_symbol(SYM,100))
    assert broker.lots["lot-1"].state == "CLOSED"


def test_output_cannot_be_dispatched_to_wrong_model():
    engine = PineExecutionEngine(StateBroker())
    with pytest.raises(ValueError, match="归属"):
        run(engine.on_output(SYM, 100, fixture_model().decode(fixture_output()), 600))


def test_two_models_same_symbol_have_separate_orders_and_recovery():
    broker = StateBroker()
    first = PineExecutionEngine(broker)
    second = PineExecutionEngine(broker, model=fixture_model(), instance_id="second")
    first.start([SYM], 100, {SYM: 1})
    second.start([SYM], 100, {SYM: 1})
    run(first.on_price(SYM, 100, _e9(100, 1, 1), 600))
    run(second.on_price(SYM, 100, fixture_output(), 600))
    run(first.on_price(SYM, 100, _e9(100, 1, 1), 600))
    run(second.on_price(SYM, 100, fixture_output(), 600))
    assert first.instances()[0]["group_id"] != second.instances()[0]["group_id"]
    assert first.instances()[0]["inst_id"] != second.instances()[0]["inst_id"]
    rows = [
        row_for(lot) | {"limit_type": lot.limit_type} for lot in broker.lots.values()
    ]
    restored = PineExecutionEngine(broker, model=fixture_model(), instance_id="second")
    run(restored.adopt_manual_lots(rows))
    assert restored.instances()[0]["lot_ids"] == ["lot-2"]
    assert restored.instances()[0]["strategy"] == "fixture_pullback"
    run(first.close_symbol(SYM,99))
    assert broker.lots["lot-1"].state == "CLOSED"
    assert broker.lots["lot-2"].state == "ACTIVE"


def test_two_instances_of_same_model_are_isolated():
    broker = StateBroker()
    one = PineExecutionEngine(broker, instance_id="one")
    two = PineExecutionEngine(broker, instance_id="two")
    for engine in (one, two):
        engine.start([SYM], 100, {SYM: 1})
        run(engine.on_price(SYM, 100, _e9(100, 1, 1), 600))
    run(one.close_symbol(SYM, 100))
    assert broker.lots["lot-2"].state == "ACTIVE"


def test_runtime_uses_each_models_snapshot_keys_and_latest_price():
    broker = StateBroker()
    market = {
        SYM: {
            "bar": {"time": 1000, "close": 100, "high": 200, "low": 50},
            "bar_c": {"time": 700, "close": 100},
            "e9": _e9(100, 1, 1),
            "e9_c": {"direction": 1},
            "fixture": fixture_output(),
            "fixture_c": {"bias": 1},
        }
    }
    collector = SimpleNamespace(latest=lambda: market)
    manager = SimpleNamespace(list_lots=lambda **kw: [])
    service = AutoService(
        broker, collector, manager, registry=registry(), max_instances=5
    )
    service.start("ema9_pyramid", [SYM], {"initial_risk_usd": 100})
    service.start("fixture_pullback", [SYM], {"initial_risk_usd": 100})
    runtime = StrategyRuntime(service)
    run(runtime.step(market, 600, trading=True))
    run(runtime.step(market, 600, trading=True))
    assert len(broker.calls) == 2
    assert all(row["mfe_r"] == 0 for row in service.instances())
    assert len(runtime.last_bars) == 2


def test_start_preview_uses_selected_model_for_qqq():
    market = {
        SYM: {"fixture": fixture_output(direction=-1)},
        "NASDAQ:QQQ": {"fixture": fixture_output(direction=-1), "e9": {"direction": 1}},
    }
    service = AutoService(StateBroker(), SimpleNamespace(latest=lambda: market), None, registry=registry())
    preview = service.start("fixture_pullback", [SYM], {}, preview=True)
    assert preview["directions"] == {SYM: -1, "NASDAQ:QQQ": -1}
    assert not service.engines()


def test_registry_rejects_collisions_and_unregistered_models():
    models = registry()
    with pytest.raises(ValueError, match="重复"):
        models.register(fixture_model())
    with pytest.raises(ValueError, match="核心行情"):
        models.register(
            replace(fixture_model(), strategy_id="bad_model", realtime_key="bar")
        )
    with pytest.raises(ValueError, match="冲突"):
        models.register(replace(fixture_model(), strategy_id="other_model"))
    service = AutoService(StateBroker(), None, None, registry=models)
    with pytest.raises(AutoServiceError, match="未注册"):
        service.engine("not_installed")


def test_all_models_share_instance_limit():
    market = {SYM: {"e9": _e9(100, 1, 1), "fixture": fixture_output()}}
    service = AutoService(
        StateBroker(),
        SimpleNamespace(latest=lambda: market),
        None,
        registry=registry(),
        max_instances=1,
    )
    assert service.start("ema9_pyramid", [SYM], {})["started"] == [SYM]
    assert service.start("fixture_pullback", [SYM], {})["skipped"] == [SYM]


def test_backtest_can_replay_another_pine_mapping():
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from engine.backtest_session import BacktestSession

    stamp = datetime(
        2026, 10, 30, 10, 0, tzinfo=ZoneInfo("America/New_York")
    ).timestamp()
    row = dict(
        time=stamp,
        open=100,
        high=101,
        low=99.5,
        close=100.5,
        volume=100,
        fixture=fixture_output(time=stamp),
    )
    session = BacktestSession(
        [row],
        SYM,
        "2026-10-30",
        "fixture_pullback",
        {"initial_risk_usd": 100},
        registry=registry(),
    )
    result = run(session.run_all())
    assert result["orders"][0]["entry_order_type"] == "MKT"
    assert result["orders"][0]["filled"]["px"] == 101  # first long-side extreme observation
    assert result["orders"][0]["qty"] == 50  # $100 / (101 observed - 99 stop)
    assert result["orders"][0]["pnl"] == -25
    assert result["params"]["risk_ratios"] == [1.0]


def test_runtime_waits_for_confirmed_indicator_alignment():
    broker = StateBroker()
    market = {
        SYM: {
            "bar": {"time": 1300, "close": 100},
            "bar_c": {"time": 1000, "close": 100},
            "fixture": fixture_output(time=1000),
            "fixture_c": fixture_output(time=700, direction=-1),
        }
    }
    service = AutoService(
        broker,
        SimpleNamespace(latest=lambda: market),
        SimpleNamespace(list_lots=lambda **kw: []),
        registry=registry(),
    )
    service.start("fixture_pullback", [SYM], {"initial_risk_usd": 100})
    runtime = StrategyRuntime(service)
    run(runtime.step(market, 600, trading=True))
    assert broker.calls == []
    assert runtime.last_bars == {}
    market[SYM]["fixture"] = fixture_output(time=1300)
    market[SYM]["fixture_c"] = fixture_output(time=1000)
    run(runtime.step(market, 600, trading=True))
    assert len(broker.calls) == 1
    assert len(runtime.last_bars) == 1


def test_recovery_does_not_act_on_a_misaligned_confirmed_direction():
    broker = StateBroker()
    producer = PineExecutionEngine(broker, model=fixture_model())
    producer.start([SYM], 100, {SYM: 1})
    run(producer.on_price(SYM, 100, fixture_output(), 600))
    row = row_for(broker.lots["lot-1"]) | {"limit_type": "auto_fixture_pullback"}
    market = {
        SYM: {
            "bar": {"time": 1300, "close": 100},
            "bar_c": {"time": 1000, "close": 100},
            "fixture_c": fixture_output(time=700, direction=-1),
        }
    }
    service = AutoService(
        broker,
        SimpleNamespace(latest=lambda: market),
        SimpleNamespace(list_lots=lambda **kw: [row]),
        registry=registry(),
    )
    runtime = StrategyRuntime(service)
    runtime.adopt_tick = 4
    run(runtime.step(market, 600, trading=True))
    assert broker.lots["lot-1"].state == "ACTIVE"


def test_opening_range_blocks_entry_without_consuming_same_bar_signal(monkeypatch):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    t = datetime(2026, 11, 3, 9, 50, tzinfo=ZoneInfo("America/New_York")).timestamp()
    monkeypatch.setattr("console.runtime.strategy.time.time", lambda: t + 1)
    broker = StateBroker()
    engine = PineExecutionEngine(broker)
    engine.start([SYM], 100, {SYM: 1})
    runtime = StrategyRuntime(SimpleNamespace(
        engines=lambda: [engine],
        record_event=lambda *args, **kwargs: None,
    ), require_opening_range=True)
    data = {
        "bar": {"time": t, "close": 100},
        "bar_c": {"time": t - 300, "close": 100},
        "e9": _e9(100, 1, 1, t),
        "attack_c": {"time": t - 300, "opening3High": 101, "opening3Low": 99},
    }
    run(runtime.step({SYM: data}, 590, trading=True))
    assert not broker.calls
    assert "小区间" in engine.get_instance(SYM).last_decision_reason
    data["bar"]["close"] = 102
    run(runtime.step({SYM: data}, 590, trading=True))
    run(runtime.step({SYM: data}, 590, trading=True))
    assert len(broker.calls) == 1


def test_service_restores_same_session_watch_and_position_only():
    from datetime import date, datetime
    from zoneinfo import ZoneInfo

    broker = StateBroker()
    manager = SimpleNamespace(
        list_lots=lambda: [row_for(lot) for lot in broker.lots.values()],
        pyramid_states=broker.pyramid_states,
    )
    first = AutoService(broker, None, manager)
    engine = first.engine()
    symbols = [SYM, "NASDAQ:AMD", "NASDAQ:STOP", "NASDAQ:OLD"]
    engine.start(symbols, 700, {symbol: 1 for symbol in symbols})
    run(engine.on_price(SYM, 100, _e9(100, 1, 1), 600))
    stamp = datetime(2026, 11, 3, 10, tzinfo=ZoneInfo("America/New_York")).timestamp()
    for symbol in symbols:
        inst = engine.get_instance(symbol)
        inst.started_at = stamp if symbol != "NASDAQ:OLD" else stamp - 86400
        engine._persist(inst)
    engine.stop_symbol("NASDAQ:STOP")

    restarted = AutoService(broker, None, manager)
    run(restarted.restore(date(2026, 11, 3)))
    restored = {row["symbol"]: row for row in restarted.instances()}
    assert set(restored) == {SYM, "NASDAQ:AMD"}
    assert all(row["restored"] and row["enabled"] for row in restored.values())
    assert restored["NASDAQ:AMD"]["start_direction"] == 1
    assert restored["NASDAQ:AMD"]["initial_risk_usd"] == 700
    assert restored[SYM]["lot_ids"] == ["lot-1"]
    assert len(broker.calls) == 1
