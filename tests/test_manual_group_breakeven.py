"""Manual group protection uses in-memory brokers and isolated HTTP services."""

import asyncio
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from engine.pine_execution import PineExecutionEngine
from console.services.auto import AutoServiceError
from console.dependencies import require_trader
from .conftest import run
from .test_pine_execution import fixture_model
from .test_pyramid_safety import StateBroker

SYM = "NASDAQ:NVDA"


class Broker(StateBroker):
    def __init__(self):
        super().__init__()
        self.stop_calls = []
        self.fail = set()

    async def move_stop(self, lot_id, new_stop):
        self.stop_calls.append((lot_id, new_stop))
        if lot_id in self.fail:
            return False
        return await super().move_stop(lot_id, new_stop)


def setup(side="long", stopped=False):
    broker = Broker()
    engine = PineExecutionEngine(broker, model=fixture_model())
    engine.start([SYM], 100)
    inst = engine.get_instance(SYM)
    inst.side = side
    inst.entries = 2
    inst.enabled = not stopped
    inst.lot_ids = ["first", "second"]
    for lot_id, cost in [("first", 100), ("second", 102)]:
        broker.lots[lot_id] = SimpleNamespace(
            lot_id=lot_id, symbol=SYM, side=side, qty=100, filled_qty=100,
            state="ACTIVE", avg_fill=cost, entry_px=cost, stop_px=99 if side == "long" else 103)
    return engine, broker, inst


@pytest.mark.parametrize("side", ["long", "short"])
def test_manual_group_uses_weighted_cost_and_keeps_better_stop(side):
    engine, broker, inst = setup(side, stopped=True)
    broker.lots["second"].stop_px = 102 if side == "long" else 100
    result = run(engine.manual_breakeven(SYM, inst.group_id, 200, 101))
    assert result["ok"] and result["target_stop"] == 101
    assert result["updated"] == ["first"] and result["unchanged"] == ["second"]
    assert broker.stop_calls == [("first", 101)]
    assert inst.breakeven_done
    assert not inst.enabled  # Manual protection does not resume entries.
    again = run(engine.manual_breakeven(SYM, inst.group_id, 200, 101))
    assert again["ok"] and again["updated"] == []
    assert len(broker.stop_calls) == 1


@pytest.mark.parametrize("group,qty,cost", [("obsolete", 200, 101), (None, 199, 101), (None, 200, 100)])
def test_stale_confirmation_never_changes_orders(group, qty, cost):
    engine, broker, inst = setup()
    with pytest.raises(ValueError, match="变化"):
        run(engine.manual_breakeven(SYM, group or inst.group_id, qty, cost))
    assert broker.stop_calls == []


def test_partial_failure_is_explicit_and_retry_preserves_success():
    engine, broker, inst = setup()
    broker.fail.add("second")
    first = run(engine.manual_breakeven(SYM, inst.group_id, 200, 101))
    assert not first["ok"] and first["updated"] == ["first"]
    assert first["failed"][0]["lot_id"] == "second"
    assert broker.lots["first"].stop_px == 101 and not inst.breakeven_done
    broker.fail.clear()
    second = run(engine.manual_breakeven(SYM, inst.group_id, 200, 101))
    assert second["ok"] and second["unchanged"] == ["first"]
    assert second["updated"] == ["second"]


def test_group_cost_is_weighted_by_actual_remaining_fills():
    engine, broker, inst = setup()
    broker.lots["first"].qty = broker.lots["first"].filled_qty = 300
    result = run(engine.manual_breakeven(SYM, inst.group_id, 400, 100.5))
    assert result["ok"] and result["target_stop"] == 100.5
    assert broker.stop_calls == [("first", 100.5), ("second", 100.5)]


@pytest.mark.parametrize("kind", ["pending", "missing_cost", "partial_exit", "unconfirmed_entry", "recovery", "missing_lot"])
def test_unsettled_orders_reject_before_protection_changes(kind):
    engine, broker, inst = setup()
    lot = broker.lots["second"]
    if kind == "pending":
        lot.state = "PENDING"
        inst.pending_lot_id = lot.lot_id
    elif kind == "missing_cost":
        lot.avg_fill = 0
    elif kind == "partial_exit":
        lot.partial_exit = {"qty": 50}
    elif kind == "recovery":
        inst.recovery_block_reason = "待核验"
    elif kind == "missing_lot":
        del broker.lots["second"]
    else:
        lot.entry_trade = SimpleNamespace(isDone=lambda: False)
    with pytest.raises(ValueError):
        run(engine.manual_breakeven(SYM, inst.group_id, 200, 101))
    assert broker.stop_calls == []


def test_manual_protection_blocks_entries_and_exit_stops_remaining_modifications():
    async def scenario():
        engine, broker, inst = setup()
        entered, release = asyncio.Event(), asyncio.Event()
        original = broker.move_stop

        async def delayed(lot_id, price):
            if lot_id == "first":
                entered.set()
                await release.wait()
            return await original(lot_id, price)

        broker.move_stop = delayed
        task = asyncio.create_task(engine.manual_breakeven(SYM, inst.group_id, 200, 101))
        await entered.wait()
        entry_calls = []

        async def unexpected_entry(*_args):
            entry_calls.append(True)

        engine._place_once = unexpected_entry
        await engine._place(inst, {}, 600, 105)
        assert entry_calls == []
        with pytest.raises(ValueError, match="正在"):
            await engine.manual_breakeven(SYM, inst.group_id, 200, 101)
        await engine.close_symbol(SYM, ref_px=105)
        release.set()
        result = await task
        assert not result["ok"]
        assert [lot_id for lot_id, _price in broker.stop_calls] == ["first"]
        assert not engine._manual_protection_inflight

    run(scenario())


def test_instance_endpoint_keeps_scope_and_surfaces_conflict(console_app, monkeypatch):
    console_app.app.dependency_overrides[require_trader] = lambda: "master"
    calls = []

    async def fake(*args):
        calls.append(args)
        return {"ok": True, "target_stop": 101, "updated": ["one"], "unchanged": [], "failed": []}

    monkeypatch.setattr(console_app.auto_service, "breakeven_instance", fake)
    client = TestClient(console_app.app)
    path = "/api/auto/instances/NASDAQ:NVDA/breakeven?strategy=fixture_pullback&instance_id=second"
    response = client.post(path, json={"group_id": "group-a", "qty": 200, "avg_cost": 101})
    assert response.status_code == 200 and response.json()["ok"]
    assert calls == [(SYM, "group-a", 200, 101, "fixture_pullback", "second")]

    async def stale(*_args):
        raise AutoServiceError(409, "交易组已变化")

    monkeypatch.setattr(console_app.auto_service, "breakeven_instance", stale)
    assert client.post(path, json={"group_id": "old", "qty": 200, "avg_cost": 101}).status_code == 409
    assert client.post(path, json={"qty": 200, "avg_cost": 101}).status_code == 422
