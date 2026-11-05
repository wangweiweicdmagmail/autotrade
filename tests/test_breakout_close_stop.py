"""Signal-open initial stop and signal-close timed protection, isolated state."""
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from execution.order_lot import OrderManager
from .conftest import run
from .test_order_lot import FakeGW, FakeTrade, mk_lot, mk_mgr, attach, alive_trail, cancel_watchers

ENTRY_BAR = 1793804100  # 2026-11-04 09:55 ET
SIGNAL_BAR = ENTRY_BAR - 300


def setup_lot(tmp_path, side="long"):
    gw = FakeGW()
    manager = mk_mgr(gw, tmp_path, "signal-close")
    lot = mk_lot(state="ACTIVE", side=side, stop_px=98 if side == "long" else 102)
    lot.filled_qty = 40
    lot.avg_fill = 100.03 if side == "long" else 99.97
    lot.entry_broker_time = ENTRY_BAR + 2
    lot.entry_time = ENTRY_BAR + 4
    lot.signal_snapshot = {
        "opening_breakout_id": "signal-1", "breakout_signal_bar_time": SIGNAL_BAR,
        "breakout_signal_open": 98 if side == "long" else 102,
        "breakout_signal_close": 100,
        "breakout_protection_rule": "signal_open_then_close_5m",
    }
    attach(manager, lot, trail=alive_trail(lot, qty=40))
    return manager, gw, lot


def snapshot(*, now=ENTRY_BAR+302, price=102):
    bar_time = now // 300 * 300
    return {
        "bar": {"time": bar_time, "open": 105, "close": price},
        "bar_c": {"time": bar_time-300, "open": 104, "close": 104.5},
    }


@pytest.mark.parametrize("side", ["long", "short"])
def test_signal_close_is_fixed_and_applies_at_entry_candle_close(tmp_path, side):
    async def go():
        manager, gw, lot = setup_lot(tmp_path, side)
        data = snapshot(now=ENTRY_BAR+299)
        assert (await manager.protect_breakout_signal_close(lot.lot_id, data, now=ENTRY_BAR+299))["action"] == "waiting"
        assert gw.stop_px_calls == []
        anchor = lot.signal_snapshot["breakout_close_protection"]
        assert anchor["target_stop"] == 100
        assert anchor["due_at"] == ENTRY_BAR+300
        recovered = OrderManager(FakeGW(), object(), manager._state_path)
        restored = recovered.get(lot.lot_id)
        restored.trail_trade = alive_trail(restored, qty=40)
        # Neither later price data nor a revised signal snapshot may move the pinned target.
        restored.signal_snapshot["breakout_signal_close"] = 101
        data = snapshot(price=102 if side == "long" else 98)
        outcome = await recovered.protect_breakout_signal_close(lot.lot_id, data, now=ENTRY_BAR+300)
        assert outcome["action"] == "stop_updated"
        assert restored.stop_px == 100
        assert not restored.breakeven_reached  # Slight fill slippage can remain.
        assert restored.signal_snapshot["breakout_close_protection"]["done"]
        assert restored.init_stop_px == lot.init_stop_px
        assert restored.init_entry_px == lot.init_entry_px
        assert restored.trail_order_id == lot.trail_order_id
        assert recovered._gw.stop_only_calls == []
        again = await recovered.protect_breakout_signal_close(lot.lot_id, data, now=ENTRY_BAR+303)
        assert again["action"] == "already"
        assert recovered._gw.stop_px_calls == [100]
    run(go())


@pytest.mark.parametrize("side", ["long", "short"])
def test_crossed_signal_close_exits_only_actual_remainder_once(tmp_path, side):
    async def go():
        manager, gw, lot = setup_lot(tmp_path, side)
        data = snapshot(price=99 if side == "long" else 101)
        result = await manager.protect_breakout_signal_close(lot.lot_id, data, now=ENTRY_BAR+302)
        assert result["action"] == "close_submitted"
        assert gw.stop_px_calls == []
        assert len(gw.close_calls) == 1 and gw.close_calls[0][2] == 40
        assert lot.state == "CLOSING"
        assert lot.close_reason == "breakout_signal_close_stop"
        await manager.protect_breakout_signal_close(lot.lot_id, data, now=ENTRY_BAR+303)
        assert len(gw.close_calls) == 1
        cancel_watchers(manager)
    run(go())


@pytest.mark.parametrize("side", ["long", "short"])
def test_tighter_existing_stop_is_kept_even_after_cost_breakeven(tmp_path, side):
    async def go():
        manager, gw, lot = setup_lot(tmp_path, side)
        lot.stop_px = 101 if side == "long" else 99
        lot.breakeven_reached = True
        result = await manager.protect_breakout_signal_close(
            lot.lot_id, snapshot(price=103 if side == "long" else 97), now=ENTRY_BAR+302,
        )
        assert result["action"] == "stop_kept"
        assert gw.stop_px_calls == []
    run(go())


def test_legacy_order_recovers_signal_close_from_history_and_ignores_old_first_bar_anchor(tmp_path):
    async def go():
        manager, gw, lot = setup_lot(tmp_path)
        lot.signal_snapshot = {
            "opening_breakout_id": "old", "stop_bar_time": SIGNAL_BAR,
            "breakout_first_bar": {"bar_time": ENTRY_BAR, "open": 105, "done": True},
        }
        data = snapshot()
        data["opening_breakout_history"] = [[SIGNAL_BAR, 98, 101, 97, 100]]
        assert (await manager.protect_breakout_signal_close(lot.lot_id, data, now=ENTRY_BAR+302))["action"] == "stop_updated"
        assert lot.stop_px == 100
    run(go())


@pytest.mark.parametrize("problem", ["missing_signal_close", "missing_signal_time", "stale_bar", "stale_price", "partial_exit"])
def test_missing_or_stale_evidence_never_amends_or_exits(tmp_path, problem):
    async def go():
        manager, gw, lot = setup_lot(tmp_path)
        data = snapshot()
        now = ENTRY_BAR+302
        if problem == "missing_signal_close":
            del lot.signal_snapshot["breakout_signal_close"]
        elif problem == "missing_signal_time":
            del lot.signal_snapshot["breakout_signal_bar_time"]
        elif problem == "stale_bar":
            data["bar"]["time"] = ENTRY_BAR
        elif problem == "stale_price":
            data["price_observed_at"] = now-16
        else:
            lot.partial_exit = {"phase": "pending"}
        await manager.protect_breakout_signal_close(lot.lot_id, data, now=now)
        assert gw.stop_px_calls == gw.close_calls == []
    run(go())


def test_filled_original_stop_prevents_duplicate_exit(tmp_path):
    async def go():
        manager, gw, lot = setup_lot(tmp_path)
        lot.trail_trade = FakeTrade(8802, status="Filled", filled=40, avg=99, qty=40)
        result = await manager.protect_breakout_signal_close(
            lot.lot_id, snapshot(price=99), now=ENTRY_BAR+302,
        )
        assert not result["ok"] and gw.close_calls == []
        cancel_watchers(manager)
    run(go())


def test_failed_amendment_retries_pinned_signal_close(tmp_path):
    async def go():
        manager, gw, lot = setup_lot(tmp_path)
        original = gw.modify_stop
        calls = 0

        async def fail_once(unit, target):
            nonlocal calls
            calls += 1
            return False if calls == 1 else await original(unit, target)

        gw.modify_stop = fail_once
        assert not (await manager.protect_breakout_signal_close(lot.lot_id, snapshot(), now=ENTRY_BAR+302))["ok"]
        assert not lot.signal_snapshot["breakout_close_protection"]["done"]
        lot.signal_snapshot["breakout_signal_close"] = 101
        result = await manager.protect_breakout_signal_close(lot.lot_id, snapshot(), now=ENTRY_BAR+307)
        assert result["target_stop"] == lot.stop_px == 100
    run(go())


def test_missing_broker_time_uses_fill_receipt_bar_close(tmp_path):
    async def go():
        manager, gw, lot = setup_lot(tmp_path)
        lot.entry_broker_time = 0
        assert (await manager.protect_breakout_signal_close(lot.lot_id, snapshot(now=ENTRY_BAR+299), now=ENTRY_BAR+299))["action"] == "waiting"
        anchor = lot.signal_snapshot["breakout_close_protection"]
        assert anchor["entry_time_basis"] == "received"
        assert anchor["due_at"] == ENTRY_BAR+300
    run(go())


def test_partial_fills_start_timer_at_earliest_broker_fill(tmp_path):
    async def go():
        manager, gw, lot = setup_lot(tmp_path)
        lot.entry_broker_time = ENTRY_BAR+301
        lot.entry_time = ENTRY_BAR+302
        lot.entry_trade = SimpleNamespace(fills=[
            SimpleNamespace(time=datetime.fromtimestamp(ENTRY_BAR+301, timezone.utc)),
            SimpleNamespace(time=datetime.fromtimestamp(ENTRY_BAR+2, timezone.utc)),
        ])
        result = await manager.protect_breakout_signal_close(lot.lot_id, snapshot(), now=ENTRY_BAR+302)
        assert result["action"] == "stop_updated"
        assert lot.signal_snapshot["breakout_close_protection"]["entry_time"] == ENTRY_BAR+2
    run(go())


def test_pending_older_timer_is_migrated_to_candle_close_without_changing_target(tmp_path):
    async def go():
        manager, gw, lot = setup_lot(tmp_path)
        lot.signal_snapshot["breakout_close_protection"] = {
            "signal_bar_time": SIGNAL_BAR, "target_stop": 100,
            "entry_time": ENTRY_BAR+120, "due_at": ENTRY_BAR+420, "done": False,
        }
        result = await manager.protect_breakout_signal_close(
            lot.lot_id, snapshot(now=ENTRY_BAR+300), now=ENTRY_BAR+300,
        )
        assert result["action"] == "stop_updated"
        assert result["due_at"] == ENTRY_BAR+300
        assert lot.stop_px == 100
    run(go())


@pytest.mark.parametrize('side', ['long','short'])
def test_current_timeout_runs_even_after_first_close_protection_done(tmp_path, side):
    from engine.opening_breakout import RULES_VERSION
    async def go():
        manager, gw, lot = setup_lot(tmp_path, side)
        lot.signal_snapshot['breakout_rules_version'] = RULES_VERSION
        data = snapshot(price=102 if side == 'long' else 98)
        assert (await manager.protect_breakout_signal_close(lot.lot_id, data, now=ENTRY_BAR+302))['action'] == 'stop_updated'
        later = ENTRY_BAR+900
        result = await manager.protect_breakout_signal_close(lot.lot_id, snapshot(now=later), now=later)
        assert result['action'] == 'close_submitted'
        assert lot.close_reason == 'breakout_three_bar_timeout'
        await manager.protect_breakout_signal_close(lot.lot_id, snapshot(now=later), now=later)
        assert len(gw.close_calls) == 1
        cancel_watchers(manager)
    run(go())
