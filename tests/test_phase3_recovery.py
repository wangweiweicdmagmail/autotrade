"""Crash, migration and broker uncertainty regressions. All state is under tmp_path."""

import asyncio
import json
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path

import pytest

from execution.order_lot import OrderManager
from execution.risk import RiskConfig, RiskGate
from execution.state_store import ExecutionStateStore, PersistenceError

from .conftest import run
from .test_order_lot import FakeGW, FakeTrade, attach, cancel_watchers, mk_lot, mk_mgr
from .test_tv_data_health import _FakeGateway


def payload(entries=1):
    return {
        "lots": [mk_lot().to_dict()],
        "pyramid_groups": {"group": {"entries": entries}},
    }


def test_legacy_migration_backs_up_and_reads_authoritative_snapshot(tmp_path):
    path = tmp_path / "lots.json"
    original = json.dumps(payload())
    path.write_text(original)
    store = ExecutionStateStore(path)
    assert store.load()["pyramid_groups"]["group"]["entries"] == 1
    assert not store.path.exists()  # read-only construction does not migrate
    store.save(payload(2))
    assert path.with_suffix(".json.pre-v1.bak").read_text() == original
    path.write_text("broken projection")
    loaded = ExecutionStateStore(path)
    assert loaded.load()["pyramid_groups"]["group"]["entries"] == 2
    assert loaded.revision == 1


@pytest.mark.parametrize(
    "bad", ["{", "[]", '{"lots":{}}', '{"lots":[],"schema_version":99}']
)
def test_invalid_legacy_never_becomes_an_empty_account(tmp_path, bad):
    path = tmp_path / "lots.json"
    path.write_text(bad)
    with pytest.raises(PersistenceError):
        OrderManager(FakeGW(), object(), path)
    assert path.read_text() == bad
    assert not path.with_suffix(".sqlite3").exists()


def test_missing_authoritative_database_cannot_fall_back_to_projection(tmp_path):
    path = tmp_path / "lots.json"
    store = ExecutionStateStore(path)
    store.save(payload())
    store.path.unlink()
    with pytest.raises(PersistenceError, match="权威账本缺失"):
        ExecutionStateStore(path).load()


@pytest.mark.parametrize("future", [True, False])
def test_unknown_schema_or_checksum_corruption_blocks_load(tmp_path, future):
    store = ExecutionStateStore(tmp_path / "lots.json")
    store.save(payload())
    with closing(sqlite3.connect(store.path)) as connection:
        if future:
            connection.execute("PRAGMA user_version=99")
        else:
            connection.execute("UPDATE snapshot SET payload='{}'")
        connection.commit()
    with pytest.raises(PersistenceError):
        ExecutionStateStore(store.legacy_path).load()


def test_transaction_failure_keeps_state_and_archive_together_then_retries(tmp_path):
    store = ExecutionStateStore(tmp_path / "lots.json")
    store.save(payload())
    row = mk_lot(state="CLOSED").to_dict()
    row["closed_at"] = 42

    def fail(_connection):
        raise OSError("disk full")

    store._before_commit = fail
    with pytest.raises(PersistenceError):
        store.save(payload(2), archive=("20261102", row))
    loaded = ExecutionStateStore(store.legacy_path)
    assert loaded.load()["pyramid_groups"]["group"]["entries"] == 1
    assert loaded.archive_records() == []
    store._before_commit = lambda connection: None
    store.save(payload(2), archive=("20261102", row))
    store.save(payload(2), archive=("20261102", row))
    assert len(store.archive_records()) == 1
    assert loaded.load()["pyramid_groups"]["group"]["entries"] == 2


@pytest.mark.parametrize("existing", [True, False])
def test_process_death_before_commit_recovers_previous_snapshot(tmp_path, existing):
    path = tmp_path / "lots.json"
    path.write_text(json.dumps({"lots": [], "pyramid_groups": {"before": True}}))
    store = ExecutionStateStore(path)
    if existing:
        store.save(store.load())
    script = """
import os, sys
from execution.state_store import ExecutionStateStore
store = ExecutionStateStore(sys.argv[1])
store.load()
store._before_commit = lambda connection: os._exit(23)
store.save({"lots": [], "pyramid_groups": {"after": True}})
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(path)],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        check=False,
    )
    assert result.returncode == 23, result.stderr
    loaded = ExecutionStateStore(path)
    assert loaded.load(allow_recovery=True)["pyramid_groups"] == {"before": True}
    loaded.save({"lots": [], "pyramid_groups": {"retry": True}})
    assert loaded.load()["pyramid_groups"] == {"retry": True}


def test_projection_failure_is_visible_but_committed_ledger_is_used(
    tmp_path, monkeypatch
):
    from execution import state_store

    store = ExecutionStateStore(tmp_path / "lots.json")
    store.save(payload())
    monkeypatch.setattr(
        state_store,
        "atomic_write",
        lambda *args: (_ for _ in ()).throw(OSError("export denied")),
    )
    row = mk_lot(state="CLOSED").to_dict()
    store.save(payload(2), archive=("20261102", row))
    assert "export denied" in store.export_error
    loaded = ExecutionStateStore(store.legacy_path)
    assert loaded.load()["pyramid_groups"]["group"]["entries"] == 2
    assert len(loaded.archive_records()) == 1


def test_stale_writer_cannot_overwrite_newer_snapshot(tmp_path):
    path = tmp_path / "lots.json"
    first, second = ExecutionStateStore(path), ExecutionStateStore(path)
    first.load()
    second.load()
    first.save(payload())
    with pytest.raises(PersistenceError, match="其他写入者"):
        second.save(payload(2))
    assert ExecutionStateStore(path).load()["pyramid_groups"]["group"]["entries"] == 1


def test_storage_failure_prevents_broker_submission_and_future_entries(tmp_path):
    async def scenario():
        gateway = _FakeGateway()
        manager = OrderManager(
            gateway, RiskGate(RiskConfig(), halt_dir=tmp_path), tmp_path / "lots.json"
        )
        manager._store._before_commit = lambda connection: (_ for _ in ()).throw(
            OSError("disk full")
        )
        with pytest.raises(PersistenceError):
            await manager.place_limit_stop_lot("NASDAQ:QCOM", "long", 10, 100, 99)
        assert gateway.place_calls == 0
        assert manager.recovery_status()["blocks_entry"]
        result = await manager.place_limit_stop_lot("NASDAQ:MSFT", "long", 10, 100, 99)
        assert result[0] is None
        assert gateway.place_calls == 0

    run(scenario())


def test_cancelled_submission_is_durable_and_unknown_result_is_not_cancelled(tmp_path):
    async def scenario():
        class HangingGateway(_FakeGateway):
            def __init__(self):
                super().__init__()
                self.entered = asyncio.Event()

            async def place_limit_with_stop(self, *args):
                self.entered.set()
                await asyncio.Event().wait()

        gateway = HangingGateway()
        gate = RiskGate(RiskConfig(), halt_dir=tmp_path)
        path = tmp_path / "lots.json"
        manager = OrderManager(gateway, gate, path)
        task = asyncio.create_task(
            manager.place_limit_stop_lot("NASDAQ:QCOM", "long", 10, 100, 99)
        )
        await gateway.entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        recovered = OrderManager(FakeGW(fills={}, positions={}), gate, path)
        await recovered.reconcile()
        lot = next(iter(recovered._lots.values()))
        assert lot.entry_submission_pending and lot.state == "PENDING"
        assert recovered.recovery_status()["blocks_entry"]
        assert recovered._gw.cancel_calls == [] and recovered._gw.close_calls == []

    run(scenario())


def test_exact_reference_recovers_pending_entry_and_repeated_reconcile_is_safe(
    tmp_path,
):
    async def scenario():
        gateway = FakeGW(fills={}, positions={})
        manager = mk_mgr(gateway, tmp_path, "reference")
        lot = mk_lot()
        lot.entry_order_id = lot.trail_order_id = None
        lot.entry_submission_pending = True
        attach(manager, lot)
        manager._save()
        entry = FakeTrade(101, qty=100)
        entry.order.orderRef = f"autotrade:{lot.lot_id}:entry"
        stop = FakeTrade(
            102, qty=100, action="SELL", order_type="STP", aux=95, parent_id=101
        )
        stop.order.orderRef = f"autotrade:{lot.lot_id}:stop"
        gateway._trades = {101: entry, 102: stop}
        recovered = OrderManager(gateway, object(), manager._state_path)
        await recovered.reconcile()
        await recovered.reconcile()
        row = recovered.get(lot.lot_id)
        assert not row.entry_submission_pending and row.entry_order_id == 101
        assert row.trail_order_id == 102 and row.state == "PENDING"
        assert not recovered.recovery_status()["blocks_entry"]
        cancel_watchers(recovered)

    run(scenario())


def test_interrupted_exit_recovers_exact_close_order_without_resubmission(tmp_path):
    async def scenario():
        gateway = FakeGW(fills={}, positions={"QCOM": 100})
        manager = mk_mgr(gateway, tmp_path, "close-ref")
        lot = mk_lot(state="CLOSING")
        lot.filled_qty = 100
        lot.avg_fill = 100
        lot.close_requested_qty = 100
        lot.close_submission_ref = "autotrade:lot-t:close:unique"
        trade = FakeTrade(201, qty=100, action="SELL")
        trade.order.orderRef = lot.close_submission_ref
        gateway._trades[201] = trade
        attach(manager, lot)
        manager._save()
        recovered = OrderManager(gateway, object(), manager._state_path)
        await recovered.reconcile()
        await recovered.reconcile()
        assert recovered.get(lot.lot_id).close_order_id == 201
        assert gateway.close_calls == []
        cancel_watchers(recovered)

    run(scenario())


def test_unavailable_snapshot_is_visible_and_recovers_on_retry(tmp_path):
    async def scenario():
        gateway = FakeGW(positions=None)
        manager = mk_mgr(gateway, tmp_path, "snapshot")
        lot = mk_lot()
        entry = FakeTrade(8801, qty=100)
        attach(manager, lot, entry=entry)
        gateway._trades[8801] = entry
        await manager.reconcile()
        assert manager.recovery_status()["phase"] == "unavailable"
        assert manager.recovery_status()["blocks_entry"]
        gateway._pos = {}
        await manager.reconcile()
        assert manager.recovery_status()["phase"] == "checked"
        assert not manager.recovery_status()["blocks_entry"]
        cancel_watchers(manager)

    run(scenario())


def test_partial_exit_restart_books_once_and_restores_cancelled_remainder(tmp_path):
    async def scenario():
        gateway = FakeGW(fills={}, positions={"QCOM": 80})
        manager = mk_mgr(gateway, tmp_path, "partial-restart")
        lot = mk_lot(state="ACTIVE")
        lot.filled_qty, lot.avg_fill = 100, 100
        lot.partial_exit = {"qty": 50, "ref": "autotrade:lot-t:partial:unique"}
        trade = FakeTrade(
            301, status="Cancelled", filled=20, avg=101, qty=50, action="SELL"
        )
        trade.order.orderRef = lot.partial_exit["ref"]
        stop = FakeTrade(
            8802, qty=50, action="SELL", order_type="STP", aux=95, parent_id=8801
        )
        gateway._trades = {301: trade, 8802: stop}
        attach(manager, lot, trail=stop)
        manager._save()
        recovered = OrderManager(gateway, object(), manager._state_path)
        await recovered.reconcile()
        await recovered.reconcile()
        row = recovered.get(lot.lot_id)
        assert row.filled_qty == 80 and row.close_filled_qty == 20
        assert row.realized_pnl == 20 and not row.partial_exit
        assert stop.order.totalQuantity == 80
        assert gateway.close_calls == []
        cancel_watchers(recovered)

    run(scenario())


def test_partial_unknown_price_waits_and_blocks_overlapping_exit(tmp_path):
    async def scenario():
        gateway = FakeGW(positions={"QCOM": 50})
        manager = mk_mgr(gateway, tmp_path, "partial-price")
        lot = mk_lot(state="ACTIVE")
        lot.filled_qty, lot.avg_fill = 100, 100
        lot.partial_exit = {"qty": 50, "ref": "partial"}
        trade = FakeTrade(301, status="Filled", filled=50, avg=0)
        attach(manager, lot)
        lot.close_trade = trade
        assert not await manager._handle_partial_terminal(lot, 50)
        assert lot.partial_exit and lot.close_filled_qty == 0
        assert not (await manager.close_lot(lot.lot_id, ref_px=101))[0]
        assert not (await manager.close_half(lot.lot_id, ref_px=101))[0]
        assert gateway.close_calls == []

    run(scenario())


def test_live_disconnect_does_not_fall_back_to_dry_run(tmp_path):
    async def scenario():
        gateway = _FakeGateway()
        gateway.enabled = True
        manager = OrderManager(
            gateway, RiskGate(RiskConfig(), halt_dir=tmp_path), tmp_path / "lots.json"
        )
        result = await manager.place_limit_stop_lot("NASDAQ:QCOM", "long", 10, 100, 99)
        assert result[0] is None and "未连接" in result[1]
        assert gateway.place_calls == 0

    run(scenario())


def test_failed_archive_commit_is_included_in_next_state_retry(tmp_path):
    manager = mk_mgr(FakeGW(), tmp_path, "archive-retry")
    lot = mk_lot(state="ACTIVE")
    lot.filled_qty, lot.avg_fill = 100, 100
    attach(manager, lot)
    manager._save()
    manager._store._before_commit = lambda connection: (_ for _ in ()).throw(
        OSError("disk full")
    )
    with pytest.raises(PersistenceError):
        manager._finalize_close(lot, 101, "manual")
    manager._store._before_commit = lambda connection: None
    manager._save()
    recovered = OrderManager(FakeGW(), object(), manager._state_path)
    assert recovered.get(lot.lot_id).state == "CLOSED"
    assert len(recovered._store.archive_records()) == 1


def test_legacy_archive_migration_preserves_records_without_retry_duplicates(tmp_path):
    path = tmp_path / "lots.json"
    path.write_text(json.dumps(payload()))
    row = mk_lot(state="CLOSED").to_dict()
    (tmp_path / "trades-20261102.json").write_text(json.dumps([row]))
    store = ExecutionStateStore(path)
    store.load()
    store.save(payload(), archive=("20261102", row))
    assert store.archive_records() == [row]


def test_death_after_commit_before_json_export_keeps_new_facts(tmp_path):
    path = tmp_path / "lots.json"
    store = ExecutionStateStore(path)
    store.save({"lots": [], "pyramid_groups": {"before": True}})
    script = """
import os, sys
from execution import state_store
store = state_store.ExecutionStateStore(sys.argv[1])
store.load()
state_store.atomic_write = lambda *args: os._exit(24)
store.save({"lots": [], "pyramid_groups": {"committed": True}})
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(path)],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        check=False,
    )
    assert result.returncode == 24, result.stderr
    assert json.loads(path.read_text())["pyramid_groups"] == {"before": True}
    loaded = ExecutionStateStore(path)
    assert loaded.load()["pyramid_groups"] == {"committed": True}


def test_conflicting_order_references_require_manual_review(tmp_path):
    async def scenario():
        gateway = FakeGW(fills={}, positions={})
        manager = mk_mgr(gateway, tmp_path, "duplicate-ref")
        lot = mk_lot()
        lot.entry_submission_pending = True
        lot.entry_order_id = None
        attach(manager, lot)
        for order_id in (101, 102):
            trade = FakeTrade(order_id)
            trade.order.orderRef = f"autotrade:{lot.lot_id}:entry"
            gateway._trades[order_id] = trade
        await manager.reconcile()
        assert lot.entry_submission_pending and lot.entry_order_id is None
        assert manager.recovery_status()["blocks_entry"]
        assert gateway.close_calls == [] and gateway.cancel_calls == []

    run(scenario())


def test_recovery_snapshot_exposes_actionable_reason(console_app):
    lot = mk_lot()
    lot.entry_submission_pending = True
    console_app.order_mgr._lots[lot.lot_id] = lot
    snapshot = run(console_app.snapshot.build_snapshot())
    recovery = snapshot["status"]["recovery"]
    assert recovery["blocks_entry"]
    assert any(issue.get("lot_id") == lot.lot_id for issue in recovery["issues"])
    assert recovery["schema_version"] == 1


@pytest.mark.parametrize("method", ["limit", "market", "oca"])
def test_real_gateway_marks_each_entry_leg_with_durable_reference(monkeypatch, method):
    from types import SimpleNamespace

    from execution import ibkr
    from execution.ibkr import IBKRGateway

    async def scenario():
        gateway = IBKRGateway("test", 0, 1, "", "", enabled=True)
        gateway._connected = True
        orders = []

        def place(contract, order):
            orders.append(order)
            return SimpleNamespace(
                order=order, orderStatus=SimpleNamespace(status="Submitted")
            )

        gateway.ib = SimpleNamespace(placeOrder=place)
        ids = iter(range(880001, 880010))
        monkeypatch.setattr(gateway, "_next_tool_order_id", lambda: next(ids))

        async def qualify(symbol):
            return SimpleNamespace(symbol="QCOM")

        async def no_sleep(seconds):
            pass

        monkeypatch.setattr(gateway, "_qualify", qualify)
        monkeypatch.setattr(ibkr.asyncio, "sleep", no_sleep)
        reference = "autotrade:lot-test"
        if method == "limit":
            result = await gateway.place_limit_with_stop(
                "NASDAQ:QCOM", "long", 10, 100, 99, order_ref=reference
            )
        elif method == "market":
            result = await gateway.place_market_with_stop(
                "NASDAQ:QCOM", "long", 10, 99, order_ref=reference
            )
        else:
            result = await gateway.place_midpoint_oca(
                "NASDAQ:QCOM", "long", 10, 100, 101, 99, "OCA-test", order_ref=reference
            )
        assert result["ok"]
        expected = [reference + ":entry", reference + ":stop"]
        if method == "oca":
            expected.append(reference + ":breakout")
        assert [order.orderRef for order in orders] == expected

    run(scenario())


def test_large_uncommitted_write_requires_locked_recovery_then_restores_snapshot(
    tmp_path,
):
    path = tmp_path / "lots.json"
    store = ExecutionStateStore(path)
    store.save(payload())
    script = """
import os, sys
from execution.state_store import ExecutionStateStore
store = ExecutionStateStore(sys.argv[1])
store.load()
store._before_commit = lambda connection: os._exit(25)
store.save({"lots": [], "pyramid_groups": {"uncommitted": "x" * 10000000}})
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(path)],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        check=False,
    )
    assert result.returncode == 25, result.stderr
    journal = Path(str(store.path) + "-journal")
    assert journal.exists()
    manager = OrderManager(FakeGW(), object(), path)
    assert manager.recovery_status()["blocks_entry"]
    assert journal.exists()  # construction did not mutate the hot journal
    with pytest.raises(PersistenceError, match="取得写锁"):
        manager._save()
    manager._load(
        allow_recovery=True
    )  # production only calls this after acquiring its writer lock
    assert manager.get("lot-t") is not None
    assert manager.pyramid_states()["group"]["entries"] == 1
    manager._save()
    assert not journal.exists()
