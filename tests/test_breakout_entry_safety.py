"""Breakout capacity/deadline/idempotence under placement lock; temporary ledger."""
import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo
import pytest
from execution.order_lot import OrderManager
from execution.risk import RiskGate, RiskConfig
from engine.opening_breakout import RULES_VERSION
from .conftest import run
from .test_pine_market_entry import Gateway
from .test_order_lot import mk_lot

T = int(datetime(2026,11,4,9,55,tzinfo=ZoneInfo('America/New_York')).timestamp())


def manager(tmp_path):
    gw = Gateway()
    gate = RiskGate(RiskConfig(max_open_positions=0, max_total_risk_usd=10000), halt_dir=tmp_path)
    return OrderManager(gw,gate,tmp_path/'ledger.json'),gw


def request(symbol='TEST',signal_time=T-300):
    return dict(symbol=symbol,side='long',qty=10,limit_px=100,stop_px=98,
                entry_order_type='MKT',submission_deadline=T+20,
                signal_snapshot=dict(opening_breakout_id=f'{symbol}:{signal_time}',
                                     breakout_signal_bar_time=signal_time,
                                     breakout_rules_version=RULES_VERSION))


def test_parallel_placements_reserve_five_slots_including_pending(tmp_path, monkeypatch):
    monkeypatch.setattr('execution.order_lot.time.time',lambda:T+1)
    mgr,gw = manager(tmp_path)
    # An accepted, unfilled order already occupies one slot.
    unknown = mk_lot(lot_id='unknown',sym='UNKNOWN')
    unknown.signal_snapshot={'opening_breakout_id':'unknown'}
    mgr._lots[unknown.lot_id]=unknown
    async def go():
        results=await asyncio.gather(*(mgr.place_limit_stop_lot(**request(f'S{i}')) for i in range(6)))
        assert sum(lot is not None for lot,err in results)==4, results
        assert len(gw.calls)==4
        assert sum('5笔' in err for lot,err in results if lot is None)==2
    run(go())


def test_same_symbol_day_stays_blocked_after_terminal_and_restart(tmp_path,monkeypatch):
    monkeypatch.setattr('execution.order_lot.time.time',lambda:T+1)
    mgr,gw=manager(tmp_path)
    lot=mk_lot(lot_id='closed',state='CLOSED',sym='TEST')
    lot.signal_snapshot=request()['signal_snapshot']
    mgr._lots[lot.lot_id]=lot
    mgr._save()
    recovered=OrderManager(gw,mgr._risk,mgr._state_path)
    result=run(recovered.place_limit_stop_lot(**request(signal_time=T)))
    assert result[0] is None and '本日' in result[1]
    assert gw.calls==[]


def test_cutoff_is_rechecked_after_awaited_risk_query(tmp_path,monkeypatch):
    clock=[T+1]
    monkeypatch.setattr('execution.order_lot.time.time',lambda:clock[0])
    mgr,gw=manager(tmp_path)
    async def pnl():
        clock[0]=T+35*60  # 10:30 exactly
        return 0
    gw.realized_pnl=pnl
    result=run(mgr.place_limit_stop_lot(**request()))
    assert result[0] is None and '10:30' in result[1]
    assert gw.calls==[]


def test_unknown_acceptance_blocks_new_entries_instead_of_retrying(tmp_path,monkeypatch):
    monkeypatch.setattr('execution.order_lot.time.time',lambda:T+1)
    mgr,gw=manager(tmp_path)
    unknown=mk_lot(lot_id='unknown',sym='UNKNOWN')
    unknown.signal_snapshot={'opening_breakout_id':'unknown'}
    unknown.entry_submission_pending=True
    mgr._lots[unknown.lot_id]=unknown
    result=run(mgr.place_limit_stop_lot(**request()))
    assert result[0] is None and '恢复' in result[1]
    assert gw.calls==[]
