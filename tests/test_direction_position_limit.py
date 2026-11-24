"""生产同向多笔，禁止多空并存；总预算2200/单笔1000，历史终态不占方向。"""

import asyncio
from dataclasses import replace

import pytest
import config

from execution.order_lot import OrderLot, OrderManager
from execution.risk import RiskConfig, RiskGate
from .conftest import run
from .test_risk_exposure import FakeGateway


class Gateway(FakeGateway):
    def __init__(self, dry_run=True):
        super().__init__(dry_run)
        self.calls = []

    async def place_limit_with_stop(self, symbol, side, qty, limit_px, stop_px):
        self.calls.append((symbol, side, 'LMT'))
        await asyncio.sleep(0)
        return await super().place_limit_with_stop(symbol, side, qty, limit_px, stop_px)

    async def place_market_with_stop(self, symbol, side, qty, stop_px):
        self.calls.append((symbol, side, 'MKT'))
        await asyncio.sleep(0)
        return {'ok': True, 'dry_run': self._dry_run, 'entry_order_id': 3, 'stop_order_id': 4}


def manager(tmp_path, *, dry_run=True, gateway=None, direction_limit=1, single_direction_only=False, direction_risk=1100):
    gate = RiskGate(RiskConfig(max_order_qty=10000, max_open_positions=0,
        max_total_risk_usd=2200, max_direction_risk_usd=direction_risk, max_direction_positions=direction_limit,
        single_direction_only=single_direction_only),
        halt_dir=tmp_path)
    return OrderManager(gateway or Gateway(dry_run), gate, tmp_path / 'lots.json')


def lot(state='ACTIVE', side='long', filled=1, pending=False):
    return OrderLot('seed', 'NASDAQ:AAA', side, 1, 100, 99 if side == 'long' else 101, 0,
        state=state, filled_qty=filled, avg_fill=100, entry_submission_pending=pending)


@pytest.mark.parametrize('state,filled,pending,expected', [
    ('PENDING', 0, False, 1), ('PENDING', 1, False, 1),
    ('ACTIVE', 1, False, 1), ('CLOSING', 1, False, 1),
    ('CLOSING', 0, False, 1),  # Await terminal confirmation, not just a local zero.
    ('FAILED', 0, True, 1), ('CANCELLED', 1, False, 0), ('CLOSED', 100, False, 0),
    ('CLOSED', 0, False, 0), ('CANCELLED', 0, False, 0), ('FAILED', 0, False, 0),
])
def test_direction_slot_follows_unfinished_intent_and_actual_remainder(tmp_path, state, filled, pending, expected):
    mgr = manager(tmp_path)
    seed = lot(state, filled=filled, pending=pending)
    seed.stop_px = 100  # A risk-free position still occupies its slot.
    seed.external_allocations = {'TEST_A': 1, 'TEST_B': 1}
    mgr._lots[seed.lot_id] = seed
    assert mgr.direction_position_counts() == {'long': expected, 'short': 0}
    seed.side = 'short'
    assert mgr.direction_position_counts() == {'long': 0, 'short': expected}


@pytest.mark.parametrize('side', ['long', 'short'])
def test_breakeven_does_not_free_direction_but_opposite_direction_is_independent(tmp_path, side):
    async def scenario():
        mgr = manager(tmp_path)
        stop = 99 if side == 'long' else 101
        first, error = await mgr.place_limit_stop_lot('AAA', side, 10, 100, stop)
        assert first is not None, error
        first.stop_px = 100
        assert mgr.risk_exposure()[side] == 0
        blocked, error = await mgr.place_limit_stop_lot('BBB', side, 1, 100, stop)
        assert blocked is None and '单方向最多1笔' in error
        other = 'short' if side == 'long' else 'long'
        accepted, error = await mgr.place_limit_stop_lot('CCC', other, 1, 100, 101 if other == 'short' else 99)
        assert accepted is not None, error
        assert mgr.direction_position_counts() == {'long': 1, 'short': 1}
        assert len(mgr._gw.calls) == 2
    run(scenario())


def test_pending_reserves_slot_and_only_confirmed_cancellation_releases_it(tmp_path):
    async def scenario():
        mgr = manager(tmp_path, dry_run=False)
        first, error = await mgr.place_limit_stop_lot('AAA', 'long', 1, 100, 99)
        assert first is not None and first.state == 'PENDING', error
        assert mgr.direction_position_counts()['long'] == 1
        blocked, error = await mgr.place_limit_stop_lot('BBB', 'long', 1, 100, 99)
        assert blocked is None and '单方向最多1笔' in error
        first.state = 'CANCELLED'; first.entry_submission_pending = False
        replacement, error = await mgr.place_limit_stop_lot('CCC', 'long', 1, 100, 99)
        assert replacement is not None, error
        assert mgr.direction_position_counts()['long'] == 1
        assert len(mgr._gw.calls) == 2
    run(scenario())


def test_concurrent_clients_cannot_reserve_the_same_direction_twice(tmp_path):
    async def scenario():
        mgr = manager(tmp_path, dry_run=False)
        results = await asyncio.gather(*[
            mgr.place_limit_stop_lot(symbol, 'long', 1, 100, 99, trader=trader)
            for symbol, trader in [('AAA', 'master'), ('BBB', 'client')]])
        assert sum(row is not None for row, _ in results) == 1
        assert any('单方向最多1笔' in error for _, error in results)
        assert len(mgr._gw.calls) == 1
        assert mgr.direction_position_counts()['long'] == 1
    run(scenario())


@pytest.mark.parametrize('source,meta,manual,order_type', [
    ('market', {}, True, 'MKT'), ('vwap_limit', {}, True, 'LMT'),
    ('auto_ema7', {'strategy_id': 'ema7_autotrade_v2'}, False, 'MKT'),
    ('golden', {'strategy_id': 'golden_strategy'}, False, 'MKT'),
])
def test_manual_auto_and_signal_entries_share_the_direction_gate(tmp_path, source, meta, manual, order_type):
    async def scenario():
        mgr = manager(tmp_path)
        first, error = await mgr.place_limit_stop_lot('AAA', 'long', 1, 100, 99)
        assert first is not None, error
        blocked, error = await mgr.place_limit_stop_lot('BBB', 'long', 1, 100, 99,
            limit_type=source, signal_snapshot=meta, manual_entry=manual, entry_order_type=order_type)
        assert blocked is None and '单方向最多1笔' in error
        assert len(mgr._gw.calls) == 1
    run(scenario())


def test_unknown_submission_and_restart_do_not_release_a_direction_or_resend(tmp_path):
    class UnknownGateway(Gateway):
        async def place_limit_with_stop(self, *args):
            self.calls.append(args)
            raise TimeoutError('unknown broker acceptance')

    async def scenario():
        gateway = UnknownGateway()
        mgr = manager(tmp_path, gateway=gateway)
        with pytest.raises(TimeoutError):
            await mgr.place_limit_stop_lot('AAA', 'short', 1, 100, 101)
        assert mgr.direction_position_counts()['short'] == 1
        restored = manager(tmp_path, gateway=gateway)
        assert restored.direction_position_counts()['short'] == 1
        rejected, _ = await restored.place_limit_stop_lot('BBB', 'short', 1, 100, 101)
        assert rejected is None and len(gateway.calls) == 1
    run(scenario())


def test_existing_over_limit_positions_are_preserved_and_block_only_additions(tmp_path):
    async def scenario():
        mgr = manager(tmp_path)
        first = lot()
        second = replace(first, lot_id='second', symbol='BBB')
        mgr._lots = {row.lot_id: row for row in (first, second)}
        rejected, error = await mgr.place_limit_stop_lot('CCC', 'long', 1, 100, 99)
        assert rejected is None and '已有2笔' in error
        assert all(row.state == 'ACTIVE' and row.filled_qty == 1 for row in mgr._lots.values())
        assert mgr._gw.calls == []
        first.state = second.state = 'CLOSED'; first.filled_qty = second.filled_qty = 0
        accepted, error = await mgr.place_limit_stop_lot('DDD', 'long', 1, 100, 99)
        assert accepted is not None, error
    run(scenario())


def test_a_configured_direction_gate_requires_an_explicit_count(tmp_path):
    gate = manager(tmp_path)._risk
    request = dict(symbol='AAA', side='long', entry=100, stop=99, qty=1, open_positions=0)
    for count in [None, -1, 1.2, True]:
        allowed, error = gate.check_new_order(**request, open_direction_positions=count)
        assert not allowed and '笔数未确认' in error
    assert gate.check_new_order(**request, open_direction_positions=0)[0]


@pytest.mark.parametrize('side', ['long', 'short'])
def test_current_production_config_allows_multiple_same_direction_positions(tmp_path, side):
    async def scenario():
        assert config.RISK_MAX_DIRECTION_POSITIONS == 0
        mgr = manager(tmp_path, direction_limit=config.RISK_MAX_DIRECTION_POSITIONS)
        for symbol in ['AAA', 'BBB', 'CCC']:
            accepted, error = await mgr.place_limit_stop_lot(symbol, side, 1, 100, 99 if side == 'long' else 101)
            assert accepted is not None, error
        assert mgr.direction_position_counts()[side] == 3
        assert len(mgr._gw.calls) == 3
    run(scenario())


def test_disabling_count_limit_keeps_risk_budget_and_breakeven_release(tmp_path):
    async def scenario():
        mgr = manager(tmp_path, direction_limit=config.RISK_MAX_DIRECTION_POSITIONS)
        first, error = await mgr.place_limit_stop_lot('AAA', 'long', 100, 100, 90)
        assert first is not None, error
        rejected, error = await mgr.place_limit_stop_lot('BBB', 'long', 100, 100, 90)
        assert rejected is None and '多头风险敞口' in error
        first.stop_px = 100
        accepted, error = await mgr.place_limit_stop_lot('CCC', 'long', 100, 100, 90)
        assert accepted is not None, error
        assert mgr.direction_position_counts()['long'] == 2
        assert mgr.risk_exposure()['long'] == 1000
    run(scenario())


def test_parallel_same_direction_orders_are_not_limited_to_one_after_cancellation(tmp_path):
    async def scenario():
        mgr = manager(tmp_path, dry_run=False, direction_limit=config.RISK_MAX_DIRECTION_POSITIONS)
        results = await asyncio.gather(*[
            mgr.place_limit_stop_lot(symbol, 'short', 1, 100, 101, trader=trader)
            for symbol, trader in [('AAA', 'master'), ('BBB', 'client')]])
        assert all(row is not None for row, _ in results), results
        assert mgr.direction_position_counts()['short'] == 2
        assert len(mgr._gw.calls) == 2
    run(scenario())


@pytest.mark.parametrize('side', ['long', 'short'])
@pytest.mark.parametrize('state,filled', [('PENDING', 0), ('ACTIVE', 1), ('CLOSING', 1)])
def test_explicit_legacy_direction_guard_blocks_opposite_orders_even_after_breakeven(tmp_path, side, state, filled):
    async def scenario():
        mgr = manager(tmp_path, direction_limit=config.RISK_MAX_DIRECTION_POSITIONS,
            single_direction_only=True)
        seed = lot(state, side, filled)
        seed.stop_px = 100
        seed.close_trade = object()  # Known in-flight close, not unknown submission.
        mgr._lots[seed.lot_id] = seed
        opposite = 'short' if side == 'long' else 'long'
        rejected, error = await mgr.place_limit_stop_lot('BBB', opposite, 1, 100,
            101 if opposite == 'short' else 99)
        assert rejected is None and '不允许多空并存' in error
        assert mgr._gw.calls == []
        seed.state = 'CLOSED'; seed.filled_qty = 0
        accepted, error = await mgr.place_limit_stop_lot('CCC', opposite, 1, 100,
            101 if opposite == 'short' else 99)
        assert accepted is not None, error
    run(scenario())


def test_competing_long_and_short_clients_cannot_both_submit(tmp_path):
    async def scenario():
        mgr = manager(tmp_path, dry_run=False, direction_limit=0, single_direction_only=True)
        results = await asyncio.gather(
            mgr.place_limit_stop_lot('AAA', 'long', 1, 100, 99, trader='master'),
            mgr.place_limit_stop_lot('BBB', 'short', 1, 100, 101, trader='client'))
        assert sum(row is not None for row, _ in results) == 1
        assert any('不允许多空并存' in error for _, error in results)
        assert len(mgr._gw.calls) == 1
    run(scenario())


def test_same_stock_can_add_to_auto_positions_but_not_exceed_aggregate_cap(tmp_path):
    async def scenario():
        mgr = manager(tmp_path, direction_limit=0, single_direction_only=True)
        mgr.position_equity = lambda: 1000
        first, error = await mgr.place_limit_stop_lot('NASDAQ:AAA', 'long', 10, 100, 99,
            signal_snapshot={'strategy_id': 'ema7_fixture'}, trader='auto')
        assert first is not None, error
        addition, error = await mgr.place_limit_stop_lot('AAA', 'long', 4, 100, 98,
            manual_entry=True, limit_type='market', entry_order_type='MKT')
        assert addition is not None and addition.lot_id != first.lot_id, error
        assert first.filled_qty == 10 and addition.filled_qty == 4
        rejected, error = await mgr.place_limit_stop_lot('AAA', 'long', 1, 100, 97,
            manual_entry=True, limit_type='vwap_limit')
        assert rejected is None and '140%' in error
        assert mgr.direction_position_counts() == {'long': 2, 'short': 0}
        assert len(mgr._gw.calls) == 2
    run(scenario())


def test_direction_guard_rejects_unknown_opposite_count_and_does_not_restrict_same_side(tmp_path):
    gate = manager(tmp_path, direction_limit=0, single_direction_only=True)._risk
    request = dict(symbol='AAA', side='long', entry=100, stop=99, qty=1,
        open_positions=5, open_direction_positions=5)
    for count in [None, -1, 1.2, True]:
        allowed, error = gate.check_new_order(**request, open_opposite_positions=count)
        assert not allowed and '方向未确认' in error
    assert gate.check_new_order(**request, open_opposite_positions=0)[0]


@pytest.mark.parametrize('source,meta,manual,order_type', [
    ('market', {}, True, 'MKT'), ('vwap_limit', {}, True, 'LMT'),
    ('auto_ema7', {'strategy_id': 'ema7_autotrade_v2'}, False, 'MKT'),
    ('golden', {'strategy_id': 'golden_strategy'}, False, 'MKT'),
])
def test_single_direction_guard_applies_to_manual_auto_and_signal_orders(tmp_path, source, meta, manual, order_type):
    async def scenario():
        mgr = manager(tmp_path, direction_limit=0, single_direction_only=True)
        first, error = await mgr.place_limit_stop_lot('AAA', 'long', 1, 100, 99)
        assert first is not None, error
        first.stop_px = 100
        rejected, error = await mgr.place_limit_stop_lot('BBB', 'short', 1, 100, 101,
            limit_type=source, signal_snapshot=meta, manual_entry=manual, entry_order_type=order_type)
        assert rejected is None and '不允许多空并存' in error
        addition, error = await mgr.place_limit_stop_lot('CCC', 'long', 1, 100, 99,
            limit_type=source, signal_snapshot=meta, manual_entry=manual, entry_order_type=order_type)
        assert addition is not None, error
        assert len(mgr._gw.calls) == 2
    run(scenario())


def test_current_production_configuration_blocks_opposite_positions(tmp_path):
    async def scenario():
        assert config.RISK_SINGLE_DIRECTION_ONLY is True
        mgr = manager(tmp_path, direction_limit=config.RISK_MAX_DIRECTION_POSITIONS,
            single_direction_only=config.RISK_SINGLE_DIRECTION_ONLY)
        accepted,error=await mgr.place_limit_stop_lot('AAA','long',1,100,99)
        assert accepted is not None,error
        rejected,error=await mgr.place_limit_stop_lot('BBB','short',1,100,101)
        assert rejected is None and '不允许多空并存' in error
        assert mgr.direction_position_counts() == {'long': 1, 'short': 0}
        assert len(mgr._gw.calls) == 1
    run(scenario())


def test_closed_history_quantities_are_not_current_positions_and_are_not_deleted(tmp_path):
    async def scenario():
        mgr = manager(tmp_path, direction_limit=0, single_direction_only=config.RISK_SINGLE_DIRECTION_ONLY)
        for i in range(78):
            seed = lot('CLOSED', 'short', filled=100)
            seed.lot_id = f'history-{i}'; seed.close_filled_qty = 100
            mgr._lots[seed.lot_id] = seed
        assert mgr.direction_position_counts() == {'long': 0, 'short': 0}
        accepted, error = await mgr.place_limit_stop_lot('BBB', 'long', 1, 100, 99)
        assert accepted is not None, error
        assert len(mgr._lots) == 79
        assert all(mgr._lots[f'history-{i}'].filled_qty == 100 for i in range(78))
        rejected,error=await mgr.place_limit_stop_lot('CCC','short',1,100,101)
        assert rejected is None and '已有多头1笔' in error and '78笔' not in error
        assert len(mgr._gw.calls)==1
    run(scenario())


@pytest.mark.parametrize('side',['long','short'])
def test_production_2200_budget_allows_two_1000_additions_and_blocks_a_third(tmp_path,side):
    async def scenario():
        assert config.RISK_PER_TRADE_USD==1000
        assert config.RISK_MAX_TOTAL_RISK_USD==config.RISK_MAX_DIRECTION_RISK_USD==2200
        mgr=manager(tmp_path,direction_limit=config.RISK_MAX_DIRECTION_POSITIONS,
            single_direction_only=config.RISK_SINGLE_DIRECTION_ONLY,direction_risk=config.RISK_MAX_DIRECTION_RISK_USD)
        stop=lambda price:price-10 if side=='long' else price+10
        first,error=await mgr.place_limit_stop_lot('AAA',side,100,100,stop(100),risk_usd=1000,manual_entry=True)
        assert first is not None,error
        second,error=await mgr.place_limit_stop_lot('AAA',side,100,101,stop(101),risk_usd=1000,manual_entry=True)
        assert second is not None and second.lot_id!=first.lot_id,error
        assert first.init_risk_usd==second.init_risk_usd==1000
        assert mgr.risk_exposure()['total']==2000
        rejected,error=await mgr.place_limit_stop_lot('BBB',side,100,102,stop(102),risk_usd=1000)
        assert rejected is None and '总风险敞口' in error
        assert len(mgr._gw.calls)==2
        first.stop_px=first.avg_fill
        third,error=await mgr.place_limit_stop_lot('CCC',side,100,103,stop(103),risk_usd=1000)
        assert third is not None,error
        assert mgr.risk_exposure()['total']==2000
        opposite='short' if side=='long' else 'long'
        rejected,error=await mgr.place_limit_stop_lot('DDD',opposite,1,100,101 if opposite=='short' else 99)
        assert rejected is None and '不允许多空并存' in error
    run(scenario())


def test_2200_total_budget_allows_exact_boundary_and_rejects_excess(tmp_path):
    gate=manager(tmp_path,direction_limit=0,single_direction_only=True,direction_risk=config.RISK_MAX_DIRECTION_RISK_USD)._risk
    request=dict(symbol='AAA',side='long',entry=100,stop=90,qty=100,risk_usd=1000,
        open_positions=2,open_direction_positions=2,open_opposite_positions=0,
        open_risk_usd=1200,open_direction_risk_usd=1200)
    assert gate.check_new_order(**request)[0]
    allowed,error=gate.check_new_order(**{**request,'open_risk_usd':1200.01,'open_direction_risk_usd':1200.01})
    assert not allowed and '总风险敞口' in error
