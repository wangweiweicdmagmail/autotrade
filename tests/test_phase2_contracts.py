"""Phase 2: input time, cumulative fills, command arbitration and ownership races."""

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from console.runtime.strategy import StrategyRuntime
from console.services.auto import AutoService
from domain.contracts import (
    ClosedBar,
    FillEvent,
    OrderPlan,
    OrderState,
    PriceObservation,
    StrategyOutput,
    TradingGroupKey,
)
from engine.pine_execution import PineExecutionEngine
from engine.pine_models import PineOutput
from execution.operations import OperationKind, OrderOperationCoordinator
from execution.order_lot import OrderManager
from execution.risk import RiskConfig, RiskGate

from .conftest import run
from .test_order_lot import FakeGW, FakeTrade, attach, mk_lot, mk_mgr
from .test_pine_execution import fixture_model, fixture_output, registry
from .test_pyramid_safety import PendingBroker, StateBroker
from .test_tv_data_health import _FakeGateway

SYM = "NASDAQ:NVDA"


def test_price_clocks_are_not_inferred_from_the_bar_timestamp():
    observation = PriceObservation.from_market(
        SYM,
        {
            "bar": {"time": 1000, "close": 100},
            "price_observed_at": 1250,
        },
        received_at=1255,
    )
    assert observation.bar_open_time == 1000
    assert observation.observed_at == 1250
    assert observation.received_at == 1255
    assert observation.market_time is None
    fallback = PriceObservation.from_market(SYM, {"bar": {"close": 100}}, 1255)
    assert fallback.time_basis == "runtime_received"


@pytest.mark.parametrize("invalid", [float("nan"), float("inf"), -1, True])
def test_non_finite_and_invalid_prices_are_rejected(invalid):
    with pytest.raises(ValueError):
        PriceObservation.from_market(SYM, {"bar": {"close": invalid}}, 1000)


def test_plan_rounding_risk_and_ownership_match_the_existing_execution_rules():
    key = TradingGroupKey("model", "instance", SYM, "round")
    plan = OrderPlan.sized(SYM, "long", 102, 99, 200, group=key)
    assert (plan.qty, plan.planned_risk) == (66, 198)
    assert plan.group.metadata()["strategy_instance_id"] == "instance"
    marketable = OrderPlan(SYM, "long", 100, 101, 99, 100, 100)
    assert marketable.planned_risk == 100  # premium limit is not the risk anchor
    assert PineOutput is StrategyOutput
    assert OrderState.from_legacy("DONE") is OrderState.CLOSED


def test_fill_delta_is_cumulative_idempotent_and_keeps_broker_time_separate():
    trade = FakeTrade(1, filled=10, avg=100)
    trade.fills = [SimpleNamespace(time=datetime(2026, 10, 30, tzinfo=UTC))]
    event = FillEvent.from_trade("lot", "trail", trade, 2000)
    assert event.source == "protective"
    assert event.broker_time != event.received_at
    assert event.delta_from(10, 100).quantity == 0
    assert event.delta_from(4, 99).amount == 604
    with pytest.raises(ValueError, match="回退"):
        event.delta_from(11, 100)


def test_entry_fill_repetition_after_partial_exit_cannot_restore_sold_shares(tmp_path):
    manager = mk_mgr(FakeGW(), tmp_path, "partial-entry")
    lot = mk_lot(state="ACTIVE")
    lot.filled_qty, lot.avg_fill = 100, 100
    trade = FakeTrade(1, filled=100, avg=100)
    attach(manager, lot, entry=trade)
    assert manager._record_entry_fill(lot)
    manager._apply_partial_fill(lot, 50, 101, "partial")
    assert lot.filled_qty == 50
    assert not manager._record_entry_fill(lot)
    assert lot.filled_qty == 50
    trade.orderStatus.filled = 110
    trade.orderStatus.avgFillPrice = 100.5
    assert manager._record_entry_fill(lot)
    assert lot.filled_qty == 60  # only ten newly filled shares, not 110


def test_exit_preempts_queued_stop_and_breakeven_commands(tmp_path):
    async def scenario():
        gateway = FakeGW()
        gateway.enabled = gateway._connected = False
        manager = mk_mgr(gateway, tmp_path, "commands")
        lot = mk_lot(state="ACTIVE")
        lot.filled_qty, lot.avg_fill, lot.last_px = 100, 100, 101
        attach(manager, lot)
        lock = manager._lot_lock(lot)
        await lock.acquire()
        stop = asyncio.create_task(manager.update_trail(lot.lot_id, 99))
        await asyncio.sleep(0)
        close = asyncio.create_task(manager.close_lot(lot.lot_id, ref_px=101))
        await asyncio.sleep(0)
        breakeven = asyncio.create_task(manager.breakeven(lot.lot_id))
        await asyncio.sleep(0)
        lock.release()
        results = await asyncio.gather(stop, close, breakeven)
        assert not results[0][0] and "优先" in results[0][1]
        assert results[1][0]
        assert not results[2][0]
        assert lot.state == "CLOSED"
        assert gateway.stop_px_calls == []

    run(scenario())


def test_cancelled_exit_waiter_does_not_permanently_block_protection():
    async def scenario():
        coordinator, lock = OrderOperationCoordinator(), asyncio.Lock()
        await lock.acquire()

        async def request_exit():
            async with coordinator.run("lot", OperationKind.EXIT, lock=lock):
                pytest.fail("cancelled waiter must not execute")

        task = asyncio.create_task(request_exit())
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        lock.release()
        async with coordinator.run("lot", OperationKind.STOP, lock=lock) as decision:
            assert decision.allowed

    run(scenario())


def test_account_risk_is_reserved_before_another_entry_can_pass(tmp_path):
    async def scenario():
        class DelayedGateway(_FakeGateway):
            def __init__(self):
                super().__init__()
                self.entered, self.release = asyncio.Event(), asyncio.Event()

            async def place_limit_with_stop(self, *args):
                self.entered.set()
                await self.release.wait()
                return await super().place_limit_with_stop(*args)

        gateway = DelayedGateway()
        risk = RiskGate(
            RiskConfig(max_total_risk_usd=150, max_open_positions=0), halt_dir=tmp_path
        )
        manager = OrderManager(gateway, risk, tmp_path / "account.json")
        first = asyncio.create_task(
            manager.place_limit_stop_lot(SYM, "long", 100, 100, 99, risk_usd=100)
        )
        await gateway.entered.wait()
        second = asyncio.create_task(
            manager.place_limit_stop_lot(
                "NASDAQ:MSFT", "long", 100, 100, 99, risk_usd=100
            )
        )
        await asyncio.sleep(0)
        gateway.release.set()
        accepted, rejected = await asyncio.gather(first, second)
        assert accepted[0] is not None
        assert rejected[0] is None and "风险" in rejected[1]
        assert gateway.place_calls == 1

    run(scenario())


def runtime_fixture():
    market = {
        SYM: {
            "bar": {"time": 1300, "close": 100},
            "bar_c": {"time": 1000, "close": 100},
            "fixture": fixture_output(time=1300, signal=0),
        }
    }
    service = AutoService(
        StateBroker(),
        SimpleNamespace(latest=lambda: market),
        SimpleNamespace(list_lots=lambda **kw: []),
        registry=registry(),
    )
    engine = service.engine("fixture_pullback")
    return market, engine, StrategyRuntime(service)


def test_missing_confirmation_and_failed_dispatch_do_not_consume_bar_cursor():
    market, engine, runtime = runtime_fixture()
    run(runtime.step(market, 600, trading=True))
    assert runtime.last_bars == {}
    market[SYM]["fixture_c"] = fixture_output(time=1000)
    original = engine.on_confirmed_bar

    async def fail(*args):
        raise RuntimeError("temporary dispatch failure")

    engine.on_confirmed_bar = fail
    run(runtime.step(market, 600, trading=True))
    assert runtime.last_bars == {}
    engine.on_confirmed_bar = original
    run(runtime.step(market, 600, trading=True))
    assert list(runtime.last_bars.values()) == [1000]
    market[SYM]["bar_c"]["time"] = 700
    market[SYM]["fixture_c"]["time"] = 700
    run(runtime.step(market, 600, trading=True))
    assert list(runtime.last_bars.values()) == [1000]


@pytest.mark.parametrize("pending", [True, False])
def test_order_accepted_after_exit_request_is_still_cancelled_or_closed(pending):
    async def scenario():
        base = PendingBroker if pending else StateBroker

        class DelayedBroker(base):
            def __init__(self):
                super().__init__()
                self.entered, self.release = asyncio.Event(), asyncio.Event()

            async def place_limit_stop(self, **kwargs):
                self.entered.set()
                await self.release.wait()
                return await super().place_limit_stop(**kwargs)

        broker = DelayedBroker()
        engine = PineExecutionEngine(broker, model=fixture_model())
        engine.start([SYM], 100, {SYM: 1})
        first = asyncio.create_task(engine.on_price(SYM, 100, fixture_output(), 600))
        await broker.entered.wait()
        engine.stop_symbol(SYM, close_position=True)
        await engine.close_symbol(SYM, 100)
        broker.release.set()
        await first
        assert broker.lots["lot-1"].state == ("CANCELLED" if pending else "CLOSED")
        await engine.on_price(SYM, 100, fixture_output(time=1300, signal=0), 600)
        assert engine.get_instance(SYM).state == "DONE"

    run(scenario())


def test_confirmed_bar_is_explicitly_distinct_from_tick_time():
    bar = ClosedBar.from_mapping(
        SYM, {"time": 1000, "close": 100, "max": 101, "min": 99}
    )
    assert bar.close_time == 1300
    assert (bar.high, bar.low) == (101, 99)


def test_slow_placement_queue_keeps_original_receipt_time_for_duplicate_check(
    tmp_path, monkeypatch
):
    from execution import operations

    async def scenario():
        clock = [1000.0]
        monkeypatch.setattr(operations.time, "time", lambda: clock[0])

        class DelayedGateway(_FakeGateway):
            def __init__(self):
                super().__init__()
                self.entered, self.release = asyncio.Event(), asyncio.Event()

            async def place_limit_with_stop(self, *args):
                self.entered.set()
                await self.release.wait()
                return await super().place_limit_with_stop(*args)

        gateway = DelayedGateway()
        gate = RiskGate(RiskConfig(max_open_positions=0), halt_dir=tmp_path)
        manager = OrderManager(gateway, gate, tmp_path / "dedup.json")
        args = (SYM, "long", 10, 100, 99)
        first = asyncio.create_task(manager.place_limit_stop_lot(*args))
        await gateway.entered.wait()
        clock[0] = 1000.5
        second = asyncio.create_task(manager.place_limit_stop_lot(*args))
        await asyncio.sleep(0)
        clock[0] = (
            1005.0  # slow broker response does not turn double-click into a new intent
        )
        gateway.release.set()
        accepted, rejected = await asyncio.gather(first, second)
        assert accepted[0] is not None
        assert rejected[0] is None and "重复" in rejected[1]
        assert gateway.place_calls == 1

    run(scenario())


@pytest.mark.parametrize("restart", [True, False])
def test_inflight_entry_is_unique_and_late_acceptance_preserves_group_ownership(
    restart,
):
    async def scenario():
        class DelayedBroker(StateBroker):
            def __init__(self):
                super().__init__()
                self.entered, self.release = asyncio.Event(), asyncio.Event()

            async def place_limit_stop(self, **kwargs):
                self.entered.set()
                await self.release.wait()
                return await super().place_limit_stop(**kwargs)

        broker = DelayedBroker()
        engine = PineExecutionEngine(broker, model=fixture_model())
        engine.start([SYM], 100, {SYM: 1})
        original = engine.get_instance(SYM)
        first = asyncio.create_task(engine.on_price(SYM, 100, fixture_output(), 600))
        await broker.entered.wait()
        await engine.on_price(SYM, 100, fixture_output(time=1300), 600)
        engine.stop_symbol(SYM)
        if restart:
            engine.start([SYM], 100, {SYM: 1})
        broker.release.set()
        await first
        assert len(broker.calls) == 1
        if restart:
            assert engine.get_instance(SYM) is not original
            assert engine.get_instance(SYM).lot_ids == []
            assert broker.lots["lot-1"].state == "CLOSED"
        else:
            assert broker.lots["lot-1"].state == "ACTIVE"

    run(scenario())


def test_live_and_simulated_ports_reject_non_finite_plan_and_share_stop_rule(tmp_path):
    from engine.brokers.live import LiveBroker
    from engine.brokers.simulated import SimBroker

    async def scenario():
        manager = mk_mgr(FakeGW(), tmp_path, "port-contract")
        simulated = SimBroker()
        for broker in (LiveBroker(manager), simulated):
            with pytest.raises(ValueError):
                await broker.place_limit_stop(SYM, "long", 10, float("nan"), 99)
        lot = await simulated.place_limit_stop(SYM, "long", 10, 100, 99)
        assert not await simulated.move_stop(lot.lot_id, float("nan"))
        assert not await simulated.move_stop(lot.lot_id, 98)
        assert await simulated.move_stop(lot.lot_id, 99.126)
        assert lot.stop_px == 99.13
        live = mk_lot(state="ACTIVE")
        attach(manager, live)
        assert not (await manager.update_trail(live.lot_id, "invalid"))[0]

    run(scenario())
