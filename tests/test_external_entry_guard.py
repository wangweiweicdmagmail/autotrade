import asyncio
from datetime import datetime
from types import SimpleNamespace as NS

import pytest

from execution.external_guard import ET, ExternalEntryGuard


CONTRACT = NS(conId=17, secType="STK", symbol="GOOGL", currency="USD")
ANT_CONTRACT = NS(conId=777, secType="STK", symbol="ANT", currency="USD")


def position(account="A", qty=10):
    return NS(account=account, contract=CONTRACT, position=qty, avgCost=100)


def fill(account="A", qty=10, side="BOT", perm=100, order_id=-10, client_id=0):
    now = datetime.now(ET)
    return NS(contract=CONTRACT, time=now, execution=NS(
        acctNumber=account, shares=qty, side=side, price=100, time=now,
        permId=perm, orderId=order_id, clientId=client_id,
        orderRef="", execId=f"{account}.{perm}.1",
    ))


def trade(*, stop=False, group="", parent=-10, client_id=0, account="A", qty=1, contract=CONTRACT):
    status = NS(status="Submitted", filled=0)
    return NS(contract=contract, order=NS(
        orderType="STP" if stop else "LMT", action="SELL" if stop else "BUY",
        clientId=client_id, orderRef="", orderId=-20 if stop else -10,
        permId=200 if stop else 100, parentId=parent if stop else 0,
        account=account, faGroup=group, ocaGroup="", totalQuantity=qty,
        auxPrice=95 if stop else 0, tif="GTC", outsideRth=False,
    ), orderStatus=status, isDone=lambda: status.status in {"Filled", "Cancelled"})


class Broker:
    fa_group = "FA"

    def __init__(self, *snapshots):
        self.snapshots = iter(snapshots)
        self.cancelled = []
        self.submitted = []
        self.cancel_status = "Cancelled"
        self.cancel_allowed = True
        self.fail_submit = False
        self.rejected = False
        self.restored = []

    async def external_guard_snapshot(self):
        return next(self.snapshots)

    def is_tool_order_id(self, value):
        return value >= 880_000 if value is not None else False

    def cancel_external_bound_order(self, trade):
        if not self.cancel_allowed:
            return False
        self.cancelled.append(trade.order.permId)
        trade.orderStatus.status = self.cancel_status
        return True

    async def external_close_rejected(self, account, contract_id, perm_id):
        return self.rejected

    def restore_external_stop(self, position, perm_id, price, tif, outside):
        self.restored.append((position.account, position.position, perm_id, price, tif, outside))
        return NS(order=NS(orderId=880_001))

    def submit_external_close(self, position, perm_id):
        self.submitted.append((position.account, position.position, perm_id))
        if self.fail_submit:
            raise OSError("disconnected after sending")
        return NS(order=NS(orderId=880_000))


def run(tmp_path, broker, *, managed=(), owned=(), market=True, test_account=None):
    guard = ExternalEntryGuard(broker, tmp_path / "external.sqlite3", test_account=test_account)
    asyncio.run(guard.run(set(owned), set(managed), allow_market=market))


@pytest.fixture
def ant_exemption(monkeypatch):
    """An optional policy under test; production defaults have no exemptions."""
    import config
    monkeypatch.setattr(config, 'EXTERNAL_ORDER_GUARD_EXEMPT_TICKERS', ('ANT',))


@pytest.mark.parametrize('symbol,exempt', [('ANT',True),('nasdaq:ant',True),(' ant ',True),
                                        ('ANTX',False),('PLANT',False),('GOOGL',False),('',False),(None,False)])
def test_long_term_exemption_matches_exact_ticker_only(ant_exemption, symbol, exempt):
    from execution.external_guard import is_external_guard_exempt
    assert is_external_guard_exempt(symbol) is exempt


def test_ant_entry_and_protection_orders_remain_while_other_external_entry_is_cancelled(tmp_path, ant_exemption):
    entry = trade(contract=ANT_CONTRACT)
    entry.order.permId, entry.order.orderId = 901, -901
    stop = trade(stop=True, parent=-901, contract=ANT_CONTRACT)
    stop.order.permId = 902
    other = trade()
    broker = Broker(([], [], [entry, stop, other], {'A'}))
    run(tmp_path, broker, market=False)
    assert broker.cancelled == [100]
    assert broker.submitted == []
    assert entry.orderStatus.status == stop.orderStatus.status == 'Submitted'


@pytest.mark.parametrize('account,qty', [('A',1041),('B',100),('A',-10)])
def test_ant_position_is_not_closed_or_journaled_in_any_fa_member(tmp_path, ant_exemption, account, qty):
    row = NS(account=account, contract=ANT_CONTRACT, position=qty, avgCost=100)
    broker = Broker(([row], [], [], {'A','B'}))
    run(tmp_path, broker)
    assert broker.cancelled == broker.submitted == broker.restored == []
    assert not (tmp_path / 'external.sqlite3').exists()


def test_ant_exemption_does_not_disable_other_external_position_liquidation(tmp_path, ant_exemption):
    ant = NS(account='A', contract=ANT_CONTRACT, position=100, avgCost=100)
    stop = trade(stop=True, contract=ANT_CONTRACT)
    snapshot = ([ant, position()], [fill()], [stop], {'A'})
    broker = Broker(snapshot, snapshot)
    run(tmp_path, broker)
    assert broker.submitted == [('A',10,100)]
    assert broker.cancelled == broker.restored == []
    assert stop.orderStatus.status == 'Submitted'


def test_long_term_ant_does_not_trigger_unnecessary_fa_execution_reconstruction(ant_exemption):
    from execution.external_guard import unallocated_singleton_positions
    ant = NS(account='A', contract=ANT_CONTRACT, position=100, avgCost=100)
    other = position()
    assert unallocated_singleton_positions([ant,other], [], {'A'}) == [(other,None)]


def test_gateway_preflight_refuses_exempt_cancel_close_and_stop_replacement(monkeypatch, ant_exemption):
    import config
    from execution.ibkr import IBKRGateway
    from execution.external_guard import GuardPreflightChanged
    from unittest.mock import Mock
    monkeypatch.setattr(config, 'EXTERNAL_MANUAL_GUARD_TEST_ACCOUNT', '')
    monkeypatch.setattr(config, 'EXTERNAL_MANUAL_GUARD_FA_SCOPE', True)
    gateway = IBKRGateway('localhost',0,0,'','FA',enabled=True)
    gateway._connected = True
    order = trade(contract=ANT_CONTRACT)
    gateway.ib = NS(openTrades=lambda:[order], positions=lambda:pytest.fail('exemption must precede any write preflight'))
    gateway._guard_verified_accounts = frozenset({'A'})
    gateway._guard_verified_connection = (gateway.ib,'')
    cancel = Mock(side_effect=AssertionError('ANT order must remain'))
    monkeypatch.setattr(gateway, 'cancel_order', cancel)
    assert gateway.cancel_external_bound_order(order) is False
    row = NS(account='A', contract=ANT_CONTRACT, position=100)
    with pytest.raises(GuardPreflightChanged, match='豁免'):
        gateway.submit_external_close(row,901)
    with pytest.raises(GuardPreflightChanged, match='豁免'):
        gateway.restore_external_stop(row,901,95,'GTC',False)
    cancel.assert_not_called()


@pytest.mark.parametrize('cleanup', [True,False])
def test_legacy_cleanup_and_alert_modes_also_exempt_ant(monkeypatch, ant_exemption, cleanup):
    import config
    from console.services.monitoring import MonitoringService
    from unittest.mock import AsyncMock, Mock
    monkeypatch.setattr(config, 'EXTERNAL_MANUAL_GUARD_ENABLED', False)
    monkeypatch.setattr(config, 'EXTERNAL_ORDER_CLEANUP_ENABLED', cleanup)
    order = trade(stop=True,contract=ANT_CONTRACT)
    gateway = NS(enabled=True,_connected=True,
        positions=AsyncMock(return_value={'ANT':100}),fa_accounts=AsyncMock(return_value=['A']),
        trade_map=AsyncMock(return_value={-20:order}),open_orders_snapshot=AsyncMock(return_value=[order]),
        cancel_order=Mock(side_effect=AssertionError('exempt order')),
        place_close=AsyncMock(side_effect=AssertionError('exempt position')))
    context = NS(gateway=gateway,order_mgr=NS(list_lots=lambda:[],aggregate=lambda:[]),
                 events=NS(record_event=Mock()))
    context.monitoring = MonitoringService(context)
    context.monitoring._external_alerts = []
    result = asyncio.run(context.monitoring._external_cleanup_once())
    assert result['cancelled'] == result['closed'] == result['failed'] == 0
    assert context.monitoring._external_alerts == []
    context.events.record_event.assert_not_called()
    gateway.cancel_order.assert_not_called()
    gateway.place_close.assert_not_awaited()


def test_console_status_reports_no_long_term_exemptions(console_app):
    snapshot = asyncio.run(console_app.snapshot.build_snapshot())
    assert snapshot['status']['external_order_guard']['exempt_tickers'] == []


@pytest.mark.parametrize('order_type', ['LMT','STP'])
def test_ant_external_pending_order_is_cancelled_without_exemption(tmp_path, order_type):
    entry = trade(contract=ANT_CONTRACT)
    entry.order.orderType = order_type
    entry.order.auxPrice = 105 if order_type == 'STP' else 0
    broker = Broker(([], [], [entry], {'A'}))
    run(tmp_path, broker, market=False)
    assert broker.cancelled == [100]
    assert entry.orderStatus.status == 'Cancelled'


@pytest.mark.parametrize('managed', [False,True])
def test_ant_external_position_is_closed_but_console_owned_position_is_preserved(tmp_path, managed):
    row = NS(account='A', contract=ANT_CONTRACT, position=10, avgCost=100)
    execution = fill()
    execution.contract = ANT_CONTRACT
    stop = trade(stop=True, contract=ANT_CONTRACT)
    before = ([row], [execution], [stop], {'A'})
    after = ([row], [execution], [], {'A'})
    broker = Broker(before, after)
    run(tmp_path, broker, managed={'ANT'} if managed else ())
    assert broker.cancelled == ([] if managed else [200])
    assert broker.submitted == ([] if managed else [('A',10,100)])


def test_pending_external_entry_cancelled_and_partial_fill_closed(tmp_path):
    entry = trade()
    partial = ([position(qty=4)], [fill(qty=4)], [], {"A"})
    broker = Broker(([], [], [entry], {"A"}), partial, partial)
    run(tmp_path, broker)
    assert broker.cancelled == [100]
    assert broker.submitted == [("A", 4, 100)]


def test_voice_result_waits_for_cancel_and_ignores_filled_order(tmp_path, monkeypatch):
    async def skip_wait(_seconds):
        pass

    monkeypatch.setattr(asyncio, "sleep", skip_wait)
    pending = trade()
    filled = trade()
    filled.order.permId = 101
    empty = ([], [], [], {"A"})
    broker = Broker(([], [], [pending, filled], {"A"}), empty, empty)
    broker.cancel_status = "PendingCancel"
    guard = ExternalEntryGuard(broker, tmp_path / "external.sqlite3")
    asyncio.run(guard.run(set(), set(), allow_market=False))
    assert list(guard.recent_results) == []

    pending.orderStatus.status = "Cancelled"
    filled.orderStatus.status = "Filled"
    asyncio.run(guard.run(set(), set(), allow_market=False))
    asyncio.run(guard.run(set(), set(), allow_market=False))
    assert [(result["kind"], result["symbol"]) for result in guard.recent_results] == [
        ("cancelled", "GOOGL"),
    ]


def test_voice_result_waits_for_full_external_exit(tmp_path):
    first = fill(qty=4, side="SLD", perm=500, client_id=11)
    last = fill(qty=6, side="SLD", perm=500, client_id=11)
    last.execution.execId = "A.500.2"
    for execution in (first.execution, last.execution):
        execution.orderRef = "external-guard:100"
    full = ([], [first, last], [], {"A"})
    broker = Broker(([], [first], [], {"A"}), full, full)
    guard = ExternalEntryGuard(broker, tmp_path / "external.sqlite3")
    assert guard._journal("A", 100, quantity=10, contract_id=17)

    asyncio.run(guard.run(set(), set(), allow_market=False))
    assert list(guard.recent_results) == []
    asyncio.run(guard.run(set(), set(), allow_market=False))
    asyncio.run(guard.run(set(), set(), allow_market=False))
    assert [(result["kind"], result["symbol"], result["qty"]) for result in guard.recent_results] == [
        ("closed", "GOOGL", 10),
    ]


def test_fa_stop_late_fill_only_closes_member_remainder(tmp_path):
    stop = trade(stop=True, group="FA")
    entry_fills = [fill("A"), fill("B")]
    broker = Broker(
        ([position("A"), position("B")], entry_fills, [stop], {"A", "B"}),
        ([position("B", 6)], entry_fills + [
            fill("A", qty=10, side="SLD", perm=200, order_id=-20),
            fill("B", qty=4, side="SLD", perm=200, order_id=-20),
        ], [], {"A", "B"}),
    )
    run(tmp_path, broker)
    assert broker.cancelled == [200]
    assert broker.submitted == [("B", 6, 100)]


@pytest.mark.parametrize("case", ["fa", "console", "owned", "wrong_parent", "cancel_failed", "outside_rth", "mixed"])
def test_no_liquidation_when_scope_or_protection_unverified(tmp_path, case):
    stop = trade(stop=True, parent=-99 if case == "wrong_parent" else -10)
    fills = [fill()]
    if case == "mixed":
        fills.append(fill(qty=5, perm=300, order_id=880_000, client_id=11))
    broker = Broker(None if case == "fa" else ([position()], fills, [stop], {"A"}))
    broker.cancel_allowed = case != "cancel_failed"
    run(tmp_path, broker, managed={"GOOGL"} if case == "console" else (),
        owned={100} if case == "owned" else (), market=case != "outside_rth")
    assert broker.cancelled == []
    assert broker.submitted == []


def test_broker_acceptance_unknown_survives_restart(tmp_path):
    snapshot = ([position(qty=-10)], [fill(side="SLD")], [], {"A"})
    broker = Broker(snapshot, snapshot)
    broker.fail_submit = True
    run(tmp_path, broker)
    assert broker.submitted == [("A", -10, 100)]
    restarted = Broker(snapshot)
    run(tmp_path, restarted)
    assert restarted.submitted == []


def test_stop_still_open_after_cancel_means_no_close(tmp_path):
    before = ([position()], [fill()], [trade(stop=True)], {"A"})
    after = ([position()], [fill()], [trade(stop=True)], {"A"})
    broker = Broker(before, after)
    run(tmp_path, broker)
    assert broker.cancelled == [200]
    assert broker.submitted == []


def test_pending_console_and_manual_exit_are_untouched(tmp_path):
    exit_order = trade()
    exit_order.order.action = "SELL"
    console = trade(client_id=11)
    broker = Broker(([position()], [fill()], [exit_order, console], {"A"}))
    run(tmp_path, broker)
    assert broker.cancelled == []
    assert broker.submitted == []


def test_second_external_entry_same_day_uses_new_execution_identity(tmp_path):
    from datetime import timedelta

    previous = fill()
    previous_exit = fill(side="SLD", perm=200, order_id=880_000, client_id=11)
    previous_exit.execution.orderRef = "external-guard:100"
    new = fill(perm=300, order_id=-30)
    previous.execution.time -= timedelta(seconds=2)
    previous_exit.execution.time -= timedelta(seconds=1)
    snapshot = ([position()], [new, previous_exit, previous], [], {"A"})
    broker = Broker(snapshot, snapshot)
    run(tmp_path, broker)
    assert broker.submitted == [("A", 10, 300)]


def test_gateway_close_routes_directly_to_fa_member(monkeypatch):
    from execution.ibkr import IBKRGateway

    calls = []
    gw = IBKRGateway("localhost", 0, 0, "", "FA", enabled=True)
    gw._connected = True
    gw.ib = NS(placeOrder=lambda *_: pytest.fail("client 0 must never submit"),
               positions=lambda: [position("B", -1)], openTrades=lambda: [])
    writer = IBKRGateway("localhost", 0, 11, "", "FA", enabled=True)
    writer._connected = True
    writer.ib = NS(placeOrder=lambda contract, order: calls.append((contract, order)))
    gw.external_order_writer = writer
    monkeypatch.setattr(writer, "_next_tool_order_id", lambda: 880_000)
    import config
    monkeypatch.setattr(config, "EXTERNAL_MANUAL_GUARD_TEST_ACCOUNT", "B")
    gw._guard_verified_connection = (gw.ib, "B")
    gw.submit_external_close(position("B", -1), 100)
    [(contract, order)] = calls
    assert contract.conId == CONTRACT.conId
    assert (order.account, order.faGroup, order.action, order.totalQuantity) == ("B", "", "BUY", 1)
    assert (order.orderType, order.orderRef) == ("MKT", "external-guard:100")
    gw.restore_external_stop(position("B", -1), 100, 110, "GTC", False)
    assert [(order.orderType, order.orderRef) for _, order in calls] == [
        ("MKT", "external-guard:100"), ("STP", "external-guard-stop:100"),
    ]


def test_gateway_requires_client_zero_and_bound_order_handle(monkeypatch):
    from execution.ibkr import IBKRGateway

    gw = IBKRGateway("localhost", 0, 11, "", "FA", enabled=True)
    gw._connected = True
    row = trade()
    calls = []
    gw.ib = NS(openTrades=lambda: [])
    monkeypatch.setattr(gw, "cancel_order", lambda handle: calls.append(handle) or True)
    assert not gw.cancel_external_bound_order(row)
    gw.client_id = 0
    import config
    monkeypatch.setattr(config, "EXTERNAL_MANUAL_GUARD_TEST_ACCOUNT", "A")
    gw._guard_verified_connection = (gw.ib, "A")
    gw._guard_verified_accounts = frozenset({"A"})
    assert not gw.cancel_external_bound_order(row)
    gw.ib.openTrades = lambda: [row]
    assert gw.cancel_external_bound_order(row)
    assert calls == [row]


def test_console_guard_clears_alerts_without_publishing(monkeypatch):
    import config
    from console.services.monitoring import MonitoringService

    handled = []

    async def enforce(*, allow_market):
        handled.append(allow_market)

    monkeypatch.setattr(config, "EXTERNAL_MANUAL_GUARD_ENABLED", True)
    service = MonitoringService(NS(
        gateway=NS(enabled=True, _connected=True),
        order_mgr=NS(enforce_external_entry_policy=enforce),
        system=NS(_is_trading_hours=lambda: True),
    ))
    service._external_alerts = [{"kind": "position", "ticker": "GOOGL"}]
    asyncio.run(service._external_cleanup_once())
    assert service._external_alerts == []
    assert handled == [True]



def test_stop_fill_status_ahead_of_execution_snapshot_blocks_close(tmp_path):
    stop = trade(stop=True)
    stop.orderStatus.filled = 10
    broker = Broker(
        ([position()], [fill()], [stop], {"A"}),
        ([position()], [fill()], [], {"A"}),
    )
    broker.cancel_status = "Filled"
    run(tmp_path, broker)
    assert broker.cancelled == [200]
    assert broker.submitted == []
    # The stop has now disappeared from the order list, but its fill is still stale.
    stale = ([position()], [fill()], [], {"A"})
    restarted = Broker(stale, stale)
    run(tmp_path, restarted)
    assert restarted.submitted == []


def test_delayed_partial_stop_fill_reconciles_on_later_cycle(tmp_path):
    stop = trade(stop=True)
    stop.orderStatus.filled = 4
    stale = ([position()], [fill()], [], {"A"})
    first = Broker(([position()], [fill()], [stop], {"A"}), stale)
    run(tmp_path, first)
    assert first.submitted == []
    current = ([position(qty=6)], [
        fill(), fill(qty=4, side="SLD", perm=200, order_id=-20)
    ], [], {"A"})
    restarted = Broker(current, current)
    run(tmp_path, restarted)
    assert restarted.submitted == [("A", 6, 100)]


@pytest.mark.parametrize("order_type", ["MKT", "LMT", "STP", "STP LMT", "TRAIL"])
def test_scoped_pending_cancellation_covers_order_types(tmp_path, order_type):
    targets = [
        trade(account="B"),
        trade(qty=2),
        trade(group="FA"),
        trade(contract=NS(**{**vars(CONTRACT), "symbol": "GOOG"})),
        trade(),
    ]
    targets[-1].order.orderType = order_type
    broker = Broker(([], [], targets, {"A", "B"}), ([], [], targets[:-1], {"A", "B"}))
    run(tmp_path, broker, test_account="A")
    assert [row.orderStatus.status for row in targets] == ["Submitted"] * 4 + ["Cancelled"]


@pytest.mark.parametrize("case", ["other_account", "larger", "outside_fa", "late_order", "late_size"])
def test_scoped_close_skips_outside_scope_or_snapshot_change(tmp_path, case):
    before = ([position(qty=1)], [fill(qty=1)], [], {"A", "B"})
    after = before
    if case == "outside_fa":
        before = ([], [], [], {"B"})
    if case == "other_account":
        before = ([position("B", 1)], [fill("B", 1)], [], {"A", "B"})
    if case == "larger":
        before = ([position(qty=2)], [fill(qty=2)], [], {"A", "B"})
    if case == "late_order":
        after = (before[0], before[1], [trade(stop=True)], before[3])
    if case == "late_size":
        after = ([position(qty=2)], [fill(qty=2)], [], {"A", "B"})
    broker = Broker(before, after)
    run(tmp_path, broker, test_account="A")
    assert broker.cancelled == broker.submitted == []


@pytest.mark.parametrize("result", ["rejected", "unknown", "new_stop", "flat"])
def test_scoped_protection_recovers_only_when_rejected_and_still_unprotected(tmp_path, result):
    before = ([position(qty=1)], [fill(qty=1)], [trade(stop=True)], {"A"})
    bare = (before[0], before[1], [], before[3])
    first = Broker(before, bare)
    first.fail_submit = True
    run(tmp_path, first, test_account="A")
    assert first.cancelled == [200]
    assert first.submitted == [("A", 1, 100)]

    fresh = bare
    if result == "new_stop":
        fresh = (bare[0], bare[1], [trade(stop=True)], bare[3])
    if result == "flat":
        fresh = ([], bare[1], [], bare[3])
    next_cycle = Broker(bare, fresh, bare, fresh)
    next_cycle.rejected = result != "unknown"
    run(tmp_path, next_cycle, test_account="A")
    run(tmp_path, next_cycle, test_account="A")
    assert next_cycle.submitted == []
    assert next_cycle.restored == ([("A", 1, 100, 95, "GTC", False)] if result == "rejected" else [])


@pytest.mark.parametrize("change", ["late_order", "late_position", "wrong_account", "oversized"])
def test_gateway_write_gate_rules_out_live_changes(monkeypatch, change):
    import config
    from execution.external_guard import GuardPreflightChanged
    from execution.ibkr import IBKRGateway

    gw = IBKRGateway("localhost", 0, 0, "", "FA", enabled=True)
    gw._connected = True
    monkeypatch.setattr(config, "EXTERNAL_MANUAL_GUARD_TEST_ACCOUNT", "A")
    gw.ib = NS(
        positions=lambda: [] if change == "late_position" else [position(qty=1)],
        openTrades=lambda: [trade()] if change == "late_order" else [],
        placeOrder=lambda *_: pytest.fail("write escaped the safety gate"),
    )
    gw._guard_verified_connection = (gw.ib, "A")
    row = position("B" if change == "wrong_account" else "A",
                   qty=2 if change == "oversized" else 1)
    with pytest.raises(GuardPreflightChanged):
        gw.submit_external_close(row, 100)


def test_gateway_verifies_explicit_rejection_by_order_reference():
    from execution.ibkr import IBKRGateway

    gw = IBKRGateway("localhost", 0, 0, "", "FA", enabled=True)
    gw._connected = True
    exit_trade = trade()
    exit_trade.order.orderId = 88_000_000
    assert gw.is_tool_order_id(exit_trade.order.orderId)
    exit_trade.order.orderRef = "external-guard:100"
    exit_trade.orderStatus.status = "Cancelled"
    exit_trade.fills = []
    exit_trade.log = [NS(errorCode=201)]

    async def completed(**kwargs):
        return []

    gw.ib = NS(trades=lambda: [exit_trade], reqCompletedOrdersAsync=completed)
    assert asyncio.run(gw.external_close_rejected("A", CONTRACT.conId, 100))
    assert not asyncio.run(gw.external_close_rejected("A", CONTRACT.conId, 101))
    exit_trade.log = [NS(errorCode=202)]
    assert not asyncio.run(gw.external_close_rejected("A", CONTRACT.conId, 100))


def test_parent_cancellation_cannot_cascade_outside_test_scope(tmp_path):
    entry = trade()
    child = trade(stop=True, account="B")
    broker = Broker(([], [], [entry, child], {"A", "B"}))
    run(tmp_path, broker, test_account="A")
    assert broker.cancelled == []


def test_gateway_snapshot_rejects_order_appearing_around_position_read(monkeypatch):
    import config
    from execution.ibkr import IBKRGateway

    gw = IBKRGateway("localhost", 0, 0, "", "FA", enabled=True)
    gw._connected = True
    monkeypatch.setattr(config, "EXTERNAL_MANUAL_GUARD_TEST_ACCOUNT", "A")
    pending = iter([[], [trade()]])

    async def open_orders():
        return next(pending)

    async def accounts(*, confirmed_only):
        assert confirmed_only
        return ["A"]

    async def fills(_):
        return [fill(qty=1)]

    async def positions():
        return [position(qty=1)]

    gw.ib = NS(reqExecutionsAsync=fills, reqPositionsAsync=positions,
               positions=lambda: [position(qty=1)])
    monkeypatch.setattr(gw, "fa_accounts", accounts)
    monkeypatch.setattr(gw, "open_orders_snapshot", open_orders)
    assert asyncio.run(gw.external_guard_snapshot()) is None
    assert gw._guard_verified_connection is None

@pytest.mark.parametrize("accounts,expected", [({"A"}, True), ({"A", "B"}, False)])
def test_fa_allocation_requires_exact_single_member(tmp_path, accounts, expected):
    pending = trade(group="FA", account="")
    broker = Broker(([], [], [pending], accounts), ([], [], [], accounts))
    run(tmp_path, broker, test_account="A")
    assert bool(broker.cancelled) is expected


def test_external_fill_identity_does_not_depend_on_commission_in_average_cost(tmp_path):
    row = position(qty=1)
    row.avgCost = 287.7695
    bought = fill(qty=1, order_id=0)
    bought.execution.price = 286.7695
    snapshot = ([row], [bought], [], {"A"})
    broker = Broker(snapshot, snapshot)
    run(tmp_path, broker, test_account="A")
    assert broker.submitted == [("A", 1, 100)]


def test_matching_price_cannot_override_console_ownership(tmp_path):
    row = position(qty=1)
    bought = fill(qty=1)
    bought.execution.price = row.avgCost
    broker = Broker(([row], [bought], [], {"A"}))
    run(tmp_path, broker, test_account="A", owned={bought.execution.permId})
    assert broker.submitted == []


def test_fa_mode_handles_larger_non_test_symbol_across_members(tmp_path):
    contract = NS(conId=28, secType="STK", symbol="MSFT", currency="USD")
    members = {"A", "B"}
    rows = [position("A", 12), position("B", 9)]
    fills = [fill("A", 12), fill("B", 9)]
    for item in rows + fills:
        item.contract = contract
    snapshot = (rows, fills, [], members)
    broker = Broker(snapshot, snapshot)
    run(tmp_path, broker)
    assert broker.submitted == [("A", 12, 100), ("B", 9, 100)]


def test_fa_mode_gateway_can_cancel_group_order_and_blocks_other_group(monkeypatch):
    import config
    from execution.ibkr import IBKRGateway

    gw = IBKRGateway("localhost", 0, 0, "", "FA", enabled=True)
    gw._connected = True
    monkeypatch.setattr(config, "EXTERNAL_MANUAL_GUARD_TEST_ACCOUNT", "")
    monkeypatch.setattr(config, "EXTERNAL_MANUAL_GUARD_FA_SCOPE", True)
    contract = NS(conId=28, secType="STK", symbol="MSFT", currency="USD")
    inside = trade(group="FA", account="", qty=27, contract=contract)
    outside = trade(group="other", account="A", qty=27, contract=contract)
    gw.ib = NS(openTrades=lambda: [inside, outside])
    gw._guard_verified_connection = (gw.ib, "")
    gw._guard_verified_accounts = frozenset({"A", "B"})
    cancelled = []
    monkeypatch.setattr(gw, "cancel_order", lambda handle: cancelled.append(handle) or True)
    assert not gw.cancel_external_bound_order(outside)
    assert gw.cancel_external_bound_order(inside)
    assert cancelled == [inside]


def test_fa_mode_rejected_close_restores_only_its_member_protection(tmp_path):
    stop = trade(stop=True, group="FA", qty=19)
    first_fills = [fill("A", 10), fill("B", 9)]
    bare = ([position("A", 10), position("B", 9)], first_fills, [], {"A", "B"})
    broker = Broker((bare[0], bare[1], [stop], bare[3]), bare)
    run(tmp_path, broker)
    assert broker.cancelled == [200]
    assert broker.submitted == [("A", 10, 100), ("B", 9, 100)]

    successful_b = fill("B", 9, side="SLD", perm=250, client_id=0, order_id=88_000_003)
    successful_b.execution.orderRef = "external-guard:100"
    still_held = ([position("A", 10)], first_fills + [successful_b], [], {"A", "B"})
    after = Broker(still_held, still_held)
    after.rejected = True
    run(tmp_path, after)
    assert after.restored == [("A", 10, 100, 95, "GTC", False)]
    assert after.submitted == []


@pytest.mark.parametrize("case", ["confirmed", "multi_member", "wrong_group", "mismatched_qty", "native"])
def test_singleton_fa_master_fill_is_used_only_for_confirmed_missing_allocation(tmp_path, case):
    from ib_async import CommissionReport, Execution, Fill
    from execution.external_guard import singleton_allocation_fills, unallocated_singleton_positions

    now = datetime.now(ET)
    row = position(qty=3)
    native = [fill(qty=3)] if case == "native" else []
    master = Fill(CONTRACT, Execution(execId="master.1", acctNumber="MASTER",
                  time=now, side="BOT", shares=3, permId=99, orderId=-6, clientId=0),
                  CommissionReport(), now)
    order = NS(faGroup="other" if case == "wrong_group" else "FA", permId=99,
               clientId=0, orderRef="", account="MASTER", action="BUY",
               filledQuantity=2 if case == "mismatched_qty" else 3)
    completed = NS(contract=CONTRACT, order=order, orderStatus=NS(status="Filled"))
    accounts = {"A", "B"} if case == "multi_member" else {"A"}
    fills = native + [master]
    pending = unallocated_singleton_positions([row], fills, accounts)
    projected = singleton_allocation_fills(pending, fills, [completed], "FA")
    assert len(projected) == (1 if case == "confirmed" else 0)
    if projected:
        assert (projected[0].execution.acctNumber, projected[0].execution.permId) == ("A", 99)
        broker = Broker(([row], fills + projected, [], accounts), ([row], fills + projected, [], accounts))
        run(tmp_path, broker)
        assert broker.submitted == [("A", 3, 99)]


def test_late_member_execution_does_not_resend_unresolved_master_allocated_exit(tmp_path):
    master_allocated = ([position(qty=3)], [fill(qty=3, perm=100)], [], {"A"})
    broker = Broker(master_allocated, master_allocated)
    broker.fail_submit = True
    run(tmp_path, broker)
    assert broker.submitted == [("A", 3, 100)]

    member = ([position(qty=3)], [fill(qty=3, perm=101)], [], {"A"})
    restarted = Broker(member, member)
    run(tmp_path, restarted)
    assert restarted.submitted == []


def test_singleton_allocation_after_truncated_history_can_close_only_latest_position(tmp_path):
    from datetime import timedelta
    from ib_async import CommissionReport, Execution, Fill
    from execution.external_guard import singleton_allocation_fills, unallocated_singleton_positions

    now = datetime.now(ET)
    row = position(qty=1041)
    older_exit = fill(qty=3500, side="SLD", perm=110)
    older_exit.execution.time = now - timedelta(hours=1)
    master = [
        Fill(CONTRACT, Execution(execId=f"master-{i}.1", acctNumber="MASTER",
             time=now + timedelta(milliseconds=i), side="BOT", shares=qty,
             permId=200, orderId=-6, clientId=0), CommissionReport(), now)
        for i, qty in enumerate((200, 100, 741))
    ]
    order = NS(contract=CONTRACT, order=NS(faGroup="FA", permId=200, clientId=0,
               orderRef="", account="MASTER", action="BUY", filledQuantity=1041),
               orderStatus=NS(status="Filled"))
    fills = [older_exit, *master]
    pending = unallocated_singleton_positions([row], fills, {"A"})
    allocation = singleton_allocation_fills(pending, fills, [order], "FA")
    assert sum(f.execution.shares for f in allocation) == 1041
    before = ([row], fills + allocation, [], {"A"})
    broker = Broker(before, before)
    run(tmp_path, broker)
    assert broker.submitted == [("A", 1041, 200)]
