from console import schemas
"""金字塔风险、成交后采样、反向撤单和重启恢复；全部内存或 tmp_path。"""
import copy
import time
from dataclasses import asdict
from types import SimpleNamespace
import pytest
from engine.auto_engine import LiveBroker
from engine.pyramid_engine import PyramidEngine
from engine.realtime_collector import RealtimeCollector
from execution.order_lot import OrderLot, OrderManager
from execution.risk import RiskConfig, RiskGate
from .conftest import run
from .test_pyramid_engine import FakeBroker, _e9
from .test_tv_data_health import _FakeGateway
SYM = 'NASDAQ:NVDA'

class StateBroker(FakeBroker):

    def __init__(self):
        super().__init__()
        self.states = {}

    def pyramid_states(self):
        return copy.deepcopy(self.states)

    def save_pyramid_state(self, group_id, state):
        self.states[group_id] = copy.deepcopy(state)

class PendingBroker(StateBroker):

    async def place_limit_stop(self, **kwargs):
        lot = await super().place_limit_stop(**kwargs)
        lot.state, lot.filled_qty, lot.avg_fill = ('PENDING', 0, 0)
        return lot

    def fill(self, lot_id, qty=None):
        lot = self.lots[lot_id]
        lot.state = 'ACTIVE'
        lot.filled_qty = qty or lot.qty
        lot.avg_fill = lot.entry_px

def engine_for(broker, direction=1, **params):
    engine = PyramidEngine(broker, **params)
    engine.start([SYM], 300, {SYM: direction})
    return engine

@pytest.mark.parametrize('direction', [1, -1])
def test_add_sized_from_actual_stop_and_budget(direction):
    broker = StateBroker()
    engine = engine_for(broker, direction)

    def tick(px, t, touch=1):
        return engine.on_price(SYM, px, _e9(px, 1, direction, t) | {'touch': touch}, 600)
    run(tick(100, 1000))
    run(tick(100 + 2 * direction, 1300, 0))
    run(tick(100 + 2 * direction, 1600))
    order = broker.calls[1]
    assert order['qty'] == 66
    assert order['stop_px'] == 100 - direction
    assert order['meta']['risk_budget'] == 200
    assert order['meta']['planned_risk_usd'] == 198
    assert engine.instances()[0]['last_order_risk'] == 198

def test_live_broker_passes_budget_to_hard_risk_gate(tmp_path):
    gateway = _FakeGateway()
    gate = RiskGate(RiskConfig(max_order_qty=1000, max_open_positions=0), halt_dir=tmp_path)
    manager = OrderManager(gateway, gate, tmp_path / 'lots.json')
    broker = LiveBroker(manager)
    with pytest.raises(RuntimeError, match='实际风险'):
        run(broker.place_limit_stop(SYM, 'long', 200, 102, 99, meta={'risk_budget': 200}))
    assert gateway.place_calls == 0

def test_prices_rounded_before_sizing():
    broker = StateBroker()
    engine = engine_for(broker)
    run(engine.on_price(SYM, 100, {'time': 1000, 'touch': 1, 'direction': 1, 'ema_9': 100.004, 'long_stop': 99.006}, 600))
    order = broker.calls[0]
    assert order['qty'] * abs(order['limit_px'] - order['stop_px']) <= 300

def test_historical_bar_extremes_cannot_drive_live_management():
    engine = engine_for(StateBroker())
    with pytest.raises(ValueError, match='历史极值'):
        run(engine.on_price_extremes(SYM, 100, 103, _e9(100, 1, 1), 600))

def test_pre_fill_snapshot_does_not_unlock_or_breakeven():
    broker = PendingBroker()
    engine = engine_for(broker)
    run(engine.on_price(SYM, 100, _e9(100, 1, 1), 600))
    broker.fill('lot-1')
    broker.lots['lot-1'].entry_time = time.time()
    stale_time = time.time() - 1
    run(engine.on_price(SYM, 103, _e9(100, 1, 1) | {'touch': 0}, 600, observed_at=stale_time))
    inst = engine._instances[SYM]
    assert inst.mfe_r == 0 and (not inst.unlocked) and (not inst.breakeven_done)
    run(engine.on_price(SYM, 103, _e9(100, 1, 1) | {'touch': 0}, 600, observed_at=time.time() + 1))
    assert inst.mfe_r == 3 and inst.breakeven_done

def test_pending_add_keeps_mfe_until_actual_fill_and_cancel_keeps_it():
    broker = StateBroker()
    engine = engine_for(broker)
    run(engine.on_price(SYM, 100, _e9(100, 1, 1), 600))
    run(engine.on_price(SYM, 102, _e9(102, 1, 1, 1300) | {'touch': 0}, 600))
    original = broker.place_limit_stop

    async def pending(**kwargs):
        lot = await original(**kwargs)
        lot.state, lot.avg_fill, lot.filled_qty = ('PENDING', 0, 0)
        return lot
    broker.place_limit_stop = pending
    run(engine.on_price(SYM, 102, _e9(102, 1, 1, 1600), 600))
    inst = engine._instances[SYM]
    assert inst.mfe_r == 2 and inst.unlocked
    run(broker.cancel('lot-2'))
    run(engine.on_price(SYM, 101, _e9(101, 1, 1, 1900) | {'touch': 0}, 600))
    assert inst.mfe_r == 2 and inst.entries == 1

def test_partial_fill_rebases_mfe_only_after_fill():
    broker = PendingBroker()
    engine = engine_for(broker)
    run(engine.on_price(SYM, 100, _e9(100, 1, 1), 600))
    broker.fill('lot-1', 10)
    run(engine.on_price(SYM, 102, _e9(102, 1, 1) | {'touch': 0}, 600))
    assert engine._instances[SYM].mfe_r == 2
    broker.lots['lot-1'].filled_qty = 20
    run(engine.on_price(SYM, 100, _e9(100, 1, 1) | {'touch': 0}, 600))
    assert engine._instances[SYM].mfe_r == 0

@pytest.mark.parametrize('direction', [1, -1])
def test_manual_exit_cancels_unfilled_seed(direction, monkeypatch):
    broker = PendingBroker()
    engine = engine_for(broker, direction)
    run(engine.on_price(SYM, 100, _e9(100, 1, direction), 600))
    run(engine.close_symbol(SYM,100))
    assert broker.lots['lot-1'].state == 'CANCELLED'
    run(engine.on_price(SYM, 100, _e9(100, 1, direction, 1300), 600))
    assert engine._instances[SYM].state == 'DONE'
    assert len(broker.calls) == 1

def test_manual_cancel_fill_race_is_closed(monkeypatch):
    broker = PendingBroker()
    engine = engine_for(broker)
    run(engine.on_price(SYM, 100, _e9(100, 1, 1), 600))

    async def racing_cancel(lot_id):
        broker.fill(lot_id, 10)
        return False
    broker.cancel = racing_cancel
    run(engine.close_symbol(SYM,99))
    assert broker.lots['lot-1'].state == 'CLOSED'
    assert len(broker.closes) == 1

def test_manual_exit_retries_without_waiting_for_next_closed_bar(monkeypatch):
    import engine.pyramid_engine as module
    clock = [1000.0]
    monkeypatch.setattr(module.time, 'time', lambda: clock[0])
    broker = StateBroker()
    engine = engine_for(broker)
    run(engine.on_price(SYM, 100, _e9(100, 1, 1), 600))
    broker.fail_close_once.add('lot-1')
    run(engine.close_symbol(SYM,99))
    run(engine.on_price(SYM, 99, _e9(99, 1, 1, 1300), 600))
    assert len(broker.close_attempts) == 1
    clock[0] += 6
    run(engine.on_price(SYM, 99, _e9(99, 1, 1, 1300), 600))
    assert len(broker.close_attempts) == 2
    assert len(broker.calls) == 1

def row_for(lot):
    return dict(lot_id=lot.lot_id, symbol=SYM, state=lot.state, side=lot.side, limit_type='auto_ema9_pyramid', created_at=1, signal_snapshot=lot.meta, qty=lot.qty, filled_qty=lot.filled_qty, avg_fill=lot.avg_fill, entry_px=lot.entry_px, stop_px=lot.stop_px, init_entry_px=lot.entry_px, init_stop_px=lot.stop_px)

def test_restart_after_seed_closed_keeps_count_risk_budget_and_mfe():
    broker = StateBroker()
    engine = engine_for(broker)
    run(engine.on_price(SYM, 100, _e9(100, 1, 1), 600))
    run(engine.on_price(SYM, 102, _e9(102, 1, 1, 1300), 600))
    run(engine.on_price(SYM, 102, _e9(102, 1, 1, 1300), 600))
    run(broker.close('lot-1', 102, 'manual'))
    run(engine.on_price(SYM, 102.5, _e9(102, 1, 1, 1600) | {'touch': 0}, 600))
    before = asdict(engine._instances[SYM])
    restored = PyramidEngine(broker)
    run(restored.adopt_manual_lots([row_for(broker.lots['lot-2'])]))
    inst = restored._instances[SYM]
    assert inst.entries == 2 and inst.initial_r == 1 and (inst.initial_risk_usd == 300)
    assert inst.mfe_r == before['mfe_r'] and inst.second_mfe_r == before['second_mfe_r']
    assert restored._second_lot(inst).lot_id == 'lot-2'
    assert inst.restored and (not inst.enabled) and (not inst.recovery_block_reason)
    restored.start([SYM], 999, {SYM: 1})
    assert inst.initial_risk_usd == 300

def test_legacy_recovery_blocks_add_without_guessing_first_r():
    broker = StateBroker()
    broker.lots['old'] = SimpleNamespace(lot_id='old', state='ACTIVE', side='long', filled_qty=10, qty=10, avg_fill=102, entry_px=102, stop_px=99, meta={'entry_index': 2, 'entry_risk_dist': 1})
    engine = PyramidEngine(broker)
    run(engine.adopt_manual_lots([row_for(broker.lots['old'])]))
    inst = engine._instances[SYM]
    assert inst.entries == 2 and inst.initial_r == 0 and inst.recovery_block_reason
    assert engine.start([SYM], 300) == []
    run(engine.close_symbol(SYM,100))
    assert broker.lots['old'].state == 'CLOSED'

def test_restart_keeps_third_entry_limit_with_only_second_remaining():
    broker = StateBroker()
    engine = engine_for(broker, breakeven_enabled=False)
    run(engine.on_price(SYM, 100, _e9(100, 1, 1), 600))
    run(engine.on_price(SYM, 102, _e9(102, 1, 1, 1300), 600))
    run(engine.on_price(SYM, 105, _e9(105, 1, 1, 1600), 600))
    assert len(broker.calls) == 3
    run(broker.close('lot-1', 105, 'manual'))
    run(broker.close('lot-3', 105, 'manual'))
    run(engine.on_price(SYM, 105, _e9(105, 1, 1, 1900) | {'touch': 0}, 600))
    restored = PyramidEngine(broker, breakeven_enabled=False)
    run(restored.adopt_manual_lots([row_for(broker.lots['lot-2'])]))
    restored.start([SYM], 999)
    run(restored.on_price(SYM, 120, _e9(120, 1, 1, 2200), 600))
    assert restored._instances[SYM].entries == 3
    assert len(broker.calls) == 3

def test_restart_retains_failed_exit_and_retries_instead_of_adding():
    broker = StateBroker()
    engine = engine_for(broker)
    run(engine.on_price(SYM, 100, _e9(100, 1, 1), 600))
    broker.fail_close_once.add('lot-1')
    run(engine.close_symbol(SYM,99))
    restored = PyramidEngine(broker)
    run(restored.adopt_manual_lots([row_for(broker.lots['lot-1'])]))
    assert restored._instances[SYM].state == 'CLOSING'
    assert restored.start([SYM], 300) == []
    restored._instances[SYM].close_attempt_at = 0
    run(restored.on_price(SYM, 99, _e9(99, 1, 1, 1300), 600))
    assert broker.lots['lot-1'].state == 'CLOSED'
    assert len(broker.calls) == 1

def test_api_reports_why_recovered_group_cannot_resume(console_app, monkeypatch):
    app = console_app
    engine = engine_for(StateBroker())
    engine._instances[SYM].recovery_block_reason = '首仓风险基准缺失'
    monkeypatch.setattr(app, 'auto_engine', engine)
    monkeypatch.setattr(app.automatic, '_auto_enabled', False)
    monkeypatch.setattr(app.automatic, '_auto_strategy_name', '')
    monkeypatch.setattr(app.collector, 'latest', lambda: {
        SYM: {'e9': _e9(100, 1, 1)}, 'NASDAQ:QQQ': {'e9': {'direction': 1}},
    })
    req = schemas.AutoStartRequest(symbols=[SYM], confirmed_directions={SYM: 1, 'NASDAQ:QQQ': 1})
    result = run(app.automatic.api_auto_start(req, trader='master'))
    assert result['started'] == []
    assert result['skipped_reasons'] == {SYM: '首仓风险基准缺失'}

def test_group_state_survives_order_manager_disk_roundtrip(tmp_path):
    path = tmp_path / 'lots.json'
    gate = RiskGate(RiskConfig(), halt_dir=tmp_path)
    manager = OrderManager(_FakeGateway(), gate, path)
    manager._lots['seed'] = OrderLot('seed', SYM, 'long', 10, 100, 99, 0)
    state = {'symbol': SYM, 'initial_r': 1, 'entries': 3, 'initial_risk_usd': 300}
    LiveBroker(manager).save_pyramid_state('group', state)
    loaded = OrderManager(_FakeGateway(), gate, path)
    assert LiveBroker(loaded).pyramid_states() == {'group': state}
    assert loaded.get('seed') is not None

def test_quote_receipt_time_tracks_each_symbol_without_mutating_input():
    collector = RealtimeCollector([SYM, 'NASDAQ:QQQ'])
    payload = {'symbols': {SYM: {'bar': {'close': 100}}, 'NASDAQ:QQQ': {'bar': {'close': 500}}}}
    for row in payload['symbols'].values():row['session']={'requested':'regular','resolved':'regular','accepted':True}
    collector._accept_tick(payload, now=1000)
    assert 'price_observed_at' not in payload['symbols'][SYM]
    payload['symbols']['NASDAQ:QQQ']['bar'] = {'close': 501}
    collector._accept_tick(payload, now=1001)
    assert collector.latest()[SYM]['price_observed_at'] == 1000
    assert collector.latest()['NASDAQ:QQQ']['price_observed_at'] == 1001
