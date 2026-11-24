# -*- coding: utf-8 -*-
"""总风险敞口预算回归（execution/risk.py + order_lot.py）。

守的口径：PENDING 计全额计划风险、ACTIVE 按（avg_fill−stop）计、保本后
敞口归零并释放预算（闸门从拒到放）、空仓镜像、超预算拦截（严格大于才拒、
原因带数字）、预算 0=不限制。halt_dir/state_path 全在 tmp_path。
"""
from execution.risk import RiskConfig, RiskGate
from execution.order_lot import OrderManager
from .conftest import run


class FakeGateway:
    """dry-run 假网关：place_limit_with_stop 直接虚拟成交。"""
    enabled = False
    _connected = False

    def __init__(self, dry_run=True):
        self._dry_run = dry_run

    async def realized_pnl(self):
        return 0.0

    async def place_limit_with_stop(self, symbol, side, qty, limit_px, stop_px):
        if self._dry_run:
            return {"ok": True, "dry_run": True, "entry_order_id": 1,
                    "stop_order_id": 2}
        return {"ok": True, "entry_order_id": 1, "stop_order_id": 2}

    async def place_midpoint_oca(self, *a, **kw):
        return {"ok": True, "entry_order_id": 1, "stop_order_id": 2}


def make_mgr(tmp_path, max_total=1500.0, dry_run=True, name="lots_state.json"):
    gate = RiskGate(RiskConfig(max_open_positions=0,
                               max_total_risk_usd=max_total),
                    halt_dir=tmp_path)
    return OrderManager(FakeGateway(dry_run), gate,
                        state_path=tmp_path / name)


def test_pending_full_plan_risk(tmp_path):
    """① PENDING 全额计划风险敞口。"""
    async def go():
        mgr = make_mgr(tmp_path, dry_run=False)
        lot, err = await mgr.place_limit_stop_lot(
            "NASDAQ:AAA", "long", 100, 100.0, 97.0)
        assert lot is not None and lot.state == "PENDING", (lot, err)
        exp = mgr.risk_exposure()
        assert exp["total"] == 300.0 and exp["lots"][0]["risk_usd"] == 300.0
    run(go())


def test_manual_add_allowed_for_auto_position_even_with_symbol_alias(tmp_path):
    async def go():
        mgr = make_mgr(tmp_path)
        auto, err = await mgr.place_limit_stop_lot(
            "NASDAQ:AAA", "long", 10, 100, 99,
            signal_snapshot={"strategy_id": "fixture_pullback"},
        )
        assert auto is not None, err
        added, err = await mgr.place_limit_stop_lot(
            "AAA", "long", 10, 100, 99, manual_entry=True,
        )
        assert added is not None and added.lot_id != auto.lot_id, err
        other, err = await mgr.place_limit_stop_lot(
            "NASDAQ:BBB", "long", 10, 100, 99, manual_entry=True,
        )
        assert other is not None, err
        auto_add, err = await mgr.place_limit_stop_lot(
            "NASDAQ:AAA", "long", 10, 101, 99, trader="auto",
        )
        assert auto_add is not None, err
    run(go())


def test_active_partial_exposure(tmp_path):
    """② ACTIVE 未保本部分敞口（止损上移一半）；avg_fill 缺失回退 entry_px。"""
    async def go():
        mgr = make_mgr(tmp_path, dry_run=True)
        lot, _ = await mgr.place_limit_stop_lot("NASDAQ:BBB", "long", 100, 100.0, 97.0)
        assert lot.state == "ACTIVE" and lot.avg_fill == 100.0
        lot.stop_px = 98.5
        assert mgr.risk_exposure()["total"] == 150.0
        lot.avg_fill = 0.0
        assert mgr.risk_exposure()["total"] == 150.0
    run(go())


def test_breakeven_frees_budget(tmp_path):
    """③ 保本（stop=成本）敞口=0，空出预算后闸门从拒到放。"""
    async def go():
        mgr = make_mgr(tmp_path, max_total=350.0, dry_run=True)
        lot, _ = await mgr.place_limit_stop_lot("NASDAQ:CCC", "long", 100, 100.0, 97.0)
        assert mgr.risk_exposure()["total"] == 300.0
        lot2, err = await mgr.place_limit_stop_lot("NASDAQ:DDD", "long", 100, 50.0, 47.0)
        assert lot2 is None, "敞口 300+300=600 超预算 350 应拦截"
        lot.stop_px = 100.0          # 止损移到成本（保本）
        assert mgr.risk_exposure()["total"] == 0.0
        lot2, err = await mgr.place_limit_stop_lot("NASDAQ:DDD", "long", 100, 51.0, 48.0)
        assert lot2 is not None, f"保本后应放行: {err}"
    run(go())


def test_short_mirror(tmp_path):
    """④ 空仓镜像：max(0, stop − avg_fill)，300→150→0→0。"""
    async def go():
        mgr = make_mgr(tmp_path, dry_run=True)
        lot, _ = await mgr.place_limit_stop_lot("NASDAQ:EEE", "short", 100, 100.0, 103.0)
        assert mgr.risk_exposure()["total"] == 300.0
        lot.stop_px = 101.5
        assert mgr.risk_exposure()["total"] == 150.0
        lot.stop_px = 100.0          # 保本
        assert mgr.risk_exposure()["total"] == 0.0
        lot.stop_px = 99.0           # 更有利
        assert mgr.risk_exposure()["total"] == 0.0
    run(go())


def test_budget_boundary_strictly_greater(tmp_path):
    """⑤ 总敞口 1490 + 新单 310 → 拒（原因带数字）；恰好等于预算放行。"""
    async def go():
        mgr = make_mgr(tmp_path, max_total=1500.0, dry_run=False)
        lot, _ = await mgr.place_limit_stop_lot("NASDAQ:FFF", "long", 149, 100.0, 90.0)
        assert mgr.risk_exposure()["total"] == 1490.0
        lot2, err = await mgr.place_limit_stop_lot("NASDAQ:GGG", "long", 31, 100.0, 90.0)
        assert lot2 is None
        assert "1490+310=1800" in err and "1500" in err
        ok, why = mgr._risk.check_new_order("NASDAQ:GGG", "long", 100.0, 90.0, 1,
                                            open_positions=1,
                                            open_risk_usd=1490.0)
        assert ok, why  # 1490+10=1500 恰好等于预算，不拦截（严格大于才拒）
    run(go())


def test_zero_budget_unlimited(tmp_path):
    """⑥ 预算 0 = 不限制。"""
    async def go():
        mgr = make_mgr(tmp_path, max_total=0.0, dry_run=False)
        await mgr.place_limit_stop_lot("NASDAQ:HHH", "long", 149, 100.0, 90.0)
        lot2, err = await mgr.place_limit_stop_lot("NASDAQ:III", "long", 100, 100.0, 90.0)
        assert lot2 is not None, f"预算 0 应不限制: {err}"
    run(go())


def test_direction_budget_counts_pending_and_releases_after_breakeven(tmp_path):
    async def go():
        gate = RiskGate(RiskConfig(max_order_qty=10000, max_open_positions=0,
            max_total_risk_usd=2200, max_direction_risk_usd=1100), halt_dir=tmp_path)
        mgr = OrderManager(FakeGateway(False), gate, state_path=tmp_path / 'directions.json')
        long, err = await mgr.place_limit_stop_lot('AAA', 'long', 100, 100, 90)
        assert long is not None, err
        blocked, err = await mgr.place_limit_stop_lot('BBB', 'long', 100, 100, 90)
        assert blocked is None and '多头风险敞口' in err and '1100' in err
        short, err = await mgr.place_limit_stop_lot('CCC', 'short', 100, 100, 110)
        assert short is not None, err
        assert mgr.risk_exposure()['total'] == 2000
        assert mgr.risk_exposure()['long'] == mgr.risk_exposure()['short'] == 1000
        long.state = 'ACTIVE'; long.filled_qty = 100; long.avg_fill = 100; long.stop_px = 100
        assert mgr.risk_exposure()['long'] == 0
        again, err = await mgr.place_limit_stop_lot('DDD', 'long', 100, 100, 90)
        assert again is not None, err
        short.state = 'ACTIVE'; short.filled_qty = 100; short.avg_fill = 100; short.stop_px = 100
        assert mgr.risk_exposure()['short'] == 0
    run(go())


def test_direction_budget_boundaries_and_total_are_independent(tmp_path):
    gate = RiskGate(RiskConfig(max_order_qty=10000, max_open_positions=0,
        max_total_risk_usd=2200, max_direction_risk_usd=1100), halt_dir=tmp_path)
    args = dict(symbol='A',side='long',entry=100,stop=99,qty=1000,open_positions=0,
                open_risk_usd=1200,open_direction_risk_usd=100)
    assert gate.check_new_order(**args)[0]  # Both exact boundaries allowed.
    assert '多头' in gate.check_new_order(**{**args,'open_direction_risk_usd':101})[1]
    assert '总风险' in gate.check_new_order(**{**args,'open_risk_usd':1201})[1]


def test_concurrent_same_direction_submissions_cannot_both_spend_1100(tmp_path):
    import asyncio
    async def go():
        gate = RiskGate(RiskConfig(max_order_qty=10000, max_open_positions=0,
            max_total_risk_usd=2200, max_direction_risk_usd=1100), halt_dir=tmp_path)
        mgr = OrderManager(FakeGateway(False), gate, state_path=tmp_path / 'concurrent.json')
        results = await asyncio.gather(*[mgr.place_limit_stop_lot(s,'long',100,100,90) for s in ('AAA','BBB')])
        assert sum(lot is not None for lot, _ in results) == 1
        assert mgr.risk_exposure()['long'] == 1000
        assert any('多头风险敞口' in err for _,err in results)
    run(go())
