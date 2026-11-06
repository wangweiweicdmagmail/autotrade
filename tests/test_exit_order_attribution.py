"""ADBE two-lot regression: unrelated cancel events cannot create phantom stops."""
import asyncio
from types import SimpleNamespace
import pytest
from execution.ibkr import IBKRGateway
from .conftest import run
from .test_order_lot import FakeGW,FakeTrade,mk_mgr,mk_lot,attach,alive_trail,cancel_watchers


def gateway(monkeypatch,*,own_error=False,status='Filled'):
    gw=IBKRGateway('test',0,1,'','',enabled=True);gw._connected=True
    gw._next_tool_order_id=lambda:123
    gw._apply_fa=lambda order:None
    async def qualify(symbol):return SimpleNamespace(symbol='ADBE')
    gw._qualify=qualify
    async def sleep(_):pass
    monkeypatch.setattr('execution.ibkr.asyncio.sleep',sleep)
    def place(contract,order):
        gw._on_error(123 if own_error else 999,202,'order cancelled')
        trade=FakeTrade(123,status=status,filled=311 if status=='Filled' else 0,
                        avg=248.8 if status=='Filled' else 0,qty=311,symbol='ADBE')
        return trade
    gw.ib=SimpleNamespace(placeOrder=place)
    return gw


def test_another_lots_cancel_does_not_reject_filled_close(monkeypatch):
    gw=gateway(monkeypatch)
    result=run(gw.place_close('NASDAQ:ADBE','long',311,249))
    assert result['ok'] and result['trade'].orderStatus.filled==311


def test_even_a_failed_close_preserves_the_trade_handle_and_fills(monkeypatch):
    gw=gateway(monkeypatch,own_error=True)
    result=run(gw.place_close('NASDAQ:ADBE','long',311,249))
    assert not result['ok']
    assert result['trade'].orderStatus.filled==311


def setup_pair(tmp_path):
    gw=FakeGW(fills={},positions={'ADBE':311})
    manager=mk_mgr(gw,tmp_path,'adbe')
    lots=[]
    for n in range(2):
        lot=mk_lot(lot_id=f'lot-{n}',state='ACTIVE',qty=311,sym='NASDAQ:ADBE')
        lot.filled_qty=311;lot.avg_fill=250;lot.last_px=249
        attach(manager,lot,trail=alive_trail(lot,qty=311,oid=100+n))
        lots.append(lot)
    return manager,gw,lots


def test_unknown_close_is_quarantined_not_reprotected_using_siblings_shares(tmp_path):
    async def go():
        manager,gw,(first,second)=setup_pair(tmp_path)
        async def close(*args,**kwargs):return {'ok':False,'order_id':215,'error':'cancel event','status':'Filled'}
        gw.place_close=close
        ok,_=await manager.close_lot(first.lot_id,ref_px=249)
        assert not ok and first.state=='CLOSING'
        assert not await manager.restore_protection(first.lot_id)
        assert gw.stop_only_calls==[]
        ok,_=await manager.close_lot(first.lot_id,ref_px=249)
        assert not ok  # No duplicate exit after uncertain acceptance.
        # Later exact per-order broker truth closes the first batch.
        first.close_trade=FakeTrade(215,status='Filled',filled=311,avg=248.8,qty=311)
        assert await manager._handle_close_terminal(first)=='closed'
        assert first.close_filled_qty==311
        assert second.filled_qty==311 and second.state=='ACTIVE'
        assert gw.stop_only_calls==[]
        cancel_watchers(manager)
    run(go())


def test_sibling_position_cannot_back_two_restored_stops(tmp_path):
    async def go():
        manager,gw,(first,second)=setup_pair(tmp_path)
        first.trail_trade=FakeTrade(100,status='Cancelled',qty=311)
        ok=await manager.restore_protection(first.lot_id)
        assert not ok and gw.stop_only_calls==[]
    run(go())


def test_failed_gateway_return_with_filled_trade_closes_instead_of_rearming(tmp_path):
    async def go():
        manager,gw,(first,second)=setup_pair(tmp_path)
        async def close(*args,**kwargs):
            return {'ok':False,'order_id':215,'error':'cancel event',
                    'trade':FakeTrade(215,status='Filled',filled=311,avg=248.8,qty=311)}
        gw.place_close=close
        ok,_=await manager.close_lot(first.lot_id,ref_px=249)
        assert ok and first.state=='CLOSED'
        assert first.close_filled_qty==311 and gw.stop_only_calls==[]
        cancel_watchers(manager)
    run(go())


def test_naked_recovery_queued_behind_exit_cannot_rearm(tmp_path):
    async def go():
        manager,gw,(first,second)=setup_pair(tmp_path)
        entered=asyncio.Event();release=asyncio.Event()
        original=gw.place_close
        async def close(*args,**kwargs):
            entered.set();await release.wait();return await original(*args,**kwargs)
        gw.place_close=close
        task=asyncio.create_task(manager.close_lot(first.lot_id,ref_px=249))
        await entered.wait()
        restore=asyncio.create_task(manager.restore_protection(first.lot_id))
        await asyncio.sleep(0)
        release.set();await task
        assert not await restore
        assert gw.stop_only_calls==[]
        cancel_watchers(manager)
    run(go())


def test_missing_close_cannot_borrow_equal_quantity_from_another_lot(tmp_path):
    async def go():
        manager,gw,(first,second)=setup_pair(tmp_path)
        first.state='CLOSING';first.close_order_id=215;first.close_requested_qty=311
        seen=[]
        async def truth(symbol,side,since,**kwargs):
            seen.append(kwargs)
            return None if kwargs.get('exact_order_ids')=={215} else (248.8,311)
        gw.closing_fill_avg=truth
        assert await manager._recover_missing_close(first)=='waiting'
        assert first.filled_qty==311 and first.close_filled_qty==0
        assert seen==[{'exact_order_ids':{215}}]
    run(go())


def test_exact_close_truth_is_incremental_and_preserves_previous_reduction(tmp_path):
    async def go():
        manager,gw,(first,second)=setup_pair(tmp_path)
        first.state='CLOSING';first.close_order_id=215;first.close_requested_qty=211
        first.filled_qty=211;first.close_filled_qty=100;first.close_avg_fill=249
        async def truth(*args,**kwargs):return (248.8,211)
        gw.closing_fill_avg=truth
        assert await manager._recover_missing_close(first)=='closed'
        assert first.close_filled_qty==311
        assert first.close_avg_fill==pytest.approx((249*100+248.8*211)/311,abs=0.0001)
    run(go())


def test_native_gateway_exact_order_filter_excludes_siblings(monkeypatch):
    gw=gateway(monkeypatch)
    def fill(oid,price):
        return SimpleNamespace(contract=SimpleNamespace(symbol='ADBE'),
          execution=SimpleNamespace(orderId=oid,side='SLD',shares=311,price=price))
    async def executions(_):return [fill(88002215,248.8),fill(88002216,248.9)]
    gw.ib=SimpleNamespace(reqExecutionsAsync=executions)
    assert run(gw.closing_fill_avg('NASDAQ:ADBE','long',exact_order_ids={88002215}))==(248.8,311)
    assert run(gw.closing_fill_avg('NASDAQ:ADBE','long',exact_order_ids={88002217})) is None


def test_error_with_live_trade_never_rearms_or_submits_a_second_close(tmp_path):
    async def go():
        manager,gw,(first,second)=setup_pair(tmp_path)
        calls=[]
        async def close(*args,**kwargs):
            calls.append(args)
            return {'ok':False,'order_id':215,'error':'late rejection',
                    'trade':FakeTrade(215,status='Submitted',qty=311)}
        gw.place_close=close
        ok,_=await manager.close_lot(first.lot_id,ref_px=249)
        assert not ok and first.state=='CLOSING'
        ok,_=await manager.close_lot(first.lot_id,ref_px=249)
        assert not ok and len(calls)==1 and gw.stop_only_calls==[]
        cancel_watchers(manager)
    run(go())


def test_failed_protective_result_keeps_identity_and_records_actual_fill(tmp_path):
    async def go():
        manager,gw,(first,second)=setup_pair(tmp_path)
        second.state='CLOSED';gw._pos={'ADBE':311}
        first.trail_trade=FakeTrade(100,status='Cancelled',qty=311)
        async def stop(*args):return {'ok':False,'order_id':217,'error':'cancel event',
            'trade':FakeTrade(217,status='Filled',filled=311,avg=247.8,qty=311)}
        gw.place_stop_only=stop
        assert not await manager.restore_protection(first.lot_id)
        assert first.trail_order_id==217 and first.close_filled_qty==311
        assert first.filled_qty==0
        assert await manager._handle_trail_terminal(first)=='closed'
        assert gw.close_calls==[]
        cancel_watchers(manager)
    run(go())


def test_phantom_failed_protection_cannot_be_force_closed_using_sibling_position(tmp_path):
    async def go():
        manager,gw,(first,second)=setup_pair(tmp_path)
        first.protection_restore_needed=True
        ok,error=await manager.close_lot(first.lot_id,ref_px=249)
        assert not ok and '余仓未核对' in error
        assert gw.close_calls==[]
    run(go())
