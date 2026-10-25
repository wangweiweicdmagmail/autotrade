# -*- coding: utf-8 -*-
"""回踩单逐 bar 改价 + 以损定量改股数回归（execution/order_lot.py）。

守的口径：固定风险预算（init_risk_usd 锚点不动）按新止损距离重新以损定量；
仅 filled_qty==0 时改股数（部分成交后只改价）；qty<1 拒绝且原单不动、
记录 bar_time 下根重试；价/量均未变返回 unchanged 不发起修改；dry-run
网关分支。mock gateway，状态文件在 tmp_path。
"""
from types import SimpleNamespace

from execution.order_lot import OrderLot, OrderManager, PENDING
from execution.ibkr import IBKRGateway
from .conftest import run


class MockGateway:
    """记录调用的 mock 券商网关。"""
    def __init__(self):
        self.calls = []          # [(method, limit, stop, qty_or_None)]

    async def modify_limit_stop(self, unit, new_limit, new_stop):
        self.calls.append(("modify_limit_stop", new_limit, new_stop, None))
        unit.entry_px = round(new_limit, 2)
        unit.stop_px = round(new_stop, 2)
        return True

    async def modify_limit_stop_qty(self, unit, new_limit, new_stop, new_qty):
        self.calls.append(("modify_limit_stop_qty", new_limit, new_stop, new_qty))
        unit.entry_px = round(new_limit, 2)
        unit.stop_px = round(new_stop, 2)
        return True


def make_lot(**kw):
    d = dict(lot_id="lot-t1", symbol="TEST", side="long", qty=100,
             entry_px=105.0, stop_px=95.0, trail_amount=0.0,
             init_entry_px=105.0, init_stop_px=95.0,
             init_risk_usd=1000.0, limit_type="retest",
             reprice_bar_time=100.0)
    d.update(kw)
    return OrderLot(**d)


def qty_formula(lot, new_limit, new_stop, max_qty=100000):
    """与 console/app.py _retest_limit_reprice_loop 同款公式。"""
    if lot.filled_qty:
        return 0
    init_risk = lot.init_risk_usd or 0
    dist = abs(new_limit - new_stop)
    if init_risk > 0 and dist > 0:
        q = int(init_risk / dist)
        return min(q, max_qty) if max_qty > 0 else q
    return lot.qty


def test_dist_half_qty_double(tmp_path):
    """① 距离减半（10→5）：qty 100→200，走改价改量路径；R 基准不动。"""
    async def go():
        gw = MockGateway()
        mgr = OrderManager(gw, object(), state_path=tmp_path / "lots.json")
        lot = make_lot()
        mgr._lots[lot.lot_id] = lot
        q = qty_formula(lot, 104.0, 99.0)
        assert q == 200
        ok, err = await mgr.reprice_retest_lot(lot.lot_id, 104.0, 99.0, 200.0, new_qty=q)
        assert ok and err == ""
        assert gw.calls == [("modify_limit_stop_qty", 104.0, 99.0, 200)]
        assert lot.qty == 200 and lot.entry_px == 104.0 and lot.stop_px == 99.0
        assert lot.init_risk_usd == 1000.0 and lot.init_entry_px == 105.0
        assert lot.reprice_bar_time == 200.0
    run(go())


def test_same_qty_price_only(tmp_path):
    """② qty 相同价不同：只改价（纯价格路径），股数不变。"""
    async def go():
        gw = MockGateway()
        mgr = OrderManager(gw, object(), state_path=tmp_path / "lots.json")
        lot = make_lot()                      # qty=100, dist=10
        mgr._lots[lot.lot_id] = lot
        q = qty_formula(lot, 106.0, 96.0)     # dist 仍 10 → qty=100
        assert q == 100
        ok, err = await mgr.reprice_retest_lot(lot.lot_id, 106.0, 96.0, 200.0, new_qty=q)
        assert ok and err == ""
        assert gw.calls == [("modify_limit_stop", 106.0, 96.0, None)]
        assert lot.qty == 100 and lot.entry_px == 106.0 and lot.stop_px == 96.0
    run(go())


def test_partial_fill_price_only(tmp_path):
    """③ filled_qty>0：只改价不改股数。"""
    async def go():
        gw = MockGateway()
        mgr = OrderManager(gw, object(), state_path=tmp_path / "lots.json")
        lot = make_lot(filled_qty=50)
        mgr._lots[lot.lot_id] = lot
        q = qty_formula(lot, 104.0, 99.0)     # 部分成交 → 0（不改股数）
        assert q == 0
        ok, err = await mgr.reprice_retest_lot(lot.lot_id, 104.0, 99.0, 200.0, new_qty=q)
        assert ok and err == ""
        assert gw.calls == [("modify_limit_stop", 104.0, 99.0, None)]
        assert lot.qty == 100 and lot.filled_qty == 50
        assert lot.entry_px == 104.0 and lot.stop_px == 99.0
    run(go())


def test_qty_below_one_rejected(tmp_path):
    """④ qty_new<1：拒绝本次改价，原单不变，记录 bar_time 下根重试。"""
    async def go():
        gw = MockGateway()
        mgr = OrderManager(gw, object(), state_path=tmp_path / "lots.json")
        lot = make_lot()                      # init_risk=1000
        mgr._lots[lot.lot_id] = lot
        q = qty_formula(lot, 2105.0, 105.0)   # dist=2000 → int(1000/2000)=0
        assert q == 0
        ok, err = await mgr.reprice_retest_lot(lot.lot_id, 2105.0, 105.0, 200.0, new_qty=q)
        assert not ok and "不足 1 股" in err
        assert gw.calls == []
        assert lot.qty == 100 and lot.entry_px == 105.0 and lot.stop_px == 95.0
        assert lot.reprice_bar_time == 200.0
    run(go())


def test_unchanged_debounce(tmp_path):
    """⑤ 防抖：价与股数都未变 → unchanged，不发起修改。"""
    async def go():
        gw = MockGateway()
        mgr = OrderManager(gw, object(), state_path=tmp_path / "lots.json")
        lot = make_lot()
        mgr._lots[lot.lot_id] = lot
        q = qty_formula(lot, 105.0, 95.0)     # 与现单完全相同
        assert q == 100
        ok, err = await mgr.reprice_retest_lot(lot.lot_id, 105.0, 95.0, 200.0, new_qty=q)
        assert ok and err == "unchanged"
        assert gw.calls == []
        assert lot.reprice_bar_time == 200.0
    run(go())


def test_dry_run_gateway_branch():
    """⑥ dry-run 真网关分支：modify_limit_stop_qty 更新价、返回 True。"""
    async def go():
        gw = IBKRGateway.__new__(IBKRGateway)
        gw.enabled = False
        gw._connected = False
        unit = SimpleNamespace(symbol="TEST", entry_px=105.0, stop_px=95.0)
        ok = await gw.modify_limit_stop_qty(unit, 104.0, 99.0, 200)
        assert ok and unit.entry_px == 104.0 and unit.stop_px == 99.0
    run(go())
