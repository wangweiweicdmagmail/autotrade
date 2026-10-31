# -*- coding: utf-8 -*-
"""行情内容健康、stale 自动恢复与开仓硬闸门回归。"""
import time

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
