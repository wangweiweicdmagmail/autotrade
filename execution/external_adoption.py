"""Attribute a TWS position to a single confirmed entry and existing protection."""

import math
from collections import defaultdict


def execution_groups(fills, contract, accounts):
    groups = defaultdict(list)
    seen = set()
    for fill in fills:
        execution = fill.execution
        if (fill.contract.conId != contract.conId or execution.acctNumber not in accounts
                or not execution.permId or not execution.execId):
            continue
        # Multiple revisions of an execution make this snapshot ambiguous.
        key = execution.execId.rsplit(".", 1)[0]
        if key in seen:
            return {}
        seen.add(key)
        groups[execution.permId].append(fill)
    return groups


def totals(fills):
    qty = sum(float(fill.execution.shares) for fill in fills)
    if not qty or not math.isfinite(qty):
        return 0, 0, 0
    price = sum(float(f.execution.price) * float(f.execution.shares) for f in fills) / qty
    return qty, price, min(getattr(fill.execution, "time", fill.time).timestamp() for fill in fills)


def adoption_candidates(positions, trades, fills, owned, is_tool_order_id, fa_group):
    """Attribute one group order, including its fills across member accounts."""
    if not positions:
        return
    position = positions[0]
    contract = position.contract
    long = position.position > 0
    allocations = {p.account: abs(float(p.position)) for p in positions}
    qty = sum(allocations.values())
    if (len(allocations) != len(positions) or not qty.is_integer()
            or not contract.conId or contract.currency != "USD"
            or any(p.contract.conId != contract.conId or (p.position > 0) != long
                   or not abs(float(p.position)).is_integer() for p in positions)):
        return
    if len(trades) != 1:
        return
    stop = trades[0]
    order = stop.order
    group = getattr(order, "faGroup", "")
    if (stop.contract.conId != contract.conId
            or (group != fa_group if group else len(positions) != 1 or order.account != position.account)
            or getattr(order, "ocaGroup", "")
            or order.action != ("SELL" if long else "BUY") or order.orderType != "STP"
            or stop.orderStatus.status not in {"Submitted", "PreSubmitted"}
            or float(order.totalQuantity) != qty or float(stop.orderStatus.filled) != 0
            or not math.isfinite(float(order.auxPrice)) or float(order.auxPrice) <= 0
            or not order.permId or {order.orderId, order.permId} & owned
            or is_tool_order_id(order.orderId)):
        return
    groups = execution_groups(fills, contract, allocations)
    if order.parentId:
        groups = {perm: rows for perm, rows in groups.items()
                  if {f.execution.orderId for f in rows} == {order.parentId}}
    if len(groups) != 1:
        return
    perm_id, entry = next(iter(groups.items()))
    ids = {f.execution.orderId for f in entry}
    if (len(ids) != 1 or perm_id in owned or ids & owned or any(map(is_tool_order_id, ids))
            or (order.parentId and order.parentId not in ids)
            or {f.execution.side for f in entry} != {"BOT" if long else "SLD"}):
        return
    filled, price, timestamp = totals(entry)
    for row in positions:
        member_qty, member_price, _ = totals([f for f in entry if f.execution.acctNumber == row.account])
        if (member_qty != allocations[row.account]
                or not math.isclose(member_price, float(row.avgCost), rel_tol=0, abs_tol=0.05)):
            return
    if (filled != qty or not math.isfinite(price) or price <= 0):
        return
    yield (group, allocations, perm_id, next(iter(ids)), int(qty), price, timestamp, long, stop)
