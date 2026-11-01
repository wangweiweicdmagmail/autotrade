from console import schemas
"""EMA9 金字塔：3-2-1 风险预算、2R 解锁、均价与整组退出。"""
from types import SimpleNamespace
import pytest
from engine.pyramid_engine import PyramidEngine
from .conftest import run

class FakeBroker:

    def __init__(self):
        self.lots = {}
        self.calls = []
        self.closes = []
        self.close_attempts = []
        self.fail_close_once = set()

    async def place_limit_stop(self, **kwargs):
        lot_id = f'lot-{len(self.lots) + 1}'
        lot = SimpleNamespace(lot_id=lot_id, state='ACTIVE', filled_qty=kwargs['qty'], avg_fill=kwargs['limit_px'], entry_px=kwargs['limit_px'], **kwargs)
        self.lots[lot_id] = lot
        self.calls.append(kwargs)
        return lot

    def get(self, lot_id):
        return self.lots.get(lot_id)

    async def mark_seed(self, lot_id, risk_dist, initial_risk_usd):
        if lot_id in self.lots:
            self.lots[lot_id].signal_snapshot = {'pyramid_seed': True, 'entry_risk_dist': risk_dist, 'initial_risk_usd': initial_risk_usd, 'entry_index': 1}

    async def move_stop(self, lot_id, new_stop):
        self.lots[lot_id].stop_px = new_stop
        return True

    async def close(self, lot_id, ref_px, reason, ts=0):
        self.close_attempts.append((lot_id, ref_px, reason))
        if lot_id in self.fail_close_once:
            self.fail_close_once.remove(lot_id)
            return False
        self.closes.append((lot_id, ref_px, reason))
        self.lots[lot_id].state = 'CLOSED'
        return True

    async def cancel(self, lot_id):
        self.lots[lot_id].state = 'CANCELLED'
        self.lots[lot_id].filled_qty = 0
        return True

def _e9(ema, stop, direction=-1, time=1000):
    return {'time': time, 'direction': direction, 'touch': 1, 'ema_9': ema, 'long_stop': ema - stop, 'short_stop': ema + stop}

def _start(broker, risk=300):
    engine = PyramidEngine(broker)
    engine.start(['NASDAQ:NVDA'], risk)
    return engine

def test_locked_touch_does_not_add():
    broker = FakeBroker()
    engine = _start(broker)
    run(engine.on_price('NASDAQ:NVDA', 207.37, _e9(207.37, 0.85), 600))
    run(engine.on_price('NASDAQ:NVDA', 207.45, _e9(207.45, 0.88, time=1300), 600))
    assert len(broker.calls) == 1
    inst = engine._instances['NASDAQ:NVDA']
    assert inst.entries == 1 and inst.unlocked is False

def test_2r_unlocks_and_scales_three_two_one_by_average_cost():
    broker = FakeBroker()
    engine = _start(broker)
    run(engine.on_price('NASDAQ:NVDA', 207.37, _e9(207.37, 0.85), 600))
    run(engine.on_price('NASDAQ:NVDA', 207.37, _e9(207.37, 0.85), 600))
    first = broker.calls[0]
    assert first['qty'] == 352 and first['stop_px'] == 208.22
    run(engine.on_price('NASDAQ:NVDA', 205.67, _e9(205.67, 0.8, time=1100) | {'touch': 0}, 600))
    inst = engine._instances['NASDAQ:NVDA']
    assert inst.unlocked is True and inst.stop_px == 208.22
    assert inst.breakeven_done is False
    run(engine.on_price('NASDAQ:NVDA', 206.01, _e9(206.01, 0.63, time=1200), 600))
    run(engine.on_price('NASDAQ:NVDA', 206.01, _e9(206.01, 0.63, time=1200), 600))
    second = broker.calls[1]
    assert second['qty'] == 90
    assert second['stop_px'] == 208.22
    assert inst.avg_cost == pytest.approx(207.0931, abs=0.001)
    assert inst.mfe_r == pytest.approx(1.274, abs=0.001)
    run(engine.on_price('NASDAQ:NVDA', 203.8, _e9(203.8, 0.55, time=1300), 600))
    run(engine.on_price('NASDAQ:NVDA', 203.8, _e9(203.8, 0.57, time=1400), 600))
    third = broker.calls[2]
    assert third['qty'] == 30
    assert third['stop_px'] == 207.09
    assert inst.entries == 3
    assert inst.avg_cost == pytest.approx(206.884, abs=0.001)
    run(engine.on_price('NASDAQ:NVDA', 203.0, _e9(203.0, 0.5, time=1500), 600))
    run(engine.on_price('NASDAQ:NVDA', 202.8, _e9(202.8, 0.5, time=1600), 600))
    assert len(broker.calls) == 3
    assert inst.breakeven_done is True and inst.stop_px == 206.88

def test_2r_unlock_does_not_move_stop_and_3r_moves_to_cost():
    broker = FakeBroker()
    engine = _start(broker)
    run(engine.on_price('NASDAQ:NVDA', 100, _e9(100, 1, direction=1), 600))
    run(engine.on_price('NASDAQ:NVDA', 100, _e9(100, 1, direction=1), 600))
    run(engine.on_price('NASDAQ:NVDA', 102, _e9(102, 1, direction=1, time=1100) | {'touch': 0}, 600))
    inst = engine._instances['NASDAQ:NVDA']
    assert inst.unlocked is True
    assert inst.breakeven_done is False and inst.stop_px == 99
    run(engine.on_price('NASDAQ:NVDA', 103, _e9(103, 1, direction=1, time=1200) | {'touch': 0}, 600))
    assert inst.breakeven_done is True and inst.stop_px == 100

def test_custom_unlock_and_disabled_breakeven_are_independent():
    broker = FakeBroker()
    engine = PyramidEngine(broker, breakeven_enabled=False, breakeven_r=3.0, add_unlock_r=1.5)
    engine.start(['NASDAQ:NVDA'], 300, {'NASDAQ:NVDA': 1})
    run(engine.on_price('NASDAQ:NVDA', 100, _e9(100, 1, direction=1), 600))
    run(engine.on_price('NASDAQ:NVDA', 101.5, _e9(101.5, 1, direction=1, time=1100) | {'touch': 0}, 600))
    inst = engine._instances['NASDAQ:NVDA']
    assert inst.unlocked is True and inst.breakeven_done is False
    run(engine.on_price('NASDAQ:NVDA', 105, _e9(105, 1, direction=1, time=1200) | {'touch': 0}, 600))
    assert inst.stop_px == 99 and inst.breakeven_done is False

def test_third_add_requires_second_position_1r_as_well_as_basket_2r():
    broker = FakeBroker()
    engine = PyramidEngine(broker, breakeven_enabled=False)
    engine.start(['NASDAQ:NVDA'], 300)
    run(engine.on_price('NASDAQ:NVDA', 100, _e9(100, 1, direction=1), 600))
    run(engine.on_price('NASDAQ:NVDA', 100, _e9(100, 1, direction=1), 600))
    run(engine.on_price('NASDAQ:NVDA', 102, _e9(102, 1, direction=1, time=1050) | {'touch': 0}, 600))
    run(engine.on_price('NASDAQ:NVDA', 105, _e9(105, 5, direction=1, time=1100), 600))
    run(engine.on_price('NASDAQ:NVDA', 105, _e9(105, 5, direction=1, time=1100), 600))
    run(engine.on_price('NASDAQ:NVDA', 107, _e9(107, 1, direction=1, time=1200), 600))
    inst = engine._instances['NASDAQ:NVDA']
    assert inst.mfe_r >= 2
    assert inst.second_mfe_r == pytest.approx(0.333, abs=0.001)
    assert inst.unlocked is True and inst.breakeven_done is False
    assert len(broker.calls) == 2
    run(engine.on_price('NASDAQ:NVDA', 111, _e9(111, 1, direction=1, time=1300), 600))
    assert inst.second_mfe_r >= 1.0
    assert inst.breakeven_done is False
    assert len(broker.calls) == 3

def _manual_seed_lot(direction='long'):
    return {'lot_id': 'manual-1', 'symbol': 'NASDAQ:NVDA', 'state': 'ACTIVE', 'limit_type': 'market', 'side': direction, 'filled_qty': 100, 'avg_fill': 100.0, 'entry_px': 100.0, 'stop_px': 99.0, 'init_entry_px': 100.0, 'init_stop_px': 99.0, 'init_risk_usd': 100.0, 'created_at': 1}

def test_manual_market_fill_is_adopted_while_strategy_waits():
    engine = _start(FakeBroker())
    adopted = run(engine.adopt_manual_lots([_manual_seed_lot()], signals={'NASDAQ:NVDA': {'e9': {'direction': 1}}}))
    inst = engine._instances['NASDAQ:NVDA']
    assert adopted == ['NASDAQ:NVDA']
    assert inst.entries == 1 and inst.total_qty == 100
    assert inst.avg_cost == 100 and inst.initial_r == 1
    assert inst.source == 'manual_seed'

def test_manual_seed_recovers_after_restart():
    engine = _start(FakeBroker())
    lot = _manual_seed_lot()
    run(engine.adopt_seed('NASDAQ:NVDA', lot, 300, {'direction': 1}))
    recovered = dict(lot, signal_snapshot={'pyramid_seed': True, 'entry_risk_dist': 1.0, 'initial_risk_usd': 300.0, 'entry_index': 1})
    broker = FakeBroker()
    broker.lots['manual-1'] = SimpleNamespace(lot_id='manual-1', state='ACTIVE', qty=100, filled_qty=100, avg_fill=100.0, entry_px=100.0, stop_px=99.0)
    new_engine = PyramidEngine(broker)
    run(new_engine.adopt_manual_lots([recovered]))
    inst = new_engine._instances['NASDAQ:NVDA']
    assert inst.entries == 1 and inst.source == 'manual_seed'
    assert inst.entry_risks['manual-1'] == 1.0
    assert inst.initial_risk_usd == 300.0

def test_manual_seed_rejects_direction_mismatch():
    engine = _start(FakeBroker())
    with pytest.raises(ValueError, match='方向不一致'):
        run(engine.adopt_seed('NASDAQ:NVDA', _manual_seed_lot(), 300, {'direction': -1}))

def test_reverse_closes_entire_basket_and_requires_explicit_new_round():
    broker = FakeBroker()
    engine = _start(broker)
    run(engine.on_price('NASDAQ:NVDA', 207.37, _e9(207.37, 0.85), 600))
    run(engine.on_price('NASDAQ:NVDA', 207.37, _e9(207.37, 0.85), 600))
    run(engine.on_bar('NASDAQ:NVDA', {'close': 207.5}, {'direction': 1}))
    assert len(broker.closes) == 1
    run(engine.on_bar('NASDAQ:NVDA', {'close': 207.5}, {'direction': 1}))
    inst = engine._instances['NASDAQ:NVDA']
    assert inst.total_qty == 0 and inst.entries == 1 and (inst.state == 'DONE')

def _fill_two_long_entries(engine):
    run(engine.on_price('NASDAQ:NVDA', 100, _e9(100, 1, direction=1), 600))
    run(engine.on_price('NASDAQ:NVDA', 100, _e9(100, 1, direction=1), 600))
    run(engine.on_price('NASDAQ:NVDA', 102, _e9(102, 1, direction=1, time=1050) | {'touch': 0}, 600))
    run(engine.on_price('NASDAQ:NVDA', 101, _e9(101, 5, direction=1, time=1100), 600))
    run(engine.on_price('NASDAQ:NVDA', 101, _e9(101, 5, direction=1, time=1100), 600))

def test_second_position_historical_mfe_is_restored():
    broker = FakeBroker()
    engine = _start(broker)
    _fill_two_long_entries(engine)
    inst = engine._instances['NASDAQ:NVDA']
    inst.second_mfe_r = 0.0
    broker.lots['lot-2'].mfe = 5 * 40 * 1.2
    run(engine.on_price('NASDAQ:NVDA', 106, _e9(106, 1, direction=1, time=1200), 600))
    assert inst.second_mfe_r >= 1.0
    assert len(broker.calls) == 3

def test_third_add_is_blocked_when_second_position_is_no_longer_active():
    broker = FakeBroker()
    engine = _start(broker)
    _fill_two_long_entries(engine)
    inst = engine._instances['NASDAQ:NVDA']
    inst.second_mfe_r = 2.0
    broker.lots['lot-2'].state = 'CLOSED'
    run(engine.on_price('NASDAQ:NVDA', 106, _e9(106, 1, direction=1, time=1200), 600))
    assert len(broker.calls) == 2

def test_close_failure_retries_and_never_uses_zero_reference_price():
    broker = FakeBroker()
    engine = _start(broker)
    run(engine.on_price('NASDAQ:NVDA', 100, _e9(100, 1, direction=1), 600))
    run(engine.on_price('NASDAQ:NVDA', 100, _e9(100, 1, direction=1), 600))
    inst = engine._instances['NASDAQ:NVDA']
    broker.fail_close_once.add('lot-1')
    run(engine._stop_close(inst, 0, 'test'))
    assert inst.state == 'CLOSING'
    assert broker.close_attempts[-1][1] == 100
    inst.close_attempt_at = 0.0
    run(engine._stop_close(inst, 0, 'test'))
    assert len(broker.close_attempts) == 2
    assert len(broker.closes) == 1

def test_manual_seed_rejects_non_positive_risk_budget():
    engine = _start(FakeBroker())
    with pytest.raises(ValueError, match='风险预算'):
        run(engine.adopt_seed('NASDAQ:NVDA', _manual_seed_lot(), 0, {'direction': 1}))

def test_start_direction_is_locked_and_finished_round_cannot_reopen():
    broker = FakeBroker()
    engine = PyramidEngine(broker)
    engine.start(['NASDAQ:NVDA'], 300, {'NASDAQ:NVDA': 1})
    run(engine.on_price('NASDAQ:NVDA', 100, _e9(100, 1, direction=-1), 600))
    assert broker.calls == []
    run(engine.on_price('NASDAQ:NVDA', 100, _e9(100, 1, direction=1, time=1100), 600))
    run(engine.on_price('NASDAQ:NVDA', 100, _e9(100, 1, direction=1, time=1100), 600))
    assert len(broker.calls) == 1
    run(engine.on_bar('NASDAQ:NVDA', {'close': 101}, {'direction': -1}))
    run(engine.on_bar('NASDAQ:NVDA', {'close': 101}, {'direction': -1}))
    inst = engine._instances['NASDAQ:NVDA']
    assert inst.state == 'DONE'
    run(engine.on_price('NASDAQ:NVDA', 102, _e9(102, 1, direction=1, time=1200), 600))
    run(engine.on_price('NASDAQ:NVDA', 102, _e9(102, 1, direction=-1, time=1300), 600))
    assert len(broker.calls) == 1

def test_stop_without_position_finishes_immediately():
    broker = FakeBroker()
    engine = PyramidEngine(broker)
    engine.start(['NASDAQ:NVDA'], 300, {'NASDAQ:NVDA': 1})
    engine.stop_symbol('NASDAQ:NVDA')
    assert engine._instances['NASDAQ:NVDA'].state == 'DONE'

def test_unfilled_cancellation_does_not_finish_locked_instance():
    broker = FakeBroker()
    engine = PyramidEngine(broker)
    engine.start(['NASDAQ:NVDA'], 300, {'NASDAQ:NVDA': 1})
    run(engine.on_price('NASDAQ:NVDA', 100, _e9(100, 1, direction=1), 600))
    inst = engine._instances['NASDAQ:NVDA']
    lot = broker.get(inst.pending_lot_id)
    lot.state, lot.filled_qty, lot.avg_fill = ('PENDING', 0, 0)
    run(broker.cancel(inst.pending_lot_id))
    run(engine.on_price('NASDAQ:NVDA', 100, _e9(100, 1, direction=1, time=1200), 600))
    assert inst.state != 'DONE'
    assert len(broker.calls) == 2

def test_fill_and_flat_events_describe_actual_execution():
    broker = FakeBroker()
    events = []
    engine = PyramidEngine(broker, emit=lambda event, **data: events.append((event, data)))
    engine.start(['NASDAQ:NVDA'], 300, {'NASDAQ:NVDA': 1})
    run(engine.on_price('NASDAQ:NVDA', 100, _e9(100, 1, direction=1), 600))
    run(engine.on_price('NASDAQ:NVDA', 100, _e9(100, 1, direction=1), 600))
    filled = next((data for event, data in events if event == 'pyramid_entry_filled'))
    assert filled['entries'] == 1
    assert filled['qty'] == 300
    assert filled['entry_px'] == 100
    run(engine.on_bar('NASDAQ:NVDA', {'close': 99}, {'direction': -1}))
    run(engine.on_bar('NASDAQ:NVDA', {'close': 99}, {'direction': -1}))
    flat = next((data for event, data in events if event == 'pyramid_flat'))
    assert flat['had_fill'] is True
    assert flat['side'] == 'long'

def test_auto_notices_cover_key_events_and_skip_unfilled_flat(console_app, monkeypatch):
    app = console_app
    monkeypatch.setattr(app.automatic, '_auto_notices', {})
    monkeypatch.setattr(app.events, 'record_event', lambda *args, **kwargs: None)
    app.automatic._emit_auto('pyramid_order_placed', symbol='NASDAQ:NVDA', entries=1)
    app.automatic._emit_auto('pyramid_entry_filled', symbol='NASDAQ:NVDA', entries=1)
    app.automatic._emit_auto('pyramid_close_requested', symbol='NASDAQ:NVDA', ok=True)
    app.automatic._emit_auto('pyramid_flat', symbol='NASDAQ:NVDA', had_fill=False)
    app.automatic._emit_auto('pyramid_flat', symbol='NASDAQ:NVDA', had_fill=True)
    notices = list(app.automatic._auto_notices.values())
    assert [n['event'] for n in notices] == ['pyramid_order_placed', 'pyramid_entry_filled', 'pyramid_close_requested', 'pyramid_flat']
    assert len({n['id'] for n in notices}) == len(notices)

    async def fake_status():
        return {'equity': None, 'daily_pnl': None}
    monkeypatch.setattr(app.accounts, 'get_status_cached', fake_status)
    snapshot = run(app.snapshot.build_snapshot())
    assert [n['event'] for n in snapshot['auto_notices']] == ['pyramid_flat', 'pyramid_close_requested', 'pyramid_entry_filled', 'pyramid_order_placed']

def test_pyramid_settings_update_engine_and_validate(console_app, monkeypatch):
    app = console_app
    monkeypatch.setattr(app.settings, '_settings', {})
    monkeypatch.setattr(app.settings, '_save_settings', lambda: None)
    monkeypatch.setattr(app.events, 'record_event', lambda *args, **kwargs: None)
    for name in ('breakeven_enabled', 'breakeven_r', 'add_unlock_r'):
        monkeypatch.setattr(app.auto_engine, name, getattr(app.auto_engine, name))
    result = run(app.settings.api_update_settings(schemas.SettingsRequest(pyramid_auto_breakeven=False, pyramid_breakeven_r=4.0, pyramid_add_unlock_r=1.5), trader='master'))
    assert result['pyramid_auto_breakeven'] is False
    assert result['pyramid_breakeven_r'] == 4.0
    assert result['pyramid_add_unlock_r'] == 1.5
    assert app.auto_engine.breakeven_enabled is False
    assert app.auto_engine.breakeven_r == 4.0
    assert app.auto_engine.add_unlock_r == 1.5
    with pytest.raises(app.HTTPException) as exc:
        run(app.settings.api_update_settings(schemas.SettingsRequest(pyramid_add_unlock_r=0), trader='master'))
    assert exc.value.status_code == 400
    assert app.auto_engine.add_unlock_r == 1.5
    assert app.monitoring._is_pyramid_lot({'limit_type': 'auto_ema9_pyramid'})
    assert app.monitoring._is_pyramid_lot({'signal_snapshot': {'pyramid_seed': True}})
    assert not app.monitoring._is_pyramid_lot({'limit_type': 'market', 'signal_snapshot': {}})

def test_backtest_receives_independent_pyramid_parameters():
    from engine.backtest_session import BacktestSession
    session = BacktestSession([], 'NASDAQ:NVDA', '2026-10-16', 'ema9_pyramid', {'pyramid_auto_breakeven': False, 'pyramid_breakeven_r': 4.0, 'pyramid_add_unlock_r': 1.5})
    assert session.engine.breakeven_enabled is False
    assert session.engine.breakeven_r == 4.0
    assert session.engine.add_unlock_r == 1.5
    assert session.params['pyramid_breakeven_r'] == 4.0
    assert session.params['pyramid_add_unlock_r'] == 1.5

def test_api_start_uses_current_risk_and_requires_e9(console_app, monkeypatch):
    app = console_app
    monkeypatch.setattr(app.collector, 'latest', lambda: {'NASDAQ:NVDA': {'e9': {'ema_9': 207.37, 'direction': 1}}})
    calls = []
    monkeypatch.setattr(app.auto_engine, 'start', lambda symbols, risk, directions: calls.append((symbols, risk, directions)) or symbols)
    req = schemas.AutoStartRequest(symbols=['NVDA'], params={'initial_risk_usd': 450})
    result = run(app.automatic.api_auto_start(req, trader='master'))
    assert calls == [(['NASDAQ:NVDA'], 450.0, {'NASDAQ:NVDA': 1})]
    assert result['params']['risk_ratios'] == [1.0, 2.0 / 3.0, 1.0 / 3.0]

def test_api_start_rejects_missing_e9(console_app, monkeypatch):
    app = console_app
    monkeypatch.setattr(app.collector, 'latest', lambda: {})
    req = schemas.AutoStartRequest(symbols=['NVDA'], params={'initial_risk_usd': 300})
    with pytest.raises(app.HTTPException) as exc:
        run(app.automatic.api_auto_start(req, trader='master'))
    assert exc.value.status_code == 409
