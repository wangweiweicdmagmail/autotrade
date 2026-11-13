"""Isolated tests: no TV, broker, or production ledger connections."""
import asyncio
from functools import wraps
from types import SimpleNamespace

import pytest

from console.services.tv_paper_sync import TvPaperSync


def run_async(test):
    @wraps(test)
    def wrapped(*args, **kwargs):
        return asyncio.run(test(*args, **kwargs))
    return wrapped


SYMBOL = "NASDAQ:GOOGL"


class Paper:
    def __init__(self):
        self.positions, self.orders, self.history, self.writes = [], [], [], []

    async def call(self, tv, action, **args):
        if action == "snapshot":
            return dict(positions=self.positions, orders=self.orders, history=self.history)
        self.writes.append((action, args))
        return True

    def fill(self, order_id, side, change, qty, stop=None):
        self.history.append(dict(id=order_id, symbol=SYMBOL, side=side, qty=change, status=2, type=2))
        self.positions[:] = [dict(id="p", symbol=SYMBOL, side=1, qty=qty, stopLoss=stop)] if qty else []
        self.orders[:] = [dict(id="s", symbol=SYMBOL, status=6, type=3, parentId="p",
                               parentType=2, stopPrice=stop, qty=qty)] if qty else []


@pytest.fixture
def isolated(tmp_path):
    lot = dict(lot_id="live-1", symbol=SYMBOL, filled_qty=10, side="long", state="ACTIVE",
               avg_fill=105, stop_px=100, trail_order_id=11,
               protection_order_status="Submitted", protection_order_qty=10)
    lots = [lot]
    context = SimpleNamespace(paths=SimpleNamespace(data_dir=tmp_path),
                              gateway=SimpleNamespace(enabled=True, _connected=True, connected_at=100),
                              order_mgr=SimpleNamespace(
                                  list_lots=lambda: lots,
                                  recovery_status=lambda: dict(phase="checked", checked_at=101, issues=[], position_differences=[])))
    sync = TvPaperSync(context, "dedicated-paper")
    paper = Paper()
    sync._call = paper.call
    return sync, lot, lots, paper


@run_async
async def test_mirrors_only_confirmed_batches_and_stop_drag_is_advisory(isolated):
    sync, lot, lots, paper = isolated
    await sync.reconcile(None)
    assert paper.writes[-1][0] == "open"
    paper.fill("open", 1, 10, 10, 100)
    await sync.reconcile(None)
    lot["stop_px"] = 102
    await sync.reconcile(None)
    assert paper.writes[-1][0] == "stop"
    paper.positions[0]["stopLoss"] = paper.orders[0]["stopPrice"] = 102
    await sync.reconcile(None)
    paper.positions[0]["stopLoss"] = paper.orders[0]["stopPrice"] = 104
    await sync.reconcile(None)
    assert sync.notices[0]["paper_stop"] == 104
    assert len(paper.writes) == 2

    lot.update(filled_qty=4, protection_order_qty=4)
    await sync.reconcile(None)
    assert paper.writes[-1][0] == "reduce"
    assert paper.writes[-1][1]["qty"] == 6
    paper.fill("reduce", -1, 6, 4, 104)
    await sync.reconcile(None)
    lots.clear()
    await sync.reconcile(None)
    assert len(paper.writes) == 3
    assert "缺少" in sync.notices[0]["message"]

    lots.append(dict(lot, state="CLOSED", closed_at=1000, exit_px=106))
    await sync.reconcile(None)
    assert paper.writes[-1][1]["qty"] == 4
    paper.fill("close", -1, 4, 0)
    await sync.reconcile(None)
    assert sync.links == {}


@run_async
async def test_uncertain_submission_survives_restart_without_resending(isolated):
    sync, _, _, paper = isolated

    async def disconnect_after_submit(tv, action, **args):
        result = await paper.call(tv, action, **args)
        if action == "open":
            raise TimeoutError
        return result

    sync._call = disconnect_after_submit
    with pytest.raises(TimeoutError):
        await sync.reconcile(None)
    restored = TvPaperSync(sync.context, sync.account)
    restored._load()
    restored._call = paper.call
    await restored.reconcile(None)
    assert len(paper.writes) == 1
    assert "暂停自动重发" in restored.notices[0]["message"]
    paper.fill("first", 1, 10, 10, 100)
    await restored.reconcile(None)
    assert restored.links[SYMBOL]["entry_order_id"] == "first"


@run_async
async def test_ambiguous_same_symbol_batches_never_submit(isolated):
    sync, lot, lots, paper = isolated
    lots.append(dict(lot, lot_id="second"))
    await sync.reconcile(None)
    assert not paper.writes
    assert "多个实盘批次" in sync.notices[0]["message"]


@run_async
async def test_reconnected_live_gateway_waits_for_fresh_reconciliation(isolated):
    sync, _, _, paper = isolated
    sync.context.gateway.connected_at = 102
    await sync.reconcile(None)
    assert not paper.writes
    assert sync.state == "等待实盘账本恢复"


@run_async
async def test_fa_snapshot_failure_pauses_paper_sync(isolated):
    sync, _, _, paper = isolated
    recovery = sync.context.order_mgr.recovery_status()
    recovery["issues"].append({"kind": "external_adoption", "reason": "FA snapshot failed"})
    sync.context.order_mgr.recovery_status = lambda: recovery
    await sync.reconcile(None)
    assert "FA 账户组" in sync.state
    assert not paper.writes


@run_async
async def test_same_symbol_reentry_waits_for_previous_paper_close(isolated):
    sync, lot, lots, paper = isolated
    await sync.reconcile(None)
    paper.fill("first", 1, 10, 10, 100)
    await sync.reconcile(None)
    new = dict(lot, lot_id="live-2")
    lot.update(state="CLOSED", closed_at=123, exit_px=103)
    lots.append(new)
    await sync.reconcile(None)
    assert [action for action, _ in paper.writes] == ["open", "reduce"]
    paper.fill("close-first", -1, 10, 0)
    await sync.reconcile(None)
    assert SYMBOL not in sync.links
    await sync.reconcile(None)
    assert [action for action, _ in paper.writes] == ["open", "reduce", "open"]
