# -*- coding: utf-8 -*-
"""订单状态机（execution/order_lot.py）回归。

守的口径：PENDING→ACTIVE（部分成交 STOP 收缩 / OCA 竞态防御性撤突破腿）、
cancel_lot（无成交撤单 / 部分成交转 ACTIVE / ACTIVE 拒撤）、close_lot
（全平闭环 / Filled 有余仓不伪造闭环 / Cancelled 恢复保护）、closeHalf
（STOP 先缩再平 / 缺句柄拒绝）、update_trail 棘轮（方向/防抖/冷却/保本）、
breakeven（幂等/幻影守卫/快照冷却）、reprice_retest_lot（幂等/重试/拒绝/
股数跟随/部分成交只改价）、reconcile（保护恢复/复活 cap+幂等/丢单按成交
真相收敛：查询失败保持 PENDING、确无成交收敛 CANCELLED）、幻影守卫边界、
_lot_lock 并发串行化。全部 mock gateway，状态文件在 tmp_path。
"""
import asyncio
import time

from execution.order_lot import (OrderManager, OrderLot, PENDING, ACTIVE,
                                 CLOSING, CANCELLED, CLOSED)
from .conftest import run

TERMINAL = ("Filled", "Cancelled", "Inactive", "ApiCancelled")


class FakeTrade:
    """最小 Trade 模拟：order/orderStatus/contract/isDone。"""
    def __init__(self, order_id, status="Submitted", filled=0, avg=0.0,
                 qty=0, action="BUY", order_type="LMT", symbol="QCOM",
                 aux=0.0, lmt=0.0, parent_id=0, perm_id=0):
        self.order = type("O", (), {"orderId": order_id, "permId": perm_id,
                                    "totalQuantity": qty, "action": action,
                                    "orderType": order_type, "auxPrice": aux,
                                    "lmtPrice": lmt, "parentId": parent_id,
                                    "trailStopPrice": 0.0})()
        self.contract = type("C", (), {"symbol": symbol})()
        self.orderStatus = type("S", (), {"status": status, "filled": filled,
                                          "avgFillPrice": avg})()

    def isDone(self):
        return self.orderStatus.status in TERMINAL


class FakeGW:
    enabled = True
    _connected = True

    def __init__(self, fills=None, positions=None, trades=None):
        self._fills_fail = fills is None
        self._fills = fills or {}
        self._pos = positions           # None = 查询失败
        self._trades = trades or {}
        self._next_id = 99000000
        self.cancel_calls = []
        self.stop_qty_calls = []        # modify_stop_qty(new_qty)
        self.stop_px_calls = []         # modify_stop(new_stop)
        self.stop_only_calls = []       # place_stop_only(...)
        self.close_calls = []           # place_close(...)
        self.modify_ls_calls = []       # modify_limit_stop
        self.modify_ls_qty_calls = []   # modify_limit_stop_qty
        self.trade_map_calls = 0
        self.modify_delay = 0.0
        self.in_modify = 0
        self.max_concurrent_modify = 0
        self.fail_modify_ls_once = False

    def _alloc(self):
        self._next_id += 1
        return self._next_id

    async def entry_fills_by_order(self, symbol, order_ids):
        if self._fills_fail:
            return None
        return {oid: v for oid, v in self._fills.items() if oid in set(order_ids)}

    async def trade_map(self):
        self.trade_map_calls += 1
        return dict(self._trades)

    async def positions(self):
        return None if self._pos is None else dict(self._pos)

    def cancel_order(self, trade):
        self.cancel_calls.append(trade.order.orderId)
        if trade is not None and not trade.isDone():
            trade.orderStatus.status = "Cancelled"
        return True

    async def modify_stop_qty(self, trade, new_qty):
        trade = getattr(trade, "trail_trade", trade)
        self.stop_qty_calls.append(int(new_qty))
        trade.order.totalQuantity = new_qty
        return True

    async def modify_stop(self, unit, new_stop_px):
        self.in_modify += 1
        self.max_concurrent_modify = max(self.max_concurrent_modify,
                                         self.in_modify)
        try:
            if self.modify_delay:
                await asyncio.sleep(self.modify_delay)
            new_stop = round(new_stop_px, 2)
            self.stop_px_calls.append(new_stop)
            unit.trail_trade.order.auxPrice = new_stop
            unit.stop_px = new_stop
            return True
        finally:
            self.in_modify -= 1

    async def place_stop_only(self, symbol, side, qty, stop_px):
        self.stop_only_calls.append((symbol, side, qty, stop_px))
        oid = self._alloc()
        t = FakeTrade(oid, status="Submitted", qty=qty,
                      action=("SELL" if side == "long" else "BUY"),
                      order_type="STP", symbol=symbol.split(":")[-1],
                      aux=stop_px)
        return {"ok": True, "order_id": oid, "stop_order_id": oid,
                "trade": t}

    async def place_close(self, symbol, side, qty, ref_px, order_ref="", market=False):
        oid = self._alloc()
        t = FakeTrade(oid, status="Submitted", qty=qty,
                      action=("SELL" if side == "long" else "BUY"),
                      order_type="MKT" if market else "LMT", symbol=symbol.split(":")[-1],
                      lmt=ref_px)
        self.close_calls.append((symbol, side, qty, order_ref, t))
        return {"ok": True, "order_id": oid, "permId": 0, "trade": t}

    async def modify_limit_stop(self, unit, new_limit, new_stop):
        if self.fail_modify_ls_once:
            self.fail_modify_ls_once = False
            return False
        self.modify_ls_calls.append((round(new_limit, 2), round(new_stop, 2)))
        unit.entry_px = round(new_limit, 2)
        unit.stop_px = round(new_stop, 2)
        return True

    async def modify_limit_stop_qty(self, unit, new_limit, new_stop, new_qty):
        self.modify_ls_qty_calls.append((round(new_limit, 2),
                                         round(new_stop, 2), int(new_qty)))
        unit.entry_px = round(new_limit, 2)
        unit.stop_px = round(new_stop, 2)
        return True

    async def last_price(self, symbol, timeout=5.0):
        return None

    async def closing_fill_avg(self, *a, **k):
        return None

    async def realized_pnl(self):
        return 0.0


def mk_lot(lot_id="lot-t", state=PENDING, qty=100, side="long",
           entry_px=100.0, stop_px=95.0, limit_type="", sym="NASDAQ:QCOM"):
    return OrderLot(lot_id=lot_id, symbol=sym, side=side, qty=qty,
                    entry_px=entry_px, stop_px=stop_px, trail_amount=0.0,
                    init_entry_px=entry_px, init_stop_px=stop_px,
                    init_risk_usd=round(abs(entry_px - stop_px) * qty, 2),
                    state=state, limit_type=limit_type,
                    entry_order_id=8801, trail_order_id=8802,
                    entry_time=time.time())


def mk_mgr(gw, tmp_path, name):
    return OrderManager(gw, object(), state_path=tmp_path / f"lot_{name}.json")


def attach(mgr, lot, entry=None, trail=None, brk=None):
    lot.entry_trade = entry
    lot.trail_trade = trail
    lot.breakout_trade = brk
    mgr._lots[lot.lot_id] = lot
    return lot


def cancel_watchers(mgr):
    for t in mgr._watchers.values():
        t.cancel()
    mgr._watchers.clear()


def alive_trail(lot, qty=None, aux=None, oid=None):
    return FakeTrade(oid or lot.trail_order_id, status="Submitted",
                     qty=qty or lot.qty,
                     action=("SELL" if lot.side == "long" else "BUY"),
                     order_type="STP", aux=aux if aux is not None else lot.stop_px,
                     parent_id=lot.entry_order_id)


# ==================== A. 部分成交→终态→激活 ====================

def test_a1_partial_fill_then_cancel_activates(tmp_path):
    """A1: PENDING 部分成交 60/100 后被撤 → ACTIVE + STOP 收缩到 60。"""
    async def go():
        gw = FakeGW(fills={})
        mgr = mk_mgr(gw, tmp_path, "a1")
        lot = mk_lot(state=PENDING, qty=100)
        entry = FakeTrade(8801, status="Cancelled", filled=60, avg=100.10,
                          qty=100, action="BUY")
        trail = alive_trail(lot)
        attach(mgr, lot, entry, trail)
        ok = await mgr._handle_entry_terminal(lot)
        assert ok and lot.state == ACTIVE, f"A1 状态错误: {lot.state}"
        assert lot.filled_qty == 60 and abs(lot.avg_fill - 100.10) < 1e-6
        assert gw.stop_qty_calls == [60], f"A1 STOP 未收缩: {gw.stop_qty_calls}"
        assert trail.order.totalQuantity == 60
        assert lot.entry_time > 0
        cancel_watchers(mgr)
    run(go())


def test_a2_full_fill_no_stop_qty_change(tmp_path):
    """A2: 全部成交 → 不调用 modify_stop_qty。"""
    async def go():
        gw = FakeGW(fills={})
        mgr = mk_mgr(gw, tmp_path, "a2")
        lot = mk_lot(state=PENDING, qty=100)
        entry = FakeTrade(8801, status="Filled", filled=100, avg=100.05,
                          qty=100, action="BUY")
        attach(mgr, lot, entry, alive_trail(lot))
        ok = await mgr._handle_entry_terminal(lot)
        assert ok and lot.state == ACTIVE and lot.filled_qty == 100
        assert gw.stop_qty_calls == []
        assert lot.protection_restore_needed is False
        cancel_watchers(mgr)
    run(go())


def test_a3_truth_fill_cancels_live_breakout_leg(tmp_path):
    """A3: 缓存漏报但成交真相有 LMT 腿成交，突破腿仍活跃 → 防御性撤掉。"""
    async def go():
        gw = FakeGW(fills={8801: (100, 100.05)}, positions={"QCOM": 100.0})
        mgr = mk_mgr(gw, tmp_path, "a3")
        lot = mk_lot(state=PENDING, qty=100)
        lot.oca_group = f"OCA-{lot.lot_id}"
        lot.breakout_order_id = 8803
        lot.breakout_px = 101.0
        entry = FakeTrade(8801, status="Filled", filled=0, qty=100, action="BUY")
        brk = FakeTrade(8803, status="Submitted", qty=100, action="BUY",
                        order_type="STP", aux=101.0)
        attach(mgr, lot, entry, alive_trail(lot), brk)
        ok = await mgr._handle_entry_terminal(lot)
        assert ok and lot.state == ACTIVE and lot.filled_qty == 100
        assert 8803 in gw.cancel_calls, "A3 突破腿仍活跃但未被防御性撤销"
        assert gw.stop_only_calls and gw.stop_only_calls[-1][2] == 100
        cancel_watchers(mgr)
    run(go())


def test_a4_cache_path_cancels_live_breakout_leg(tmp_path):
    """A4: 缓存路径（无真相查询）激活 + 活跃突破腿同样防御性撤销。"""
    async def go():
        gw = FakeGW(fills={})
        mgr = mk_mgr(gw, tmp_path, "a4")
        lot = mk_lot(state=PENDING, qty=100)
        lot.oca_group = f"OCA-{lot.lot_id}"
        lot.breakout_order_id = 8803
        entry = FakeTrade(8801, status="Filled", filled=100, avg=100.05,
                          qty=100, action="BUY")
        brk = FakeTrade(8803, status="Submitted", qty=100, action="BUY",
                        order_type="STP", aux=101.0)
        attach(mgr, lot, entry, alive_trail(lot), brk)
        ok = await mgr._handle_entry_terminal(lot)
        assert ok and lot.state == ACTIVE
        assert 8803 in gw.cancel_calls, "A4 缓存路径突破腿未被防御性撤销"
        cancel_watchers(mgr)
    run(go())


# ==================== B. cancel_lot ====================

def test_b1_pending_no_fill_cancel(tmp_path):
    """B1: PENDING 无成交 → 入场+STOP 均撤 → CANCELLED。"""
    async def go():
        gw = FakeGW(fills={})
        mgr = mk_mgr(gw, tmp_path, "b1")
        lot = mk_lot(state=PENDING, qty=100)
        attach(mgr, lot, FakeTrade(8801, status="Submitted", qty=100),
               alive_trail(lot))
        ok, msg = await mgr.cancel_lot(lot.lot_id)
        assert ok and lot.state == CANCELLED, f"B1 错误: {ok} {lot.state} {msg}"
        assert set(gw.cancel_calls) == {8801, 8802}
        cancel_watchers(mgr)
    run(go())


def test_b2_pending_partial_fill_cancel(tmp_path):
    """B2: PENDING 撤单时已部分成交 30 → 撤剩余 + ACTIVE + STOP 缩到 30。"""
    async def go():
        gw = FakeGW(fills={})
        mgr = mk_mgr(gw, tmp_path, "b2")
        lot = mk_lot(state=PENDING, qty=100)
        entry = FakeTrade(8801, status="Submitted", filled=30, avg=100.10,
                          qty=100, action="BUY")
        attach(mgr, lot, entry, alive_trail(lot))
        ok, msg = await mgr.cancel_lot(lot.lot_id)
        assert ok and lot.state == ACTIVE, f"B2 错误: {ok} {lot.state} {msg}"
        assert lot.filled_qty == 30 and "部分成交" in msg
        assert gw.stop_qty_calls == [30]
        cancel_watchers(mgr)
    run(go())


def test_b3_active_cancel_rejected(tmp_path):
    """B3: ACTIVE 实盘拒绝撤单（防裸仓）。"""
    async def go():
        gw = FakeGW(fills={})
        mgr = mk_mgr(gw, tmp_path, "b3")
        lot = mk_lot(state=ACTIVE, qty=100)
        lot.filled_qty = 100
        lot.avg_fill = 100.0
        attach(mgr, lot, None, alive_trail(lot))
        ok, msg = await mgr.cancel_lot(lot.lot_id)
        assert not ok and "不能撤单" in msg
        assert gw.cancel_calls == [] and lot.state == ACTIVE
    run(go())


# ==================== C. close_lot ====================

def test_c1_full_close_cycle(tmp_path):
    """C1: ACTIVE 全平闭环 CLOSING→CLOSED（exit_px/realized_pnl/actual_rr）。"""
    async def go():
        closed_events = []
        gw = FakeGW(fills={}, positions={"QCOM": 100.0})
        mgr = mk_mgr(gw, tmp_path, "c1")
        mgr.on_lot_closed = closed_events.append
        lot = mk_lot(state=ACTIVE, qty=100)
        lot.filled_qty = 100
        lot.avg_fill = 100.0
        lot.last_px = 101.0
        attach(mgr, lot, None, alive_trail(lot))
        ok, msg = await mgr.close_lot(lot.lot_id, ref_px=101.0)
        assert ok and lot.state == CLOSING, f"C1 提交错误: {ok} {lot.state} {msg}"
        assert 8802 in gw.cancel_calls, "C1 保护单未先撤"
        assert lot.close_requested_qty == 100
        close_trade = gw.close_calls[-1][-1]
        cancel_watchers(mgr)
        close_trade.orderStatus.status = "Filled"
        close_trade.orderStatus.filled = 100
        close_trade.orderStatus.avgFillPrice = 101.5
        outcome = await mgr._handle_close_terminal(lot)
        assert outcome == "closed" and lot.state == CLOSED
        assert abs(lot.exit_px - 101.5) < 1e-6
        assert abs(lot.realized_pnl - 150.0) < 1e-6
        assert abs(lot.actual_rr - 150.0 / 500.0) < 1e-6
        assert lot.exit_reason == "manual" and lot.closed_at > 0
        assert len(closed_events) == 0  # _handle_close_terminal 不上抛，watcher 才上抛
        cancel_watchers(mgr)
    run(go())


def test_c2_filled_with_residual_no_fake_close(tmp_path):
    """C2: 平仓单 Filled 但本地有余仓（60/100）→ 禁止伪造 CLOSED。"""
    async def go():
        gw = FakeGW(fills={}, positions={"QCOM": 100.0})
        mgr = mk_mgr(gw, tmp_path, "c2")
        lot = mk_lot(state=ACTIVE, qty=100)
        lot.filled_qty = 100
        lot.avg_fill = 100.0
        lot.last_px = 101.0
        attach(mgr, lot, None, alive_trail(lot))
        ok, _ = await mgr.close_lot(lot.lot_id, ref_px=101.0)
        assert ok
        close_trade = gw.close_calls[-1][-1]
        cancel_watchers(mgr)
        close_trade.orderStatus.status = "Filled"
        close_trade.orderStatus.filled = 60
        close_trade.orderStatus.avgFillPrice = 101.0
        outcome = await mgr._handle_close_terminal(lot)
        assert outcome == "waiting" and lot.state == CLOSING
        assert lot.filled_qty == 40 and "核查" in lot.note
        cancel_watchers(mgr)
    run(go())


def test_c3_close_cancelled_restores_protection(tmp_path):
    """C3: 平仓单 Cancelled 无成交 → 回 ACTIVE + 按实际余仓恢复 STOP。"""
    async def go():
        gw = FakeGW(fills={}, positions={"QCOM": 100.0})
        mgr = mk_mgr(gw, tmp_path, "c3")
        lot = mk_lot(state=ACTIVE, qty=100)
        lot.filled_qty = 100
        lot.avg_fill = 100.0
        lot.last_px = 101.0
        attach(mgr, lot, None, alive_trail(lot))
        ok, _ = await mgr.close_lot(lot.lot_id, ref_px=101.0)
        assert ok
        close_trade = gw.close_calls[-1][-1]
        cancel_watchers(mgr)
        close_trade.orderStatus.status = "Cancelled"
        outcome = await mgr._handle_close_terminal(lot)
        assert outcome == "restored" and lot.state == ACTIVE
        assert lot.filled_qty == 100
        assert gw.stop_only_calls[-1][2] == 100
        cancel_watchers(mgr)
    run(go())


# ==================== D. closeHalf ====================

def test_d1_close_half(tmp_path):
    """D1: 平一半：先缩 STOP 到 50 → 平 50 → 记账/余仓/实现盈亏。"""
    async def go():
        gw = FakeGW(fills={}, positions={"QCOM": 100.0})
        mgr = mk_mgr(gw, tmp_path, "d1")
        lot = mk_lot(state=ACTIVE, qty=100)
        lot.filled_qty = 100
        lot.avg_fill = 100.0
        lot.last_px = 101.0
        trail = alive_trail(lot)
        attach(mgr, lot, None, trail)
        ok, msg = await mgr.close_half(lot.lot_id, ref_px=101.0)
        assert ok, f"D1 提交错误: {msg}"
        assert gw.stop_qty_calls == [50], f"D1 STOP 未先收缩: {gw.stop_qty_calls}"
        assert gw.close_calls[-1][2] == 50
        close_trade = gw.close_calls[-1][-1]
        cancel_watchers(mgr)
        close_trade.orderStatus.status = "Filled"
        close_trade.orderStatus.filled = 50
        close_trade.orderStatus.avgFillPrice = 101.0
        await mgr._watch_partial(lot, 50)
        assert lot.state == ACTIVE and lot.filled_qty == 50 and lot.qty == 50
        assert lot.close_filled_qty == 50
        assert abs(lot.realized_pnl - 50.0) < 1e-6
        assert trail.order.totalQuantity == 50
        cancel_watchers(mgr)
    run(go())


def test_d2_close_half_missing_trail_handle_rejected(tmp_path):
    """D2: 缺保护单句柄 → closeHalf 拒绝（否则券商侧 STOP 按原股数，
    触发会变反向仓）。"""
    async def go():
        gw = FakeGW(fills={}, positions={"QCOM": 100.0})
        mgr = mk_mgr(gw, tmp_path, "d2")
        lot = mk_lot(state=ACTIVE, qty=100)
        lot.filled_qty = 100
        lot.avg_fill = 100.0
        lot.last_px = 101.0
        lot.trail_trade = None          # 句柄丢失（重启/断线）
        assert lot.trail_order_id == 8802
        mgr._lots[lot.lot_id] = lot
        ok, msg = await mgr.close_half(lot.lot_id, ref_px=101.0)
        assert not ok and "句柄" in msg
        assert gw.close_calls == []
        assert lot.filled_qty == 100
        cancel_watchers(mgr)
    run(go())


# ==================== E. update_trail 棘轮 ====================

def test_e1_long_ratchet(tmp_path):
    """E1: 多仓棘轮：只上不下 + 1 美分防抖 + 保本标记 + 冷却拒绝。"""
    async def go():
        gw = FakeGW()
        mgr = mk_mgr(gw, tmp_path, "e1")
        lot = mk_lot(state=ACTIVE, qty=100, stop_px=95.0)
        lot.filled_qty = 100
        lot.avg_fill = 100.0
        attach(mgr, lot, None, alive_trail(lot))
        ok, _ = await mgr.update_trail(lot.lot_id, 94.0)      # 多仓只上不下
        assert not ok and lot.stop_px == 95.0 and gw.stop_px_calls == []
        ok, _ = await mgr.update_trail(lot.lot_id, 96.0)
        assert ok and lot.stop_px == 96.0 and gw.stop_px_calls == [96.0]
        ok, _ = await mgr.update_trail(lot.lot_id, 96.005)    # 1 美分防抖
        assert not ok and lot.stop_px == 96.0 and gw.stop_px_calls == [96.0]
        ok, _ = await mgr.update_trail(lot.lot_id, 100.50)    # 越过成本 → 保本
        assert ok and lot.stop_px == 100.50
        assert lot.breakeven_reached and lot.breakeven_time > 0 \
            and lot.breakeven_seconds >= 0
        lot.trail_modify_blocked_until = time.time() + 100    # 冷却期拒绝
        n = len(gw.stop_px_calls)
        ok, msg = await mgr.update_trail(lot.lot_id, 200.0)
        assert not ok and "冷却" in msg and len(gw.stop_px_calls) == n
        assert lot.stop_px == 100.50
        cancel_watchers(mgr)
    run(go())


def test_e2_short_ratchet(tmp_path):
    """E2: 空仓棘轮：只下不上 + 越过成本保本标记。"""
    async def go():
        gw = FakeGW()
        mgr = mk_mgr(gw, tmp_path, "e2")
        lot = mk_lot(state=ACTIVE, qty=100, side="short",
                     entry_px=100.0, stop_px=105.0)
        lot.filled_qty = 100
        lot.avg_fill = 100.0
        attach(mgr, lot, None, alive_trail(lot))
        ok, _ = await mgr.update_trail(lot.lot_id, 106.0)     # 空仓只下不上
        assert not ok and lot.stop_px == 105.0 and gw.stop_px_calls == []
        ok, _ = await mgr.update_trail(lot.lot_id, 104.0)
        assert ok and lot.stop_px == 104.0 and gw.stop_px_calls == [104.0]
        ok, _ = await mgr.update_trail(lot.lot_id, 99.50)     # 越过成本 → 保本
        assert ok and lot.breakeven_reached
        cancel_watchers(mgr)
    run(go())


# ==================== F. breakeven ====================

def test_f1_breakeven_idempotent(tmp_path):
    """F1: 保护单活跃 → 直接改到成本；重复调用幂等不重复改单。"""
    async def go():
        gw = FakeGW()
        mgr = mk_mgr(gw, tmp_path, "f1")
        lot = mk_lot(state=ACTIVE, qty=100)
        lot.filled_qty = 100
        lot.avg_fill = 100.0
        attach(mgr, lot, None, alive_trail(lot))
        ok, msg = await mgr.breakeven(lot.lot_id)
        assert ok and lot.stop_px == 100.0 and lot.breakeven_reached
        assert gw.stop_px_calls == [100.0]
        ok, msg = await mgr.breakeven(lot.lot_id)
        assert ok and "已处于保本" in msg
        assert gw.stop_px_calls == [100.0], "保本不幂等，重复改单"
        cancel_watchers(mgr)
    run(go())


def test_f2_phantom_guard_then_restore(tmp_path):
    """F2: 保护单已终态 + 幻影守卫拦截（券商无持仓）→ 拒绝新建 STOP；
    有足额持仓 → 恢复独立 STOP @成本。"""
    async def go():
        gw = FakeGW(positions={})
        mgr = mk_mgr(gw, tmp_path, "f2")
        lot = mk_lot(state=ACTIVE, qty=100)
        lot.filled_qty = 100
        lot.avg_fill = 100.0
        attach(mgr, lot, None, FakeTrade(8802, status="Cancelled", qty=100,
                                         action="SELL", order_type="STP"))
        ok, msg = await mgr.breakeven(lot.lot_id)
        assert not ok and gw.stop_only_calls == []
        gw._pos = {"QCOM": 100.0}
        ok, msg = await mgr.breakeven(lot.lot_id)
        assert ok and gw.stop_only_calls[-1][2] == 100 \
            and gw.stop_only_calls[-1][3] == 100.0
        assert lot.breakeven_reached and lot.stop_px == 100.0
        cancel_watchers(mgr)
    run(go())


def test_f3_snapshot_find_cooldown(tmp_path):
    """F3: 快照认回冷却生效（cooldown 内不打快照、直接拒绝）。"""
    async def go():
        gw = FakeGW(positions={"QCOM": 100.0})
        mgr = mk_mgr(gw, tmp_path, "f3")
        lot = mk_lot(state=ACTIVE, qty=100)
        lot.filled_qty = 100
        lot.avg_fill = 100.0
        lot.trail_trade = None
        lot.trail_order_id = None
        mgr._lots[lot.lot_id] = lot
        lot.protection_find_blocked_until = time.time() + 30
        ok, msg = await mgr.breakeven(lot.lot_id)
        assert not ok and "冷却" in msg and gw.trade_map_calls == 0
        cancel_watchers(mgr)
    run(go())


def test_f4_incomplete_snapshot_cooldown(tmp_path):
    """F4: 快照不完整（trade_map=None）→ 禁止新建并进入冷却，冷却内不再查询。"""
    async def go():
        class NoneMapGW(FakeGW):
            async def trade_map(self):
                self.trade_map_calls += 1
                return None
        gw = NoneMapGW(positions={"QCOM": 100.0})
        mgr = mk_mgr(gw, tmp_path, "f4")
        lot = mk_lot(state=ACTIVE, qty=100)
        lot.filled_qty = 100
        lot.avg_fill = 100.0
        lot.trail_trade = None
        lot.trail_order_id = None
        mgr._lots[lot.lot_id] = lot
        ok, msg = await mgr.breakeven(lot.lot_id)
        assert not ok and "快照不完整" in msg and gw.trade_map_calls == 1
        assert lot.protection_find_blocked_until > time.time()
        ok, msg = await mgr.breakeven(lot.lot_id)
        assert not ok and gw.trade_map_calls == 1, "冷却内重复打快照"
        assert gw.stop_only_calls == []
        cancel_watchers(mgr)
    run(go())


# ==================== G. reprice_retest_lot ====================

def test_g1_same_bar_idempotent(tmp_path):
    """G1: 同 bar_time 重复调用幂等拒绝。"""
    async def go():
        gw = FakeGW()
        mgr = mk_mgr(gw, tmp_path, "g1")
        lot = mk_lot(state=PENDING, qty=100, limit_type="retest",
                     entry_px=105.0, stop_px=95.0)
        lot.reprice_bar_time = 200.0
        attach(mgr, lot, FakeTrade(8801, status="Submitted", qty=100),
               alive_trail(lot))
        ok, msg = await mgr.reprice_retest_lot(lot.lot_id, 104.0, 96.0, 200.0,
                                               new_qty=111)
        assert not ok and "已尝试过改价" in msg
        assert gw.modify_ls_calls == [] and gw.modify_ls_qty_calls == []
    run(go())


def test_g2_retry_next_bar(tmp_path):
    """G2: 改价失败 → bar_time 已记录、原单保留；下一根 bar 可重试成功。"""
    async def go():
        gw = FakeGW()
        gw.fail_modify_ls_once = True
        mgr = mk_mgr(gw, tmp_path, "g2")
        lot = mk_lot(state=PENDING, qty=100, limit_type="retest",
                     entry_px=105.0, stop_px=95.0)
        lot.reprice_bar_time = 100.0
        attach(mgr, lot, FakeTrade(8801, status="Submitted", qty=100),
               alive_trail(lot))
        ok, msg = await mgr.reprice_retest_lot(lot.lot_id, 104.0, 96.0, 200.0,
                                               new_qty=100)
        assert not ok and lot.entry_px == 105.0 and lot.stop_px == 95.0
        assert lot.reprice_bar_time == 200.0
        ok, msg = await mgr.reprice_retest_lot(lot.lot_id, 104.0, 96.0, 300.0,
                                               new_qty=100)
        assert ok and lot.entry_px == 104.0 and lot.stop_px == 96.0
        assert gw.modify_ls_calls == [(104.0, 96.0)]
    run(go())


def test_g3_active_reprice_rejected(tmp_path):
    """G3: ACTIVE 拒绝改价。"""
    async def go():
        gw = FakeGW()
        mgr = mk_mgr(gw, tmp_path, "g3")
        lot = mk_lot(state=ACTIVE, qty=100, limit_type="retest")
        lot.filled_qty = 100
        mgr._lots[lot.lot_id] = lot
        ok, msg = await mgr.reprice_retest_lot(lot.lot_id, 104.0, 96.0, 300.0,
                                               new_qty=100)
        assert not ok and "不可改价" in msg
        assert gw.modify_ls_calls == [] and gw.modify_ls_qty_calls == []
    run(go())


def test_g4_qty_follows_risk(tmp_path):
    """G4: 距离减半→股数翻倍（modify_limit_stop_qty），init_* 基准不动。"""
    async def go():
        gw = FakeGW()
        mgr = mk_mgr(gw, tmp_path, "g4")
        lot = mk_lot(state=PENDING, qty=100, limit_type="retest",
                     entry_px=105.0, stop_px=95.0)   # init_risk=1000
        attach(mgr, lot, FakeTrade(8801, status="Submitted", qty=100),
               alive_trail(lot))
        ok, msg = await mgr.reprice_retest_lot(lot.lot_id, 104.0, 99.0, 200.0,
                                               new_qty=200)
        assert ok and gw.modify_ls_qty_calls == [(104.0, 99.0, 200)]
        assert lot.qty == 200 and lot.init_entry_px == 105.0 \
            and lot.init_risk_usd == 1000.0
    run(go())


def test_g5_partial_fill_price_only(tmp_path):
    """G5: 部分成交后只改价不改股数。"""
    async def go():
        gw = FakeGW()
        mgr = mk_mgr(gw, tmp_path, "g5")
        lot = mk_lot(state=PENDING, qty=100, limit_type="retest",
                     entry_px=105.0, stop_px=95.0)
        lot.filled_qty = 50
        attach(mgr, lot, FakeTrade(8801, status="Submitted", filled=50,
                                   avg=104.9, qty=100),
               alive_trail(lot))
        ok, msg = await mgr.reprice_retest_lot(lot.lot_id, 104.0, 99.0, 200.0,
                                               new_qty=0)
        assert ok and gw.modify_ls_calls == [(104.0, 99.0)]
        assert gw.modify_ls_qty_calls == [] and lot.qty == 100
    run(go())


# ==================== H. reconcile ====================

def test_h1_protection_terminal_auto_restore(tmp_path):
    """H1: ACTIVE lot 保护单终态（无成交）→ 自动 _restore_protection。"""
    async def go():
        gw = FakeGW(positions={"QCOM": 100.0})
        mgr = mk_mgr(gw, tmp_path, "h1")
        lot = mk_lot(state=ACTIVE, qty=100)
        lot.filled_qty = 100
        lot.avg_fill = 100.0
        dead_trail = FakeTrade(8802, status="Cancelled", qty=100,
                               action="SELL", order_type="STP", aux=95.0)
        attach(mgr, lot, None, dead_trail)
        gw._trades = {8802: dead_trail}
        await mgr.reconcile()
        assert lot.state == ACTIVE
        assert gw.stop_only_calls and gw.stop_only_calls[-1][2] == 100
        assert lot.trail_trade is not None and not lot.trail_trade.isDone()
        assert lot.protection_restore_needed is False
        cancel_watchers(mgr)
    run(go())


def test_h2_revive_cap_and_idempotent(tmp_path):
    """H2: 复活 cap=券商余仓（400<真相 1000），无 overfill 平仓；只复活一次。"""
    async def go():
        gw = FakeGW(fills={8801: (1000, 100.05)}, positions={"QCOM": 400.0})
        mgr = mk_mgr(gw, tmp_path, "h2")
        revived = []
        mgr.on_lot_revived = revived.append
        lot = mk_lot(state=CANCELLED, qty=1000)
        lot.filled_qty = 0
        mgr._lots[lot.lot_id] = lot
        await mgr.reconcile()
        assert lot.state == ACTIVE and lot.filled_qty == 400
        assert abs(lot.avg_fill - 100.05) < 1e-6
        assert gw.close_calls == [], "cap 场景不应 overfill 平仓"
        assert gw.stop_only_calls and gw.stop_only_calls[-1][2] == 400
        assert len(revived) == 1
        lot.state = CANCELLED          # 人为再终结，第二轮不再复活
        lot.filled_qty = 0
        n_stops = len(gw.stop_only_calls)
        await mgr.reconcile()
        assert len(gw.stop_only_calls) == n_stops and lot.state == CANCELLED
        cancel_watchers(mgr)
    run(go())


def test_h3_pending_truth_query_fails_stays_pending(tmp_path):
    """H3a: PENDING 入场单句柄丢失 + 成交真相查询失败 → 保持 PENDING 待对账，
    不撤不改（绝不误判）。"""
    async def go():
        gw = FakeGW(fills=None, positions={})   # fills=None = 真相查询失败
        mgr = mk_mgr(gw, tmp_path, "h3a")
        lot = mk_lot(state=PENDING, qty=100)
        attach(mgr, lot, None, None)    # 重启后句柄全丢
        await mgr.reconcile()
        assert lot.state == PENDING
        assert gw.cancel_calls == [] and gw.stop_px_calls == [] \
            and gw.stop_qty_calls == []
        cancel_watchers(mgr)
    run(go())


def test_h3_pending_no_fill_converges_cancelled(tmp_path):
    """H3b: PENDING 入场单不在开放快照且成交真相确无成交 → 收敛 CANCELLED
    （2026-10 起现行语义：绝不永远挂 PENDING；不撤单——订单券商侧已不可见）。"""
    async def go():
        gw = FakeGW(fills={}, positions={})
        mgr = mk_mgr(gw, tmp_path, "h3b")
        lot = mk_lot(state=PENDING, qty=100)
        attach(mgr, lot, None, None)
        await mgr.reconcile()
        assert lot.state == CANCELLED
        assert gw.cancel_calls == [] and gw.stop_px_calls == [] \
            and gw.stop_qty_calls == []
        cancel_watchers(mgr)
    run(go())


# ==================== I. 幻影守卫边界 ====================

def test_i_phantom_guard_boundaries(tmp_path):
    """I: 反向持仓/不足额/查询失败均拦截，足额同向放行。"""
    async def go():
        gw = FakeGW(positions={"QCOM": -100.0})
        mgr = mk_mgr(gw, tmp_path, "i1")
        lot = mk_lot(state=ACTIVE, qty=100)
        lot.filled_qty = 100
        lot.avg_fill = 100.0
        mgr._lots[lot.lot_id] = lot
        ok, err = await mgr._has_broker_position(lot)
        assert not ok and "幻影" in err           # I1 反向持仓拦截
        gw._pos = {"QCOM": 50.0}
        ok, err = await mgr._has_broker_position(lot)
        assert not ok                             # I2 不足额拦截
        lot_s = mk_lot(lot_id="lot-s", state=ACTIVE, qty=100, side="short",
                       entry_px=100.0, stop_px=105.0)
        lot_s.filled_qty = 100
        lot_s.avg_fill = 100.0
        gw._pos = {"QCOM": 100.0}
        ok, err = await mgr._has_broker_position(lot_s)
        assert not ok                             # I3 空仓 vs 券商多仓拦截
        gw._pos = {"QCOM": -100.0}
        ok, err = await mgr._has_broker_position(lot_s)
        assert ok, f"I3 足额空仓被误拦: {err}"
        gw._pos = None
        ok, err = await mgr._has_broker_position(lot)
        assert not ok and "查询失败" in err       # I4 查询失败拦截
    run(go())


# ==================== J. 并发安全 ====================

def test_j1_trail_breakeven_serialized(tmp_path):
    """J1: update_trail × breakeven 并发 → 串行化（改单不重叠）。"""
    async def go():
        gw = FakeGW()
        gw.modify_delay = 0.05   # 拉长改单窗口，强制交错
        mgr = mk_mgr(gw, tmp_path, "j1")
        lot = mk_lot(state=ACTIVE, qty=100)
        lot.filled_qty = 100
        lot.avg_fill = 100.0
        attach(mgr, lot, None, alive_trail(lot))
        await asyncio.gather(mgr.update_trail(lot.lot_id, 101.0),
                             mgr.breakeven(lot.lot_id))
        assert gw.max_concurrent_modify == 1, \
            f"改单并发重叠! max={gw.max_concurrent_modify}"
        assert lot.stop_px in (100.0, 101.0)
        cancel_watchers(mgr)
    run(go())


def test_j2_concurrent_breakeven_single_modify(tmp_path):
    """J2: 两个 breakeven 并发 → 只改一次单（_lot_lock 幂等）。"""
    async def go():
        gw = FakeGW()
        gw.modify_delay = 0.05
        mgr = mk_mgr(gw, tmp_path, "j2")
        lot = mk_lot(state=ACTIVE, qty=100)
        lot.filled_qty = 100
        lot.avg_fill = 100.0
        attach(mgr, lot, None, alive_trail(lot))
        r = await asyncio.gather(mgr.breakeven(lot.lot_id),
                                 mgr.breakeven(lot.lot_id))
        assert all(ok for ok, _ in r)
        assert gw.stop_px_calls == [100.0], f"并发保本重复发单: {gw.stop_px_calls}"
        assert gw.max_concurrent_modify == 1
        cancel_watchers(mgr)
    run(go())


def test_symbol_position_cap_counts_pending_and_active_across_batches(tmp_path):
    mgr = mk_mgr(FakeGW(fills={}), tmp_path, 'symbol_cap')
    mgr.position_equity = lambda: 10000
    active = mk_lot('active', state=ACTIVE, qty=80)
    active.filled_qty = 60
    active.close_filled_qty = 20
    attach(mgr, active)
    attach(mgr, mk_lot('pending', qty=40, side='short', sym='NYSE:QCOM'))
    attach(mgr, mk_lot('closed', state=CLOSED, qty=10000))
    assert mgr._symbol_position_block('NASDAQ:QCOM', 40, 100) == ''
    assert '140%' in mgr._symbol_position_block('NASDAQ:QCOM', 41, 100)
    assert mgr._symbol_position_block('NASDAQ:OTHER', 140, 100) == ''
    assert mgr._symbol_position_block('NASDAQ:QCOM', 80, 100, 'pending') == ''
    mgr.position_equity = lambda: float('nan')
    assert '净值未就绪' in mgr._symbol_position_block('NASDAQ:QCOM', 1, 100)


def test_symbol_cap_blocks_automatic_entry_before_broker_submission(tmp_path):
    async def go():
        mgr = mk_mgr(FakeGW(fills={}), tmp_path, 'auto_symbol_cap')
        mgr.position_equity = lambda: 10000
        lot, error = await mgr.place_limit_stop_lot(
            'NASDAQ:QCOM', 'short', 141, 100, 102,
            limit_type='auto_ema7_autotrade_v2', entry_order_type='MKT')
        assert lot is None and '140%' in error
        assert not mgr._lots
    run(go())


def test_manual_market_entry_once_until_existing_batch_is_finished(tmp_path):
    async def go():
        for state in (PENDING, ACTIVE, CLOSING):
            mgr = mk_mgr(FakeGW(fills={}), tmp_path, f'manual_once_{state}')
            mgr.recovery_status = lambda: {"blocks_entry": False}
            attach(mgr, mk_lot('first', state=state, qty=10, sym='NYSE:QCOM'))
            lot, error = await mgr.place_limit_stop_lot(
                'NASDAQ:QCOM', 'long', 10, 100, 95,
                limit_type='market', manual_entry=True)
            assert lot is None and '只允许建仓一次' in error
            assert len(mgr._lots) == 1
    run(go())


def test_golden_entry_limit_uses_same_direction_fill_and_pending_state(tmp_path):
    async def go():
        mgr = mk_mgr(FakeGW(fills={}), tmp_path, "golden_daily")
        mgr.recovery_status = lambda: {"blocks_entry": False}
        mgr.market_health = lambda: {"trade_allowed": False, "reason": "test safety stub"}
        first = mk_lot("first", state=CLOSED, sym="NYSE:QCOM")
        first.signal_snapshot = {"strategy_id": "golden_strategy", "strategy_instance_id": "first"}
        first.entry_cumulative_qty = 10
        first.entry_broker_time = time.time()
        attach(mgr, first)

        async def submit(side):
            return (await mgr.place_limit_stop_lot(
                "NASDAQ:QCOM", side, 10, 100, 95 if side == "long" else 105,
                signal_snapshot={"strategy_id": "golden_strategy", "strategy_instance_id": "second"},
            ))[1]

        assert "本日同方向已成交" in await submit("long")
        assert "行情数据陈旧" in await submit("short")
        first.entry_broker_time -= 86400
        assert "行情数据陈旧" in await submit("long")
        first.entry_submission_pending = True
        assert "同方向已有未结束订单" in await submit("long")
        first.entry_submission_pending = False
        first.state = CANCELLED
        first.entry_cumulative_qty = 0
        assert "行情数据陈旧" in await submit("long")
    run(go())
