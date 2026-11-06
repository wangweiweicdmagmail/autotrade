# -*- coding: utf-8 -*-
"""行情内容健康、stale 自动恢复与开仓硬闸门回归。"""
import asyncio
import time

import pytest

from engine.realtime_collector import RealtimeCollector
from execution.order_lot import OrderManager
from execution.risk import RiskConfig, RiskGate
from .conftest import run


def _collector():
    c = RealtimeCollector(["NASDAQ:QQQ"])
    c._running = True
    return c


def _tick(close=100.0, bar_time=1_000.0, volume=10):
    return {"type": "tick", "symbols": {
        "NASDAQ:QQQ": {"bar": {"time": bar_time, "close": close,
                                "volume": volume}}}}


def test_repeated_old_ticks_are_not_fresh_content():
    c = _collector()
    now = time.time()
    c._accept_tick(_tick(bar_time=now), now=now)
    c._accept_tick(_tick(bar_time=now), now=now + 20)
    health = c.data_health(now + 20, trading=True)
    assert health["transport_age"] == 0.0
    assert health["content_age"] == 20.0
    assert health["stale"] is False
    health = c.data_health(now + 46, trading=True)
    assert health["transport_age"] == 26.0
    assert health["content_age"] == 46.0
    assert health["stale"] is True
    assert "content_stale" in health["reason"]
    assert health["trade_allowed"] is False


def test_real_content_change_recovers_health():
    c = _collector()
    now = time.time()
    c._accept_tick(_tick(bar_time=now), now=now)
    assert c.data_health(now + 50, trading=True)["stale"] is True
    c._accept_tick(_tick(close=100.2, bar_time=now, volume=20), now=now + 51)
    health = c.data_health(now + 52, trading=True)
    assert health["content_age"] == 1.0
    assert health["stale"] is False
    assert health["trade_allowed"] is True


def test_local_calculation_does_not_make_an_older_price_look_fresh():
    c = _collector()
    tick = _tick(bar_time=1000)
    tick["symbols"]["NASDAQ:QQQ"]["price_observed_at"] = 1001
    c._accept_tick(tick, now=1002)
    assert c.latest()["NASDAQ:QQQ"]["price_observed_at"] == 1001
    assert c.data_health(1003, trading=True)["content_age"] == 2


def test_off_hours_silence_does_not_block_or_restart():
    c = _collector()
    now = time.time()
    c._accept_tick(_tick(bar_time=now - 7_000), now=now - 7_000)
    health = c.data_health(now, trading=False)
    assert health["content_age"] == 7_000.0
    assert health["stale"] is False
    assert health["trade_allowed"] is True


def test_bar_timestamp_stale_is_detected_even_when_ticks_change():
    c = _collector()
    now = time.time()
    c._accept_tick(_tick(bar_time=now - 700), now=now)
    c._accept_tick(_tick(close=100.1, bar_time=now - 700), now=now + 1)
    health = c.data_health(now + 2, trading=True)
    assert health["content_age"] == 1.0
    assert health["bar_age"] == 702.0
    assert "bar_stale" in health["reason"]


class _FakeProcess:
    returncode = None

    def __init__(self):
        self.terminated = False

    def terminate(self):
        self.terminated = True

    async def wait(self):
        self.returncode = 0


def test_watchdog_allows_recent_batch_progress_but_restarts_if_it_stalls(monkeypatch):
    c = _collector()
    c._proc = _FakeProcess()
    c._last_launch = 1000.0
    c._last_startup_progress = 1088.0
    clock = iter([1090.0, 1095.0, 1105.0])
    now = 0.0
    restarts = []

    async def advance(_):
        nonlocal now
        now = next(clock, None)
        if now is None:
            raise asyncio.CancelledError

    async def restart(reason):
        restarts.append((now, reason))

    monkeypatch.setattr("engine.realtime_collector.asyncio.sleep", advance)
    monkeypatch.setattr("engine.realtime_collector.time.time", lambda: now)
    monkeypatch.setattr(c, "_is_trading_hours", lambda: True)
    monkeypatch.setattr(c, "_restart", restart)
    assert c.data_health(1090.0, trading=True)["trade_allowed"] is False
    with pytest.raises(asyncio.CancelledError):
        run(c._watchdog_loop())
    assert restarts == [(1105.0, "transport_stale,content_stale,bar_stale")]


def test_restart_terminates_old_process_before_launch(monkeypatch):
    c = _collector()
    old = _FakeProcess()
    c._proc = old
    calls = []

    async def fake_launch():
        calls.append("launch")
        c._proc = _FakeProcess()

    monkeypatch.setattr(c, "_launch", fake_launch)

    async def go():
        await c._restart("content_stale")
        assert old.terminated is True
        assert calls == ["launch"]
        assert c._reader_task is None and c._drain_task is None
    run(go())


class _FakeGateway:
    enabled = False
    _connected = False

    def __init__(self):
        self.place_calls = 0

    async def realized_pnl(self):
        return 0.0

    async def place_limit_with_stop(self, symbol, side, qty, limit_px, stop_px):
        self.place_calls += 1
        return {"ok": True, "dry_run": True,
                "entry_order_id": 1, "stop_order_id": 2}


def _manager(tmp_path, health):
    gw = _FakeGateway()
    gate = RiskGate(RiskConfig(max_open_positions=0), halt_dir=tmp_path)
    mgr = OrderManager(gw, gate, state_path=tmp_path / "lots_state.json")
    mgr.market_health = lambda: health
    return mgr, gw


def test_stale_health_blocks_every_new_order_before_broker(tmp_path):
    mgr, gw = _manager(tmp_path, {
        "trade_allowed": False, "reason": "content_stale"})

    async def go():
        lot, err = await mgr.place_limit_stop_lot(
            "NASDAQ:QQQ", "long", 10, 100.0, 98.0)
        assert lot is None
        assert "content_stale" in err
        assert gw.place_calls == 0
        assert mgr.list_lots() == []
    run(go())


def test_health_failure_fails_closed_without_touching_broker(tmp_path):
    gw = _FakeGateway()
    gate = RiskGate(RiskConfig(max_open_positions=0), halt_dir=tmp_path)
    mgr = OrderManager(gw, gate, state_path=tmp_path / "lots_state.json")

    def broken():
        raise RuntimeError("health unavailable")
    mgr.market_health = broken

    async def go():
        lot, err = await mgr.place_limit_stop_lot(
            "NASDAQ:QQQ", "long", 10, 100.0, 98.0)
        assert lot is None and "健康检查失败" in err
        assert gw.place_calls == 0
    run(go())


def test_fresh_allows_order_and_stale_does_not_block_close(tmp_path):
    mgr, gw = _manager(tmp_path, {"trade_allowed": True, "reason": ""})

    async def go():
        lot, err = await mgr.place_limit_stop_lot(
            "NASDAQ:QQQ", "long", 10, 100.0, 98.0)
        assert lot is not None, err
        assert gw.place_calls == 1
        mgr.market_health = lambda: {
            "trade_allowed": False, "reason": "content_stale"}
        ok, err = await mgr.close_lot(lot.lot_id, ref_px=101.0)
        assert ok is True, err
    run(go())


def test_complete_watchlist_tick_larger_than_four_mb_is_read(monkeypatch):
    """Previous 4MB pipe cut snapshots at 48/53. Exercise actual launch limit."""
    import json
    async def go():
        c=RealtimeCollector([f'TEST:{i}' for i in range(53)])
        c._running=True
        payload={'type':'tick','symbols':{symbol:{'bar':{'time':1000,'close':100},
                 'history_context':'x'*90000} for symbol in c.symbols}}
        line=(json.dumps(payload)+'\n').encode()
        assert len(line)>4*1024*1024
        async def spawn(*args,**kwargs):
            proc=_FakeProcess()
            proc.stdout=asyncio.StreamReader(limit=kwargs['limit'])
            proc.stderr=asyncio.StreamReader(limit=kwargs['limit'])
            proc.stdout.feed_data(line)
            proc.stdout.feed_eof()
            proc.stderr.feed_eof()
            return proc
        monkeypatch.setattr('engine.realtime_collector.asyncio.create_subprocess_exec',spawn)
        await c._launch()
        await c._reader_task
        await c._drain_task
        assert len(c.latest())==53
    run(go())


def test_watchdog_recovers_dead_reader_outside_trading_hours(monkeypatch):
    async def go():
        c=_collector()
        c._proc=_FakeProcess()
        async def failed_reader():
            raise ValueError('Separator is not found, and chunk exceed the limit')
        task=asyncio.create_task(failed_reader())
        with pytest.raises(ValueError):
            await task
        c._reader_task=task
        calls=[]
        async def sleep(_):
            if calls: raise asyncio.CancelledError
        async def restart(reason): calls.append(reason)
        monkeypatch.setattr('engine.realtime_collector.asyncio.sleep',sleep)
        monkeypatch.setattr(c,'_is_trading_hours',lambda:False)
        monkeypatch.setattr(c,'_restart',restart)
        with pytest.raises(asyncio.CancelledError): await c._watchdog_loop()
        assert calls==['行情读取任务已停止']
    run(go())


def test_24h_feed_rejects_unconfirmed_data_and_removes_prior_quotes():
    c = RealtimeCollector(["NASDAQ:QQQ", "NASDAQ:MSFT"], market_session="24h")
    bar = {"time": 1000, "close": 101}
    good = {"resolved": "24h"}
    wrong = {"resolved": "extended"}
    c._accept_tick({
        "sessions": {"NASDAQ:QQQ": good, "NASDAQ:MSFT": wrong},
        "symbols": {
            "NASDAQ:QQQ": {"bar": bar, "session": good},
            "NASDAQ:MSFT": {"bar": bar, "session": wrong},
        },
    }, now=1001)
    assert set(c.latest()) == {"NASDAQ:QQQ"}
    assert c.data_health(now=1001, trading=False)["confirmed_24h_symbols"] == ("NASDAQ:QQQ",)
    assert set(c.market_session_status()["unavailable"]) == {"NASDAQ:MSFT"}

    c._accept_tick({
        "sessions": {"NASDAQ:QQQ": wrong, "NASDAQ:MSFT": wrong},
        "symbols": {"NASDAQ:QQQ": {"bar": bar, "session": wrong}},
    }, now=1002)
    assert c.latest() == {}
    assert c.market_session_status()["confirmed"] == 0


def test_24h_feed_counts_default_sessions_only_for_configured_symbols():
    stock = "NASDAQ:QQQ"
    index = "CAPITALCOM:NAS100"
    c = RealtimeCollector([stock, index], market_session="24h", default_session_symbols=[index])
    bar = {"time": 1000, "close": 101}
    stock_session = {"requested": "24h", "resolved": "24h", "accepted": True}
    default_session = {"requested": "default", "resolved": None, "accepted": True}

    def tick(stock_info, index_info, now):
        sessions = {stock: stock_info, index: index_info}
        c._accept_tick({
            "sessions": sessions,
            "symbols": {symbol: {"bar": bar, "session": info} for symbol, info in sessions.items()},
        }, now=now)

    tick(stock_session, default_session, 1001)
    assert set(c.latest()) == {stock, index}
    assert c.market_session_status()["confirmed"] == 2
    assert c.market_session_status()["unavailable"] == {}
    assert c.data_health(now=1001, trading=False)["confirmed_24h_symbols"] == (stock,)

    tick({**default_session, "resolved": "regular"}, {**default_session, "error": "series error"}, 1002)
    assert c.latest() == {}
    assert set(c.market_session_status()["unavailable"]) == {stock, index}
