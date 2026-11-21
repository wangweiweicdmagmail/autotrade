import asyncio
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import HTTPException

from console.schemas import OpeningVwapPrepareRequest
from console.services.opening_vwap import ET, STRATEGY_ID, OpeningVwapService
from execution.order_lot import OrderLot, OrderManager

OPENING = int(datetime(2026, 11, 16, 9, 30, tzinfo=ET).timestamp())
CLOSE = OPENING + 180
SYMBOL = "NASDAQ:QQQ"
KEY = f"{SYMBOL}:{CLOSE}"


def market(at, *, confirmed=False, side="long", present=True):
    signal = {"bar_time": CLOSE - 60, "close_at": CLOSE, "side": side,
              "confirmed": confirmed, "entry_px": 103, "stop_px": 101 if confirmed else 100,
              "body_pct": 0.8}
    return {"opening": OPENING, "bar_time": CLOSE if confirmed else CLOSE - 60,
            "price": 102.8, "observed_at": at, "signals": [signal] if present else []}


def service():
    context = SimpleNamespace(
        gateway=SimpleNamespace(enabled=True, _connected=True),
        collector=SimpleNamespace(vwap_opening={}, data_health=lambda: {"stale": False}),
        accounts=SimpleNamespace(_equity_cache={"equity": 100_000}),
        events=SimpleNamespace(record_event=Mock()),
        order_mgr=SimpleNamespace(place_limit_stop_lot=AsyncMock(return_value=(SimpleNamespace(lot_id="x"), "")),
                                  list_lots=Mock(return_value=[]), breakeven=AsyncMock(return_value=(True, "")),
                                  cancel_lot=AsyncMock(return_value=(True, "")),
                                  close_lot=AsyncMock(return_value=(True, ""))),
    )
    return OpeningVwapService(context)


def test_only_armed_signal_submits_once_with_final_midpoint(monkeypatch):
    svc = service()
    now = [CLOSE - 10]
    monkeypatch.setattr("console.services.opening_vwap.time.time", lambda: now[0])
    svc._market()[SYMBOL] = market(now[0])
    svc.prepare(OpeningVwapPrepareRequest(signal_id=KEY, side="long"), "master")
    assert not svc.context.order_mgr.place_limit_stop_lot.await_count
    async def scenario():
        now[0] = CLOSE + 1
        svc._market()[SYMBOL] = market(now[0], confirmed=True)
        svc._market()["NASDAQ:NVDA"] = market(now[0], confirmed=True)
        await svc.advance(now[0])
        await svc.advance(now[0])
    asyncio.run(scenario())
    kwargs = svc.context.order_mgr.place_limit_stop_lot.await_args.kwargs
    assert svc.context.order_mgr.place_limit_stop_lot.await_count == 1
    assert (kwargs["symbol"], kwargs["limit_px"], kwargs["stop_px"], kwargs["entry_order_type"]) == (
        SYMBOL, 103, 101, "MKT",
    )
    assert kwargs["risk_ref_px"] == 102.8
    assert kwargs["risk_usd"] == 1000.0 and kwargs["qty"] == 555
    assert next(row for row in svc.display(now[0]) if row["id"] == KEY)["status"] == "submitted"


@pytest.mark.parametrize(("present", "side", "delay", "status"), [
    (False, "long", 1, "invalid"), (True, "short", 1, "invalid"),
    (True, "long", 20, "expired"),
])
def test_confirmation_cannot_change_direction_or_submit_late(monkeypatch, present, side, delay, status):
    svc = service()
    monkeypatch.setattr("console.services.opening_vwap.time.time", lambda: CLOSE - 10)
    svc._market()[SYMBOL] = market(CLOSE - 10)
    svc.prepare(OpeningVwapPrepareRequest(signal_id=KEY, side="long"), "master")
    svc._market()[SYMBOL] = market(CLOSE + delay, confirmed=True, present=present, side=side)
    asyncio.run(svc.advance(CLOSE + delay))
    assert svc._orders[KEY]["status"] == status
    svc.context.order_mgr.place_limit_stop_lot.assert_not_awaited()


def test_cancel_requires_owner_and_removes_order_before_confirmation(monkeypatch):
    svc = service()
    monkeypatch.setattr("console.services.opening_vwap.time.time", lambda: CLOSE - 10)
    svc._market()[SYMBOL] = market(CLOSE - 10)
    request = OpeningVwapPrepareRequest(signal_id=KEY, side="long")
    svc.prepare(request, "master")
    cancel = OpeningVwapPrepareRequest(signal_id=KEY, side="long", cancel=True)
    with pytest.raises(HTTPException):
        svc.prepare(cancel, "other")
    svc.prepare(cancel, "master")
    svc._market()[SYMBOL] = market(CLOSE + 1, confirmed=True)
    asyncio.run(svc.advance(CLOSE + 1))
    svc.context.order_mgr.place_limit_stop_lot.assert_not_awaited()


@pytest.mark.parametrize("crossed", [False, True])
def test_three_minute_breakeven_or_exit_and_fixed_time_exit(crossed):
    svc = service()
    filled_at = CLOSE + 2
    meta = {"strategy_id": STRATEGY_ID, "opening_vwap_opening": OPENING}
    svc.context.order_mgr.list_lots.return_value = [{
        "lot_id": "target", "symbol": SYMBOL, "signal_snapshot": meta, "state": "ACTIVE",
        "side": "long", "entry_broker_time": filled_at, "avg_fill": 102,
    }, {"lot_id": "other", "symbol": "NASDAQ:NVDA", "state": "ACTIVE"}]
    now = filled_at + 180
    svc._market()[SYMBOL] = {"bar_time": now // 60 * 60, "observed_at": now,
                              "price": 101 if crossed else 103}
    asyncio.run(svc.manage(now - 1))
    svc.context.order_mgr.breakeven.assert_not_awaited()
    svc.context.order_mgr.close_lot.assert_not_awaited()
    asyncio.run(svc.manage(now))
    if crossed:
        assert svc.context.order_mgr.close_lot.await_args.kwargs["exit_reason"] == "opening_vwap_breakeven_crossed"
    else:
        svc.context.order_mgr.breakeven.assert_awaited_once_with("target")
    asyncio.run(svc.manage(OPENING + 900))
    assert svc.context.order_mgr.close_lot.await_args.args == ("target",)
    assert svc.context.order_mgr.close_lot.await_args.kwargs["exit_reason"] == "opening_vwap_time_exit"
    assert svc.context.order_mgr.close_lot.await_args.kwargs["market_exit"] is True


def test_accepted_but_unfilled_market_order_is_cancelled_at_end_of_its_window():
    svc = service()
    svc.context.order_mgr.list_lots.return_value = [{
        "lot_id": "pending", "symbol": SYMBOL, "state": "PENDING",
        "signal_snapshot": {"strategy_id": STRATEGY_ID, "opening_vwap_opening": OPENING,
                            "opening_vwap_evidence": {"close_at": CLOSE}},
    }]
    asyncio.run(svc.manage(CLOSE + 11))
    svc.context.order_mgr.cancel_lot.assert_not_awaited()
    asyncio.run(svc.manage(CLOSE + 12))
    svc.context.order_mgr.cancel_lot.assert_awaited_once_with("pending")
    svc.context.order_mgr.close_lot.assert_not_awaited()


def test_daily_limit_counts_filled_and_uncertain_intents(tmp_path):
    gateway = SimpleNamespace(enabled=False)
    manager = OrderManager(gateway, None, state_path=tmp_path / "lots.json")
    for index, state in enumerate(("CLOSED", "FAILED")):
        manager._lots[str(index)] = OrderLot(
            lot_id=str(index), symbol=SYMBOL if index else "NASDAQ:AMD", side="long", qty=1,
            entry_px=100, stop_px=99, trail_amount=0, state=state,
            filled_qty=1 if index == 0 else 0, entry_submission_pending=index == 1,
            signal_snapshot={"opening_vwap_opening": OPENING, "opening_vwap_signal_id": str(index)},
        )
    manager.recovery_status = lambda: {"blocks_entry": False}
    lot, reason = asyncio.run(manager.place_limit_stop_lot(
        "NASDAQ:NVDA", "long", 1, 100, 99, submission_deadline=CLOSE + 12,
        signal_snapshot={"opening_vwap_opening": OPENING, "opening_vwap_signal_id": "new"},
    ))
    assert lot is None and "两笔" in reason
