import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as Row
from unittest.mock import AsyncMock

from execution.order_lot import OrderManager


def test_external_batch_survives_restart_and_tracks_broker_changes(tmp_path):
    async def scenario():
        contract = Row(conId=123, symbol="GOOGL", currency="USD", secType="STK")
        now = datetime.now(timezone.utc)
        position = Row(contract=contract, account="sub", position=10, avgCost=100)
        stop = Row(contract=contract, order=Row(account="sub", permId=2001, orderId=-2,
                                               parentId=-1, orderType="STP", action="SELL",
                                               totalQuantity=10, auxPrice=99),
                   orderStatus=Row(status="Submitted", filled=0), isDone=lambda: False)
        entry = Row(contract=contract, time=now, execution=Row(
            permId=2000, orderId=-1, acctNumber="sub", side="BOT", shares=10, price=100, execId="entry.01"))
        fills = [entry]
        positions = [position]
        orders = [stop]
        gateway = Row(fa_group="group", enabled=True, _connected=True, is_tool_order_id=lambda oid: False,
                      trade_map=AsyncMock(return_value={-2: stop}),
                      positions=AsyncMock(return_value={"GOOGL": 10}),
                      external_adoption_snapshot=AsyncMock(return_value=(positions, fills, orders, {"sub"})))
        path = tmp_path / "lots.json"
        manager = OrderManager(gateway, object(), state_path=path)
        manager.external_adoption_enabled = True
        orders.append(stop)
        await manager.reconcile()
        assert not manager.list_lots()
        orders.pop()

        await manager.reconcile()
        await manager.reconcile()
        lot, = manager.list_lots()
        assert (lot["lot_id"], lot["avg_fill"], lot["stop_px"], lot["filled_qty"]) == (
            "ibkr-external-2000", 100, 99, 10)
        manager = OrderManager(gateway, object(), state_path=path)
        assert (await manager.breakeven(lot["lot_id"]))[0] is False
        assert (await manager.close_lot(lot["lot_id"]))[0] is False

        stop.order.auxPrice = 100
        await manager.reconcile()
        assert manager.list_lots()[0]["stop_px"] == 100
        fills.append(Row(contract=contract, time=now, execution=Row(
            permId=3001, orderId=-3, acctNumber="sub", side="SLD", shares=5, price=102, execId="exit.01")))
        position.position = 5
        stop.order.totalQuantity = 5
        gateway.positions.return_value = {"GOOGL": 5}
        await manager.reconcile()
        assert manager.list_lots()[0]["filled_qty"] == 5

        # The next session only returns today's executions. Reload the ledger first.
        manager = OrderManager(gateway, object(), state_path=path)
        fills.clear()
        positions.clear()
        orders.clear()
        gateway.positions.return_value = {}
        await manager.reconcile()
        assert manager.list_lots()[0]["state"] == "ACTIVE"
        fills.append(Row(contract=contract, time=now + timedelta(days=1), execution=Row(
            permId=2001, orderId=-2, acctNumber="sub", side="SLD", shares=5, price=100, execId="stop.01")))
        await manager.reconcile()
        closed, = manager.list_lots()
        assert (closed["state"], closed["exit_px"], closed["realized_pnl"]) == ("CLOSED", 101, 10)

    asyncio.run(scenario())


def test_fa_group_adopts_allocations_as_one_batch(tmp_path):
    async def scenario():
        contract = Row(conId=123, symbol="GOOGL", currency="USD", secType="STK")
        now = datetime.now(timezone.utc)
        positions = [Row(contract=contract, account=account, position=qty, avgCost=price)
                     for account, qty, price in [("a", 4, 100), ("b", 6, 101)]]

        def fill(account, qty, price, side, perm, key):
            return Row(contract=contract, time=now, execution=Row(
                permId=perm, orderId=-1 if side == "BOT" else -3, acctNumber=account,
                side=side, shares=qty, price=price, execId=key))

        fills = [fill("a", 4, 100, "BOT", 2000, "a.01"), fill("b", 6, 101, "BOT", 2000, "b.01")]
        stop = Row(contract=contract, order=Row(account="", faGroup="other", permId=2001,
                                               orderId=-2, parentId=-1, orderType="STP",
                                               action="SELL", totalQuantity=10, auxPrice=99),
                   orderStatus=Row(status="Submitted", filled=0), isDone=lambda: False)
        gw = Row(fa_group="group", enabled=True, _connected=True, is_tool_order_id=lambda oid: False,
                 trade_map=AsyncMock(return_value={-2: stop}),
                 positions=AsyncMock(return_value={"GOOGL": 10}),
                 external_adoption_snapshot=AsyncMock(return_value=(positions, fills, [stop], {"a", "b"})))
        path = tmp_path / "lots.json"
        mgr = OrderManager(gw, object(), state_path=path)
        mgr.external_adoption_enabled = True
        await mgr.reconcile()
        assert not mgr.list_lots()
        stop.order.faGroup = "group"
        old = fill("a", 4, 100, "BOT", 1900, "old.01")
        old.execution.orderId = -9
        old.time = now - timedelta(hours=1)
        fills.append(old)
        stop.order.parentId = 0
        await mgr.reconcile()
        assert not mgr.list_lots()
        stop.order.parentId = -1
        await mgr.reconcile()
        lot, = mgr.list_lots()
        assert (lot["external_fa_group"], lot["avg_fill"], lot["filled_qty"]) == ("group", 100.6, 10)
        mgr = OrderManager(gw, object(), state_path=path)
        positions[0].position = 2
        positions[1].position = 3
        gw.positions.return_value = {"GOOGL": 5}
        stop.order.totalQuantity = 5
        fills.extend([fill("a", 3, 102, "SLD", 3000, "c.01"), fill("b", 2, 102, "SLD", 3000, "d.01")])
        await mgr.reconcile()
        assert mgr.list_lots()[0]["filled_qty"] == 10
        fills[-2:] = [fill("a", 2, 102, "SLD", 3000, "c.01"), fill("b", 3, 102, "SLD", 3000, "d.01")]
        await mgr.reconcile()
        assert mgr.list_lots()[0]["filled_qty"] == 5

    asyncio.run(scenario())


def test_missing_fa_members_does_not_use_config_for_adoption(tmp_path):
    from execution.ibkr import IBKRGateway

    async def scenario():
        gw = IBKRGateway.from_config()
        gw.enabled = gw._connected = True
        gw.ib = Row(requestFAAsync=AsyncMock(side_effect=TimeoutError),
                    reqExecutionsAsync=AsyncMock())
        assert await gw.external_adoption_snapshot({"GOOGL"}) is None
        gw.ib.reqExecutionsAsync.assert_not_awaited()

        gw.positions = AsyncMock(return_value={})
        gw.trade_map = AsyncMock(return_value={})
        mgr = OrderManager(gw, object(), state_path=tmp_path / "lots.json")
        mgr.external_adoption_enabled = True
        await mgr.reconcile()
        status = mgr.recovery_status()
        assert any(issue["kind"] == "external_adoption" for issue in status["issues"])
        assert not status["blocks_entry"]

    asyncio.run(scenario())
