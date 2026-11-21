"""Broker-side reconciliation for entries made manually in TWS."""

import asyncio
import logging
import math
import sqlite3
import time
from collections import defaultdict, deque
from contextlib import closing
from dataclasses import dataclass, replace
from datetime import datetime
from uuid import uuid4
from zoneinfo import ZoneInfo

import config
from execution.external_adoption import execution_groups

logger = logging.getLogger(__name__)
ET = ZoneInfo("America/New_York")
TERMINAL_CANCEL = {"Cancelled", "ApiCancelled"}
STOP_TYPES = {"STP", "STP LMT", "TRAIL", "TRAIL LIMIT"}


class GuardPreflightChanged(RuntimeError):
    """The operation was stopped before calling the broker's submit method."""


def is_external_guard_exempt(symbol):
    """Exact ticker match; accept the console's exchange-prefixed symbols too."""
    if not isinstance(symbol, str):
        return False
    ticker = symbol.strip().upper().rsplit(":", 1)[-1]
    return bool(ticker) and ticker in {
        value.strip().upper().rsplit(":", 1)[-1]
        for value in config.EXTERNAL_ORDER_GUARD_EXEMPT_TICKERS
    }


def in_test_scope(contract, account, qty, selected_account):
    return (
        bool(selected_account) and account == selected_account
        and contract.symbol == "GOOGL" and contract.secType == "STK"
        and contract.currency == "USD" and contract.conId > 0
        and math.isfinite(float(qty)) and 0 < float(qty) <= 1
    )


def scoped_order_account(order, selected, fa_group, accounts):
    group = getattr(order, "faGroup", "") or ""
    if group:
        return selected if group == fa_group and accounts == {selected} else None
    return order.account if order.account == selected and selected in accounts else None


@dataclass(frozen=True)
class VerifiedAllocationFill:
    contract: object
    execution: object
    commissionReport: object
    time: datetime


def unallocated_singleton_positions(positions, fills, accounts):
    if len(accounts) != 1:
        return []
    pending = []
    for row in positions:
        if (row.account not in accounts or row.contract.secType != "STK" or not row.position
                or is_external_guard_exempt(row.contract.symbol)):
            continue
        native = [f for f in fills if f.contract.conId == row.contract.conId
                  and f.execution.acctNumber == row.account]
        groups = execution_groups(native, row.contract, accounts)
        if native and sum(map(len, groups.values())) != len(native):
            continue
        if any(f.execution.side not in {"BOT", "SLD"} or not f.execution.time
               for f in native):
            continue
        net = sum(float(f.execution.shares) * (1 if f.execution.side == "BOT" else -1)
                  for f in native)
        # The returned history may start with an exit from an earlier session.
        if not math.isclose(net, float(row.position), rel_tol=0, abs_tol=1e-8):
            pending.append((row, max((f.execution.time for f in native), default=None)))
    return pending


def singleton_allocation_fills(pending, fills, completed, fa_group):
    """Attribute a completed FA entry when the sole member's fills are delayed."""
    allocated = []
    for row, last_member_fill in pending:
        direction = "BOT" if row.position > 0 else "SLD"
        action = "BUY" if row.position > 0 else "SELL"
        orders = [t for t in completed if t.contract.conId == row.contract.conId
                  and t.order.faGroup == fa_group and t.order.permId
                  and t.order.clientId == 0 and not t.order.orderRef
                  and t.orderStatus.status == "Filled" and t.order.account
                  and t.order.account != row.account]
        matches = []
        for trade in orders:
            order = trade.order
            if order.action not in {"BUY", "SELL"}:
                continue
            master = execution_groups(fills, row.contract, {order.account}).get(order.permId, [])
            if (not master or any(not f.execution.time or
                    (last_member_fill is not None and f.execution.time <= last_member_fill)
                    or f.execution.time.astimezone(ET).date() != datetime.now(ET).date()
                    or f.execution.clientId != 0 or f.execution.orderId > 0 or f.execution.orderRef
                    or f.execution.side != ("BOT" if order.action == "BUY" else "SLD")
                    for f in master)):
                continue
            matches.append((order, master))
        if len(matches) != 1:
            continue
        order, master = matches[0]
        quantity = sum(float(f.execution.shares) for f in master)
        if (order.action != action or not all(f.execution.side == direction for f in master)
                or not math.isclose(quantity, abs(row.position), rel_tol=0, abs_tol=1e-8)
                or not math.isclose(float(order.filledQuantity), quantity, rel_tol=0, abs_tol=1e-8)):
            continue
        allocated.extend(VerifiedAllocationFill(
            f.contract, replace(f.execution, acctNumber=row.account), f.commissionReport, f.time,
        ) for f in master)
    return allocated


class ExternalEntryGuard:
    def __init__(self, gateway, journal, test_account=None):
        self.gateway = gateway
        self.journal = journal
        self.test_account = test_account
        self.recent_results = deque(maxlen=64)
        self._pending_cancellations = {}

    def _record_result(self, kind, symbol, **details):
        self.recent_results.append({
            "id": uuid4().hex, "ts": time.time(), "kind": kind, "symbol": symbol, **details,
        })

    def _observe_cancellations(self):
        now = time.monotonic()
        for perm_id, (trade, deadline) in list(self._pending_cancellations.items()):
            status = trade.orderStatus.status
            if status in TERMINAL_CANCEL:
                self._record_result("cancelled", trade.contract.symbol)
            elif not trade.isDone() and now < deadline:
                continue
            del self._pending_cancellations[perm_id]

    def _journal(self, account, perm_id, *, quantity=None, contract_id=0):
        with closing(sqlite3.connect(self.journal)) as connection, connection:
            connection.execute("""
                CREATE TABLE IF NOT EXISTS exits (
                    account TEXT NOT NULL, entry_perm_id INTEGER NOT NULL,
                    requested REAL NOT NULL, observed REAL NOT NULL DEFAULT 0,
                    con_id INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (account, entry_perm_id)
                )
            """)
            columns = {row[1] for row in connection.execute("PRAGMA table_info(exits)")}
            if "con_id" not in columns:
                connection.execute("ALTER TABLE exits ADD COLUMN con_id INTEGER NOT NULL DEFAULT 0")
            connection.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS unresolved_external_exit
                ON exits (account, con_id) WHERE con_id > 0 AND observed < requested
            """)
            if quantity is None:
                return connection.execute("""
                    SELECT 1 FROM exits WHERE account=? AND
                    (entry_perm_id=? OR (con_id=? AND con_id > 0 AND observed < requested))
                """, (account, perm_id, contract_id)).fetchone() is not None
            return connection.execute("""
                INSERT OR IGNORE INTO exits (account, entry_perm_id, requested, con_id)
                VALUES (?, ?, ?, ?)
            """, (account, perm_id, quantity, contract_id)).rowcount == 1

    def _protection_pending(self, candidates, contract_id, stop_perm_id):
        with closing(sqlite3.connect(self.journal)) as connection, connection:
            connection.execute("""
                CREATE TABLE IF NOT EXISTS protections (
                    account TEXT NOT NULL, entry_perm_id INTEGER NOT NULL,
                    stop_perm_id INTEGER NOT NULL, con_id INTEGER NOT NULL, reported REAL,
                    PRIMARY KEY (account, entry_perm_id)
                )
            """)
            for account, (entry_perm_id, _) in candidates.items():
                row = connection.execute(
                    "SELECT stop_perm_id FROM protections WHERE account=? AND entry_perm_id=?",
                    (account, entry_perm_id),
                ).fetchone()
                if row and row[0] != stop_perm_id:
                    return True
            if stop_perm_id is not None:
                connection.executemany("INSERT OR IGNORE INTO protections VALUES (?, ?, ?, ?, NULL)", [
                    (acct, identity[0], stop_perm_id, contract_id) for acct, identity in candidates.items()
                ])
        return False

    def _record_stop_terminal(self, stop_perm_id, reported):
        with closing(sqlite3.connect(self.journal)) as connection, connection:
            connection.execute("UPDATE protections SET reported=? WHERE stop_perm_id=?",
                               (reported, stop_perm_id))

    def _prior_stops(self, contract_id):
        if not self.journal.exists():
            return {}
        with closing(sqlite3.connect(self.journal)) as connection:
            if not connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='protections'"
            ).fetchone():
                return {}
            return {(acct, entry): (stop, reported)
                    for acct, entry, stop, reported in connection.execute(
                        "SELECT account, entry_perm_id, stop_perm_id, reported FROM protections WHERE con_id=?",
                        (contract_id,),
                    )}

    @staticmethod
    def _stop_fills_match(fills, contract_id, accounts, expected):
        for perm_id, reported in expected.items():
            groups = defaultdict(dict)
            for fill in fills:
                execution = fill.execution
                if (fill.contract.conId == contract_id and execution.permId == perm_id
                        and execution.acctNumber in accounts):
                    groups[execution.acctNumber][execution.execId] = float(execution.shares)
            observed = sum(qty for group in groups.values() for qty in group.values())
            if (reported is None or not math.isfinite(reported) or not math.isfinite(observed)
                    or observed < reported):
                return False
        return True

    def _record_original_stop(self, candidates, contract_id, stop):
        with closing(sqlite3.connect(self.journal)) as connection, connection:
            connection.execute("""
                CREATE TABLE IF NOT EXISTS restore_stops (
                    account TEXT NOT NULL, entry_perm_id INTEGER NOT NULL,
                    con_id INTEGER NOT NULL, stop_perm_id INTEGER NOT NULL,
                    stop_price REAL NOT NULL, tif TEXT NOT NULL, outside_rth INTEGER NOT NULL,
                    restore_sent INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (account, entry_perm_id)
                )
            """)
            connection.executemany(
                "INSERT OR IGNORE INTO restore_stops VALUES (?, ?, ?, ?, ?, ?, ?, 0)",
                [(account, identity[0], contract_id, stop.order.permId, stop.order.auxPrice,
                  stop.order.tif, int(stop.order.outsideRth))
                 for account, identity in candidates.items()],
            )

    def _restorations(self, accounts):
        if not self.journal.exists():
            return []
        with closing(sqlite3.connect(self.journal)) as connection:
            if not connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='restore_stops'"
            ).fetchone():
                return []
            rows = connection.execute("""
                SELECT r.account, r.entry_perm_id, r.con_id, r.stop_perm_id, r.stop_price,
                       r.tif, r.outside_rth, e.requested, p.reported
                FROM restore_stops r
                JOIN exits e USING (account, entry_perm_id)
                JOIN protections p USING (account, entry_perm_id)
                WHERE r.restore_sent=0 AND e.observed=0
            """).fetchall()
            return [row for row in rows if row[0] in accounts
                    and (self.test_account is None or row[0] == self.test_account)]

    async def _restore_rejected_exits(self, snapshot, owned, managed_tickers):
        for account, entry, contract_id, stop, price, tif, outside, max_qty, reported in self._restorations(snapshot[3]):
            if not await self.gateway.external_close_rejected(account, contract_id, entry):
                continue
            fresh = await self.gateway.external_guard_snapshot()
            if fresh is None:
                continue
            rows, fills, orders, accounts = fresh
            if (accounts != snapshot[3] or account not in accounts
                    or any(t.contract.conId == contract_id and not t.isDone() for t in orders)
                    or not self._stop_fills_match(fills, contract_id, accounts, {stop: reported})
                    or any(f.contract.conId == contract_id
                           and f.execution.acctNumber == account
                           and getattr(f.execution, "orderRef", "") == f"external-guard:{entry}" for f in fills)):
                continue
            matching = [row for row in rows if row.account == account
                        and row.contract.conId == contract_id]
            if len(matching) != 1:
                continue
            row = matching[0]
            valid = self._candidate(row, fills, owned, {stop})
            if (not self._row_in_scope(row) or not valid or valid[0] != entry
                    or row.contract.symbol in managed_tickers or abs(row.position) > max_qty):
                continue
            with closing(sqlite3.connect(self.journal)) as connection, connection:
                claimed = connection.execute("""
                    UPDATE restore_stops SET restore_sent=1
                    WHERE account=? AND entry_perm_id=? AND restore_sent=0
                """, (account, entry)).rowcount == 1
            if not claimed:
                continue
            try:
                trade = self.gateway.restore_external_stop(row, entry, price, tif, bool(outside))
                logger.warning("外部平仓明确拒绝，保护单已提交 entryPermId=%s orderId=%s",
                               entry, trade.order.orderId)
            except GuardPreflightChanged:
                with closing(sqlite3.connect(self.journal)) as connection, connection:
                    connection.execute("""
                        UPDATE restore_stops SET restore_sent=0
                        WHERE account=? AND entry_perm_id=?
                    """, (account, entry))
                logger.warning("保护单恢复在提交前中止，等待重新核对 entryPermId=%s", entry)
            except Exception:
                logger.exception("外部保护单恢复受理未知，禁止重复提交 entryPermId=%s", entry)

    def _reconcile_submissions(self, fills, accounts):
        if not self.journal.exists():
            return
        executed = defaultdict(dict)
        for fill in fills:
            entry = fill.execution
            prefix = "external-guard:"
            ref = getattr(entry, "orderRef", "") or ""
            if not ref.startswith(prefix) or entry.acctNumber not in accounts:
                continue
            perm_id = ref[len(prefix):]
            if perm_id.isdecimal() and entry.execId:
                executed[entry.acctNumber, int(perm_id)][entry.execId] = fill
        completed = []
        with closing(sqlite3.connect(self.journal)) as connection, connection:
            for (account, perm_id), executions in executed.items():
                quantity = sum(float(fill.execution.shares) for fill in executions.values())
                row = connection.execute(
                    "SELECT requested, observed FROM exits WHERE account=? AND entry_perm_id=?",
                    (account, perm_id),
                ).fetchone()
                if row is None or not math.isfinite(quantity) or not row[1] < quantity <= row[0]:
                    continue
                connection.execute(
                    "UPDATE exits SET observed=? WHERE account=? AND entry_perm_id=?",
                    (quantity, account, perm_id),
                )
                logger.info("外部手动仓平仓成交核对 account=%s entryPermId=%s filled=%s/%s",
                            account, perm_id, quantity, row[0])
                if quantity == row[0]:
                    completed.append((next(iter(executions.values())).contract.symbol, quantity))
        for symbol, quantity in completed:
            self._record_result("closed", symbol, qty=quantity)

    def _manual(self, order, owned):
        return (getattr(order, "clientId", None) == 0
                and not getattr(order, "orderRef", "")
                and isinstance(order.orderId, int) and order.orderId <= 0
                and bool(order.permId)
                and order.permId not in owned and order.orderId not in owned
                and not self.gateway.is_tool_order_id(order.orderId))

    def _order_in_scope(self, trade, accounts):
        if is_external_guard_exempt(trade.contract.symbol):
            return False
        if self.test_account is None:
            return True
        order = trade.order
        return in_test_scope(
            trade.contract, scoped_order_account(order, self.test_account, self.gateway.fa_group, accounts),
            order.totalQuantity, self.test_account,
        )

    def _linked_orders_in_scope(self, trade, orders, owned, accounts):
        linked = [child for child in orders if child.order.parentId == trade.order.orderId
                  and not child.isDone()]
        return all(child.contract.conId == trade.contract.conId
                   and self._members(child.order, accounts) == self._members(trade.order, accounts)
                   and self._order_in_scope(child, accounts) and self._manual(child.order, owned)
                   and not child.order.ocaGroup
                   and not any(grandchild.order.parentId == child.order.orderId
                               and not grandchild.isDone() for grandchild in orders)
                   for child in linked)

    def _row_in_scope(self, row):
        if is_external_guard_exempt(row.contract.symbol):
            return False
        return self.test_account is None or in_test_scope(
            row.contract, row.account, abs(row.position), self.test_account,
        )

    def _members(self, order, accounts):
        group = getattr(order, "faGroup", "")
        if group:
            return accounts if group == self.gateway.fa_group else set()
        return {order.account} & accounts

    async def _cancel(self, trade):
        status = trade.orderStatus.status
        if status != "PendingCancel":
            if not self.gateway.cancel_external_bound_order(trade):
                logger.warning("外部订单未取得撤单权限 permId=%s", trade.order.permId)
                return False
            # ib_async can mark inactive or untransmitted orders cancelled locally.
            if (status not in TERMINAL_CANCEL | {"Filled", "Inactive"}
                    and (status != "PendingSubmit" or getattr(trade.order, "transmit", True))):
                self._pending_cancellations[trade.order.permId] = (trade, time.monotonic() + 300)
        for _ in range(20):
            if trade.isDone():
                status = trade.orderStatus.status
                self._observe_cancellations()
                logger.info("外部订单撤单核对 permId=%s status=%s",
                            trade.order.permId, status)
                return status in TERMINAL_CANCEL or status == "Filled"
            await asyncio.sleep(0.25)
        logger.warning("外部订单撤单结果待核对 permId=%s", trade.order.permId)
        return False

    def _positions(self, rows):
        positions = defaultdict(dict)
        for row in rows:
            positions[row.contract.conId][row.account] = row
        return positions

    def _candidate(self, row, fills, owned, allowed_stop_ids, expected=None):
        position = float(row.position)
        if not math.isfinite(position) or not position:
            return None
        groups = execution_groups(fills, row.contract, {row.account})
        ordered = sorted((f for group in groups.values() for f in group),
                         key=lambda f: f.execution.time)
        allocation_start = min(
            (f.execution.time for f in ordered if isinstance(f, VerifiedAllocationFill)),
            default=None,
        )
        if allocation_start is not None:
            # A fully matched latest FA allocation covers the current position.
            # Earlier fills may include closes of inventory held before the history window.
            ordered = [f for f in ordered if f.execution.time >= allocation_start]
        outstanding = []
        cumulative = 0.0
        for fill in ordered:
            entry = fill.execution
            qty = float(entry.shares)
            if not math.isfinite(qty) or qty <= 0 or entry.side not in {"BOT", "SLD"}:
                return None
            updated = cumulative + (qty if entry.side == "BOT" else -qty)
            if cumulative * updated < 0:
                return None  # A reversal can mix closing and opening shares in a single fill.
            outstanding.append(fill)
            if math.isclose(updated, 0, abs_tol=1e-8):
                outstanding.clear()
                updated = 0.0
            cumulative = updated
        if not math.isclose(cumulative, position, rel_tol=0, abs_tol=1e-8):
            return None
        groups = execution_groups(outstanding, row.contract, {row.account})
        direction = "BOT" if position > 0 else "SLD"
        opening = []
        net = 0.0
        for perm, group in groups.items():
            sides = {f.execution.side for f in group}
            if len(sides) != 1 or not sides <= {"BOT", "SLD"}:
                return None
            same_side = direction in sides
            qty = sum(float(f.execution.shares) for f in group)
            if not math.isfinite(qty) or qty <= 0:
                return None
            net += qty if same_side else -qty
            if not same_side and perm in allowed_stop_ids:
                continue
            if (not same_side or (self.test_account is not None and qty > 1)
                    or not all(self._manual(f.execution, owned) for f in group)):
                return None
            if any(f.execution.time.astimezone(ET).date() != datetime.now(ET).date()
                   for f in group):
                return None
            ids = {f.execution.orderId for f in group}
            if len(ids) != 1:
                return None
            opening.append((perm, ids.pop()))
        if (len(opening) != 1 or not math.isclose(net, abs(position), rel_tol=0, abs_tol=1e-8)
                or (expected is not None and opening[0] != expected)):
            return None
        return opening[0]

    async def run(self, owned, managed_tickers, *, allow_market):
        self._observe_cancellations()
        snapshot = await self.gateway.external_guard_snapshot()
        if snapshot is None:
            logger.warning("外部交易处理跳过：FA 成员、仓位或订单未确认")
            return
        rows, fills, orders, accounts = snapshot
        if self.test_account is not None and self.test_account not in accounts:
            logger.warning("外部订单测试暂缓：指定账户不在确认的 FA 成员列表")
            return
        self._reconcile_submissions(fills, accounts)
        positions = self._positions(row for row in rows if self._row_in_scope(row))
        cancelled_entries = False
        for trade in orders:
            order = trade.order
            members = self._members(order, accounts)
            if (trade.contract.secType != "STK" or not trade.contract.conId
                    or trade.contract.symbol.upper() in managed_tickers
                    or not members or not self._order_in_scope(trade, accounts) or not self._manual(order, owned)
                    or not self._linked_orders_in_scope(trade, orders, owned, accounts)
                    or order.parentId
                    or getattr(order, "ocaGroup", "") or order.action not in {"BUY", "SELL"}):
                continue
            by_member = positions.get(trade.contract.conId, {})
            sign = 1 if order.action == "BUY" else -1
            if any(account in by_member and by_member[account].position * sign < 0
                   for account in members):
                continue
            await self._cancel(trade)
            cancelled_entries = True
        await self._restore_rejected_exits(snapshot, owned, managed_tickers)
        if not allow_market:
            return
        if cancelled_entries:
            snapshot = await self.gateway.external_guard_snapshot()
            if snapshot is None:
                return
            rows, fills, orders, accounts = snapshot
            if self.test_account is not None and self.test_account not in accounts:
                return
            positions = self._positions(row for row in rows if self._row_in_scope(row))

        for contract_id, members in positions.items():
            first = next(iter(members.values()))
            if (not contract_id or first.contract.symbol.upper() in managed_tickers
                    or first.contract.currency != "USD"):
                continue
            active = [trade for trade in orders if trade.contract.conId == contract_id
                      and not trade.isDone()]
            stop = active[0] if len(active) == 1 else None
            if active and (stop is None or stop.order.orderType not in STOP_TYPES
                           or not self._manual(stop.order, owned)
                           or not self._order_in_scope(stop, accounts)
                           or not stop.order.parentId or getattr(stop.order, "ocaGroup", "")
                           or self._members(stop.order, accounts) != set(members)):
                continue
            prior = self._prior_stops(contract_id)
            stop_ids = {stop.order.permId} if stop else set()
            candidates = {
                acct: self._candidate(
                    row, fills, owned,
                    stop_ids | {p for (member, _), (p, _) in prior.items() if member == acct},
                )
                for acct, row in members.items()
            }
            if any(identity is None for identity in candidates.values()):
                continue
            awaiting = {
                acct for acct, identity in candidates.items()
                if self._journal(acct, identity[0], contract_id=contract_id)
            }
            if awaiting == set(candidates) or (stop and awaiting):
                continue
            expected = {}
            for account, (entry_perm_id, _) in candidates.items():
                saved = prior.get((account, entry_perm_id))
                if saved is not None:
                    stop_perm_id, reported = saved
                    expected[stop_perm_id] = reported
            if stop:
                if (
                    stop.order.orderType != "STP" or not math.isfinite(float(stop.order.auxPrice))
                    or stop.order.auxPrice <= 0 or stop.order.tif not in {"DAY", "GTC"}
                    or getattr(stop.order, "conditions", [])
                ):
                    continue
                if any(identity[1] != stop.order.parentId
                       or stop.order.action != ("SELL" if members[acct].position > 0 else "BUY")
                       for acct, identity in candidates.items()):
                    continue
                if self._protection_pending(candidates, contract_id, stop.order.permId):
                    continue
                self._record_original_stop(candidates, contract_id, stop)
                if not await self._cancel(stop):
                    continue
                reported = float(stop.orderStatus.filled or 0)
                if (not math.isfinite(reported) or reported < 0
                        or (stop.orderStatus.status == "Filled" and reported <= 0)):
                    continue
                self._record_stop_terminal(stop.order.permId, reported)
                expected[stop.order.permId] = reported
            fresh = await self.gateway.external_guard_snapshot()
            if fresh is None:
                logger.warning("外部平仓跳过 conId=%s：撤单后快照未确认", contract_id)
                continue
            fresh_rows, fresh_fills, fresh_orders, fresh_accounts = fresh
            if not self._stop_fills_match(fresh_fills, contract_id, accounts, expected):
                logger.warning("外部平仓跳过 conId=%s：保护单成交尚未对账", contract_id)
                continue
            remaining = self._positions(
                row for row in fresh_rows
                if self.test_account is None or row.account == self.test_account
            ).get(contract_id, {})
            if (fresh_accounts != accounts or set(remaining) - set(candidates)
                    or any(t.contract.conId == contract_id and not t.isDone() for t in fresh_orders)):
                logger.warning("外部平仓跳过 conId=%s：账户或活动订单有变化", contract_id)
                continue
            verified = {acct: self._candidate(row, fresh_fills, owned, set(expected), candidates[acct])
                        for acct, row in remaining.items()}
            if any(identity is None or not self._row_in_scope(remaining[acct])
                   or remaining[acct].position * members[acct].position <= 0
                   or abs(remaining[acct].position) > abs(members[acct].position)
                   for acct, identity in verified.items()):
                logger.warning("外部平仓跳过 conId=%s：撤单后余仓归属不明", contract_id)
                continue
            if not remaining:
                logger.info("外部手动仓余仓已归零 conId=%s", contract_id)
            for acct, identity in verified.items():
                if acct in awaiting:
                    continue
                row = remaining[acct]
                perm = identity[0]
                qty = abs(float(row.position))
                if not self._journal(acct, perm, quantity=qty, contract_id=contract_id):
                    continue
                try:
                    trade = self.gateway.submit_external_close(row, perm)
                    logger.info("外部手动仓平仓提交 account=%s entryPermId=%s closeOrderId=%s qty=%s",
                                acct, perm, trade.order.orderId, qty)
                except GuardPreflightChanged:
                    with closing(sqlite3.connect(self.journal)) as connection, connection:
                        connection.execute("DELETE FROM exits WHERE account=? AND entry_perm_id=? AND observed=0",
                                           (acct, perm))
                    logger.warning("外部平仓在提交前中止，等待重新核对 entryPermId=%s", perm)
                except Exception:
                    logger.exception("外部手动仓平仓受理未知，禁止重发 account=%s entryPermId=%s",
                                     acct, perm)
