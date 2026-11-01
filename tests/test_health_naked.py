# -*- coding: utf-8 -*-
"""健康巡检 + 裸仓巡检回归（console/app.py）。

守的口径：
- 数据陈旧：仅 ET 工作日 09:30–16:00 内传输中断、内容停止变化或 bar 超龄
  才 warning；恢复自动解除并通知；非交易时段/周末静默（活跃中的也静默
  摘除，绝不误报）。
- TWS 断线：connected 跳变 False → critical；恢复 → 解除通知；首轮基线不报。
- 裸仓巡检：ACTIVE lot 的 STP 缺失 → critical + _restore_protection 自动
  补挂（lot.stop_px）；补挂成功 → 解除；连续 3 次失败 → close_lot 市价平仓
  + 最高级告警；快照不完整（trade_map=None）整拍跳过不误报；dry-run/未连接
  跳过；正常仓位绝不被动。
全 mock（notifier 用真实 Notifier + 内存 record_event，iMessage 未配置跳过）。
"""
import pytest

from engine.notifier import Notifier
from .conftest import run


class FakeTrade:
    def __init__(self, done=False):
        self._done = done

    def isDone(self):
        return self._done


class FakeLot:
    def __init__(self, lot_id="lot-n1", symbol="NASDAQ:QCOM", side="long",
                 filled=100, stop=95.0, alive=True):
        self.lot_id = lot_id
        self.symbol = symbol
        self.side = side
        self.filled_qty = filled
        self.qty = filled
        self.stop_px = stop
        self.state = "ACTIVE"
        self.last_px = 101.0
        self.avg_fill = 100.0
        self.entry_px = 100.0
        self.trail_order_id = 8802
        self.trail_trade = FakeTrade(done=not alive)


class FakeGateway:
    def __init__(self, connected=True, enabled=True, trades=None):
        self._connected = connected
        self.enabled = enabled
        self._trades = trades if trades is not None else {}

    async def trade_map(self):
        return None if self._trades is None else dict(self._trades)


class FakeOrderMgr:
    def __init__(self, lots, restore_ok=True):
        self._lots = {l.lot_id: l for l in lots}
        self.restore_ok = restore_ok
        self.restore_calls = []
        self.close_calls = []

    def list_lots(self, active_only=False):
        return [{"lot_id": l.lot_id, "state": l.state, "filled_qty": l.filled_qty}
                for l in self._lots.values()]

    def get(self, lot_id):
        return self._lots.get(lot_id)

    async def _restore_protection(self, lot):
        self.restore_calls.append(lot.lot_id)
        if self.restore_ok:
            lot.trail_trade = FakeTrade(done=False)   # 补挂成功：句柄复活
        return self.restore_ok

    async def close_lot(self, lot_id, ref_px=0.0, exit_reason=""):
        self.close_calls.append((lot_id, ref_px, exit_reason))
        return True, ""


@pytest.fixture
def app(console_app, monkeypatch):
    n = Notifier(record_event=lambda *a, **k: None,
                 imessage_recipient=lambda: "")   # 未配置 → iMessage 跳过
    monkeypatch.setattr(console_app, "notifier", n)
    return console_app


# ----------------------------------------------------------------------
# TWS 断线
# ----------------------------------------------------------------------

def test_tws_disconnect_and_recovery(app, monkeypatch):
    """connected 跳变 False→critical；恢复→解除通知；首轮基线不报。"""
    async def go():
        gw = FakeGateway(connected=False)
        monkeypatch.setattr(app, "gateway", gw)
        monkeypatch.setattr(app.system, "_is_trading_hours", lambda: False)
        # 首轮：基线，不报
        r = await app.monitoring._health_check_once(None)
        assert r is False and not app.notifier.is_active("tws_down")
        # 断线跳变
        r = await app.monitoring._health_check_once(True)
        assert app.notifier.is_active("tws_down")
        a = next(x for x in app.notifier.active_alerts() if x["key"] == "tws_down")
        assert a["level"] == "critical" and "7496" in a["msg"]
        # 恢复
        gw._connected = True
        r = await app.monitoring._health_check_once(False)
        assert r is True and not app.notifier.is_active("tws_down")
    run(go())


# ----------------------------------------------------------------------
# 数据陈旧
# ----------------------------------------------------------------------

def test_data_stale_trading_hours(app, monkeypatch):
    """交易时段内传输/内容/bar 任一陈旧 → warning；恢复 → 解除通知。"""
    async def go():
        import time as _t
        now = _t.time()
        monkeypatch.setattr(app, "gateway", FakeGateway(connected=True))
        monkeypatch.setattr(app.collector, "_is_trading_hours", lambda: True)
        monkeypatch.setattr(app.collector, "_running", True)
        monkeypatch.setattr(app.collector, "latest_bar_time", now - 700)
        monkeypatch.setattr(app.collector, "_last_update", now - 700)
        monkeypatch.setattr(app.collector, "_last_content_update", now - 700)
        await app.monitoring._health_check_once(True)
        assert app.notifier.is_active("data_stale")
        a = next(x for x in app.notifier.active_alerts()
                 if x["key"] == "data_stale")
        assert a["level"] == "warning"
        now = _t.time()
        monkeypatch.setattr(app.collector, "latest_bar_time", now)
        monkeypatch.setattr(app.collector, "_last_update", now)
        monkeypatch.setattr(app.collector, "_last_content_update", now)
        await app.monitoring._health_check_once(True)
        assert not app.notifier.is_active("data_stale")
    run(go())


def test_data_stale_silent_off_hours_and_weekend(app, monkeypatch):
    """非交易时段/周末静默：超龄不报；活跃中的也静默摘除（周末绝不误报）。"""
    async def go():
        import time as _t
        monkeypatch.setattr(app, "gateway", FakeGateway(connected=True))
        monkeypatch.setattr(app.collector, "latest_bar_time", _t.time() - 7000)
        monkeypatch.setattr(app.collector, "_is_trading_hours", lambda: False)
        await app.monitoring._health_check_once(True)
        assert not app.notifier.is_active("data_stale")
        # 活跃中的告警进入非交易时段 → 静默摘除（无恢复通知）
        app.notifier._active["data_stale"] = {
            "key": "data_stale", "level": "warning", "title": "行情数据陈旧",
            "msg": "x", "since": _t.time()}
        await app.monitoring._health_check_once(True)
        assert not app.notifier.is_active("data_stale")
    run(go())


# ----------------------------------------------------------------------
# 裸仓巡检
# ----------------------------------------------------------------------

def test_naked_alive_noop(app, monkeypatch):
    """STP 存活 → 不告警不补挂（正常仓位绝不动）。"""
    async def go():
        lot = FakeLot(alive=True)
        monkeypatch.setattr(app, "gateway", FakeGateway())
        monkeypatch.setattr(app, "order_mgr", FakeOrderMgr([lot]))
        await app.monitoring._naked_position_check_once({})
        assert not app.notifier.active_alerts()
        assert app.order_mgr.restore_calls == []
    run(go())


def test_naked_missing_autorestore_recovers(app, monkeypatch):
    """STP 缺失 → critical + 自动补挂；补挂成功 → 解除通知。"""
    async def go():
        lot = FakeLot(alive=False)
        monkeypatch.setattr(app, "gateway", FakeGateway())
        monkeypatch.setattr(app, "order_mgr", FakeOrderMgr([lot], restore_ok=True))
        fails = {}
        await app.monitoring._naked_position_check_once(fails)
        key = f"naked:{lot.lot_id}"
        assert app.order_mgr.restore_calls == [lot.lot_id]
        assert not app.notifier.is_active(key)   # 已恢复
        assert fails == {}
        # 补挂后下一拍存活 → 完全静默
        await app.monitoring._naked_position_check_once(fails)
        assert not app.notifier.active_alerts()
    run(go())


def test_naked_restore_fail_3x_escalates_market_close(app, monkeypatch):
    """补挂连续 3 次失败 → 升级市价平仓（close_lot, exit_reason=naked_guard）
    + 最高级别告警。"""
    async def go():
        lot = FakeLot(alive=False)
        monkeypatch.setattr(app, "gateway", FakeGateway())
        monkeypatch.setattr(app, "order_mgr", FakeOrderMgr([lot], restore_ok=False))
        monkeypatch.setattr(app.accounts, "_tv_last_price", lambda s: 101.0)
        fails = {}
        for _ in range(2):
            await app.monitoring._naked_position_check_once(fails)
            assert app.order_mgr.close_calls == []   # 前两次只补挂不平仓
        await app.monitoring._naked_position_check_once(fails)  # 第 3 次 → 升级
        assert app.order_mgr.close_calls == [(lot.lot_id, 101.0, "naked_guard")]
        keys = {a["key"] for a in app.notifier.active_alerts()}
        assert f"naked:{lot.lot_id}:force_closed" in keys
        crit = next(a for a in app.notifier.active_alerts()
                    if a["key"].endswith("force_closed"))
        assert crit["level"] == "critical"
    run(go())


def test_naked_snapshot_incomplete_skips(app, monkeypatch):
    """订单快照不完整（trade_map=None）→ 整拍跳过，不误报不补挂。"""
    async def go():
        lot = FakeLot(alive=False)
        gw = FakeGateway()
        gw._trades = None   # 快照不完整
        monkeypatch.setattr(app, "gateway", gw)
        monkeypatch.setattr(app, "order_mgr", FakeOrderMgr([lot]))
        await app.monitoring._naked_position_check_once({})
        assert not app.notifier.active_alerts()
        assert app.order_mgr.restore_calls == []
    run(go())


def test_naked_skipped_when_disconnected_or_dry_run(app, monkeypatch):
    """未连接 / dry-run（enabled=False 虚拟成交）→ 整拍跳过。"""
    async def go():
        lot = FakeLot(alive=False)
        monkeypatch.setattr(app, "order_mgr", FakeOrderMgr([lot]))
        for gw in (FakeGateway(connected=False), FakeGateway(enabled=False)):
            monkeypatch.setattr(app, "gateway", gw)
            await app.monitoring._naked_position_check_once({})
        assert not app.notifier.active_alerts()
        assert app.order_mgr.restore_calls == []
    run(go())


def test_naked_alive_via_snapshot_handle_lost(app, monkeypatch):
    """重启后内存句柄丢失（trail_trade=None），快照里 trail_order_id 存活
    → 视为有保护，不误报。"""
    async def go():
        lot = FakeLot(alive=False)
        lot.trail_trade = None               # 句柄丢失
        alive_trade = FakeTrade(done=False)
        monkeypatch.setattr(app, "gateway",
                            FakeGateway(trades={lot.trail_order_id: alive_trade}))
        monkeypatch.setattr(app, "order_mgr", FakeOrderMgr([lot]))
        await app.monitoring._naked_position_check_once({})
        assert not app.notifier.active_alerts()
        assert app.order_mgr.restore_calls == []
    run(go())
