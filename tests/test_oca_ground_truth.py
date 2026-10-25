# -*- coding: utf-8 -*-
"""OCA 竞态漏报修复回归（execution/order_lot.py）。

守的口径：LMT 缓存显示 Cancelled 但突破腿真实成交时，必须以成交真相为准
激活（绝不误判 CANCELLED）；真相查询失败保持 PENDING 待对账；双腿双成
overfill 自动平掉多余股数；对账复活 cap 到券商余仓且每 lot 只复活一次；
券商无持仓不复活（无幻影仓）。全部 mock gateway，状态文件在 tmp_path。
"""
import time

from execution.order_lot import OrderManager, OrderLot, PENDING, ACTIVE, CANCELLED
from .conftest import run


class FakeTrade:
    """最小 Trade 模拟：orderStatus/isDone/order/contract。"""
    def __init__(self, order_id, status="Cancelled", filled=0, qty=0, action="SELL",
                 order_type="LMT", symbol="QCOM"):
        self.order = type("O", (), {"orderId": order_id, "totalQuantity": qty,
                                    "action": action, "orderType": order_type,
                                    "auxPrice": 0.0, "parentId": 0})()
        self.contract = type("C", (), {"symbol": symbol})()
        self.orderStatus = type("S", (), {"status": status, "filled": filled,
                                          "avgFillPrice": 0.0})()

    def isDone(self):
        return self.orderStatus.status in ("Filled", "Cancelled", "Inactive",
                                           "ApiCancelled")


class FakeGW:
    enabled = True
    _connected = True

    def __init__(self, fills={}, positions=None):
        self._fills = fills          # None = 查询失败
        self._pos = positions or {}
        self.stop_orders = []   # place_stop_only 记录
        self.close_orders = []

    async def entry_fills_by_order(self, symbol, order_ids):
        if self._fills is None:
            return None
        return {oid: v for oid, v in self._fills.items() if oid in set(order_ids)}

    async def trade_map(self):
        return {}

    async def positions(self):
        return dict(self._pos)

    async def place_stop_only(self, symbol, side, qty, stop_px):
        self.stop_orders.append((symbol, side, qty, stop_px))
        return {"ok": True, "order_id": 88000001, "stop_order_id": 88000001,
                "trade": None}

    async def place_close(self, symbol, side, qty, ref, order_ref=""):
        self.close_orders.append((symbol, side, qty, order_ref))
        return {"ok": True, "order_id": 88000002, "trade": None}


class FakeRisk:
    pass


def mk_lot(lot_id="lot-x", state=PENDING, qty=1025):
    return OrderLot(lot_id=lot_id, symbol="NASDAQ:QCOM", side="long", qty=qty,
                    entry_px=176.25, stop_px=175.5, trail_amount=0.0,
                    init_entry_px=176.25, init_stop_px=175.5,
                    init_risk_usd=768.75, state=state,
                    entry_order_id=88000362, breakout_order_id=88000364,
                    entry_time=time.time())


def test_s1_oca_race_breakout_leg_filled(tmp_path):
    """场景1：OCA 竞态——LMT 缓存 Cancelled 无成交，真相是突破腿成交 1025。"""
    async def go():
        gw = FakeGW(fills={88000364: (1025, 176.253)}, positions={"QCOM": 1025.0})
        mgr = OrderManager(gw, FakeRisk(), state_path=tmp_path / "lots.json")
        lot = mk_lot()
        lot.entry_trade = FakeTrade(88000362, "Cancelled", filled=0, qty=1025, action="BUY")
        lot.trail_trade = FakeTrade(88000363, "Cancelled", filled=0, order_type="STP")
        lot.breakout_trade = FakeTrade(88000364, "Filled", filled=1025, qty=1025,
                                       action="BUY", order_type="STP")
        mgr._lots[lot.lot_id] = lot
        ok = await mgr._handle_entry_terminal(lot)
        assert ok and lot.state == ACTIVE, f"状态错误: {lot.state}"
        assert lot.filled_qty == 1025 and abs(lot.avg_fill - 176.253) < 0.001
        assert lot.breakout_filled and lot.entry_order_id == 88000364
        assert gw.stop_orders == [("NASDAQ:QCOM", "long", 1025, 175.5)]
    run(go())


def test_s2_truth_query_fails_stays_pending(tmp_path):
    """场景2：真相查询失败 → 保持 PENDING，绝不误判 CANCELLED。"""
    async def go():
        gw = FakeGW(fills=None, positions={})
        mgr = OrderManager(gw, FakeRisk(), state_path=tmp_path / "lots.json")
        lot = mk_lot(lot_id="lot-y")
        lot.entry_trade = FakeTrade(88000362, "Cancelled", filled=0, qty=1025, action="BUY")
        lot.trail_trade = FakeTrade(88000363, "Cancelled", filled=0, order_type="STP")
        mgr._lots[lot.lot_id] = lot
        ok = await mgr._handle_entry_terminal(lot)
        assert not ok and lot.state == PENDING, f"错误: {lot.state}"
    run(go())


def test_s3_truth_no_fill_normal_cancel(tmp_path):
    """场景3：真相确无成交 → 正常 CANCELLED。"""
    async def go():
        gw = FakeGW(fills={}, positions={})
        mgr = OrderManager(gw, FakeRisk(), state_path=tmp_path / "lots.json")
        lot = mk_lot(lot_id="lot-z")
        lot.entry_trade = FakeTrade(88000362, "Cancelled", filled=0, qty=1025, action="BUY")
        lot.trail_trade = FakeTrade(88000363, "Cancelled", filled=0, order_type="STP")
        mgr._lots[lot.lot_id] = lot
        ok = await mgr._handle_entry_terminal(lot)
        assert ok and lot.state == CANCELLED, f"错误: {lot.state}"
    run(go())


def test_s4_double_fill_overfill_autoclose(tmp_path):
    """场景4：双腿双成 overfill → 多余 1025 股自动平仓，保护单按计划股数。"""
    async def go():
        gw = FakeGW(fills={88000362: (1025, 176.25), 88000364: (1025, 176.26)},
                    positions={"QCOM": 2050.0})
        mgr = OrderManager(gw, FakeRisk(), state_path=tmp_path / "lots.json")
        lot = mk_lot(lot_id="lot-w")
        lot.entry_trade = FakeTrade(88000362, "Cancelled", filled=0, qty=1025, action="BUY")
        lot.trail_trade = FakeTrade(88000363, "Cancelled", filled=0, order_type="STP")
        mgr._lots[lot.lot_id] = lot
        ok = await mgr._handle_entry_terminal(lot)
        assert ok and lot.state == ACTIVE
        assert lot.filled_qty == 1025
        assert gw.close_orders == [("NASDAQ:QCOM", "long", 1025,
                                    "oca-overfill-lot-w")]
        assert gw.stop_orders and gw.stop_orders[-1][2] == 1025
    run(go())


def test_s5_reconcile_revive_once(tmp_path):
    """场景5：对账复活——CANCELLED lot + 券商有持仓 + 真相有成交；只复活一次。"""
    async def go():
        gw = FakeGW(fills={88000364: (1025, 176.253)}, positions={"QCOM": 1025.0})
        mgr = OrderManager(gw, FakeRisk(), state_path=tmp_path / "lots.json")
        lot = mk_lot(lot_id="lot-v", state=CANCELLED)
        mgr._lots[lot.lot_id] = lot
        await mgr.reconcile()
        assert lot.state == ACTIVE and lot.filled_qty == 1025
        assert gw.stop_orders, "复活后未恢复保护单"
        assert "lot-v" in mgr._revive_done
        n_stops = len(gw.stop_orders)
        lot.state = CANCELLED      # 人为再终态
        lot.filled_qty = 0
        await mgr.reconcile()
        assert len(gw.stop_orders) == n_stops, "复活不幂等"
    run(go())


def test_s6_no_broker_position_no_revive(tmp_path):
    """场景6：券商无持仓 → 不复活（用户已手动平仓的旧单，无幻影）。"""
    async def go():
        gw = FakeGW(fills={88000364: (1025, 176.253)}, positions={})
        mgr = OrderManager(gw, FakeRisk(), state_path=tmp_path / "lots.json")
        lot = mk_lot(lot_id="lot-u", state=CANCELLED)
        mgr._lots[lot.lot_id] = lot
        await mgr.reconcile()
        assert lot.state == CANCELLED
    run(go())
