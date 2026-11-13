# -*- coding: utf-8 -*-
"""跟单 follower 侧回归（copy_trader.py CopyFollower）。

守的口径：master 未成交 follower 永远无仓（PENDING→CANCELLED 无动作）；
PENDING→ACTIVE 市价镜像入场+STP 同发；stop_px 变动同步改价；CLOSED→
市价平仓（无限重试）；三种 size_mode 股数公式；qty<1 记失败；回撤熔断
拦截新开仓但平仓同步仍执行；cmd retry/dismiss/unhalt；long_only 跳空单；
启动不追历史仓。全 mock 网关 + fake master 快照序列，logs_dir 在 tmp_path，
绝不连真实 TWS。
"""
import json
import time

import pytest

import logging
logging.disable(logging.CRITICAL)

from copy_trader import CopyFollower
from .conftest import run


class FakeGateway:
    def __init__(self, equity=100000.0):
        self.enabled = True
        self._connected = True
        self.equity = equity
        self.calls = []
        self.fail_entry = False
        self.fail_close = False

    async def connect(self, timeout=15.0):
        return True

    async def disconnect(self):
        pass

    async def trade_map(self):
        return {}

    async def positions(self):
        return {}

    async def net_liquidation(self):
        return self.equity

    async def place_limit_with_stop(self, symbol, side, qty, limit_px, stop_px):
        self.calls.append(("place_limit_with_stop", symbol, side, qty,
                           limit_px, stop_px))
        if self.fail_entry:
            return {"ok": False, "status": "Rejected", "error": "rejected"}
        return {"ok": True, "dry_run": True, "entry_order_id": 88000001,
                "stop_order_id": 88000002, "status": "Filled",
                "stop_trade": None}

    async def modify_stop(self, unit, px):
        self.calls.append(("modify_stop", unit.symbol, round(px, 2)))
        unit.stop_px = round(px, 2)
        return True

    async def place_close(self, symbol, side, qty, ref_px, order_ref=""):
        self.calls.append(("place_close", symbol, side, qty,
                           round(float(ref_px), 2), order_ref))
        if self.fail_close:
            return {"ok": False, "status": "Rejected", "error": "close rejected"}
        return {"ok": True, "dry_run": True, "order_id": 88000003,
                "status": "Filled"}

    def cancel_order(self, trade):
        return True


def snap(lots, equity=100000.0):
    return {"type": "snapshot", "ts": time.time(),
            "status": {"equity": equity}, "lots": lots}


def lot(mid, state, symbol="NASDAQ:NFLX", side="long", qty=33, filled=0,
        avg=0.0, stop=0.0, exit_px=0.0):
    return {"lot_id": mid, "symbol": symbol, "side": side, "qty": qty,
            "state": state, "filled_qty": filled, "avg_fill": avg,
            "stop_px": stop, "entry_px": avg or 100.0, "last_px": avg,
            "exit_px": exit_px}


def make_follower(tmp_path, cfg_extra=None, equity=100000.0, name="T"):
    cfg = {"name": name, "master_ws": "ws://127.0.0.1:8000/ws",
           "host": "127.0.0.1", "port": 7497, "client_id": 13,
           "account_id": "", "fa_group": "", "enabled": True,
           "size_mode": "risk", "risk_usd": 300, "size_multiplier": 0.1,
           "max_drawdown_pct": 3.0, "direction": "both",
           "copy_existing_on_start": False}
    cfg.update(cfg_extra or {})
    gw = FakeGateway(equity=equity)
    f = CopyFollower(name, cfg, logs_dir=tmp_path, servers_path=None, gateway=gw)
    return f, gw


def test_entry_mirror_with_stop(tmp_path):
    """① PENDING→ACTIVE → 市价入场+STP 同发（价格/止损/risk 股数正确）。"""
    async def go():
        f, gw = make_follower(tmp_path)
        await f.startup()
        await f.process_snapshot(snap([lot("lot-1", "PENDING")]))
        assert not gw.calls
        await f.process_snapshot(snap([lot("lot-1", "ACTIVE", filled=33,
                                           avg=100.0, stop=97.0)]))
        entries = [c for c in gw.calls if c[0] == "place_limit_with_stop"]
        assert len(entries) == 1
        _, symbol, side, qty, limit_px, stop_px = entries[0]
        # risk: floor(300×0.1 / |100−97|) = floor(30/3) = 10
        assert (symbol, side, qty) == ("NASDAQ:NFLX", "long", 10)
        assert limit_px == 100.5          # 多 avg×1.005
        assert stop_px == 97.0            # STP 同发，锚定 master stop
        rec = json.loads((tmp_path / "copy-T.json").read_text())["map"]["lot-1"]
        assert rec["state"] == "ACTIVE" and rec["qty"] == 10
        assert rec["follower_order_ids"] == [88000001, 88000002]
        assert "copy_entry" in (tmp_path / "copy-T-events.jsonl").read_text()
    run(go())


def test_cancelled_without_fill_no_action(tmp_path):
    """② master 未成交 CANCELLED → follower 无仓无动作。"""
    async def go():
        f, gw = make_follower(tmp_path)
        await f.startup()
        await f.process_snapshot(snap([lot("lot-2", "PENDING")]))
        await f.process_snapshot(snap([lot("lot-2", "CANCELLED")]))
        assert not gw.calls
        assert f.map == {}
    run(go())


def test_stop_sync(tmp_path):
    """③ stop_px 变动 → follower 同步改价。"""
    async def go():
        f, gw = make_follower(tmp_path)
        await f.startup()
        await f.process_snapshot(snap([lot("lot-3", "PENDING")]))
        await f.process_snapshot(snap([lot("lot-3", "ACTIVE", filled=33,
                                           avg=100.0, stop=97.0)]))
        await f.process_snapshot(snap([lot("lot-3", "ACTIVE", filled=33,
                                           avg=100.0, stop=98.5)]))
        mods = [c for c in gw.calls if c[0] == "modify_stop"]
        assert len(mods) == 1 and mods[0][2] == 98.5
        assert f.map["lot-3"]["stop_px"] == 98.5
        assert "copy_stop_synced" in (tmp_path / "copy-T-events.jsonl").read_text()
    run(go())


def test_close_mirror(tmp_path):
    """④ master CLOSED → follower 平仓（ref=exit_px）。"""
    async def go():
        f, gw = make_follower(tmp_path)
        await f.startup()
        await f.process_snapshot(snap([lot("lot-4", "PENDING")]))
        await f.process_snapshot(snap([lot("lot-4", "ACTIVE", filled=33,
                                           avg=100.0, stop=97.0)]))
        await f.process_snapshot(snap([lot("lot-4", "CLOSED", filled=0,
                                           avg=100.0, stop=97.0, exit_px=101.2)]))
        closes = [c for c in gw.calls if c[0] == "place_close"]
        assert len(closes) == 1
        _, symbol, side, qty, ref_px, ref = closes[0]
        assert (symbol, side, qty) == ("NASDAQ:NFLX", "long", 10)
        assert ref_px == 101.2 and ref.startswith("copy-T-lot-4")
        assert f.map["lot-4"]["state"] == "CLOSED"
        assert "copy_close" in (tmp_path / "copy-T-events.jsonl").read_text()
    run(go())


def test_size_modes(tmp_path):
    """⑤ 三种 size_mode 股数公式（multiplier/balance/risk，空头定价镜像）。"""
    async def go():
        # multiplier: floor(33 × 0.1) = 3
        f, gw = make_follower(tmp_path, {"size_mode": "multiplier"}, name="M")
        await f.startup()
        await f.process_snapshot(snap([]))   # 消费首帧标记（非历史仓）
        await f.process_snapshot(snap([lot("m1", "ACTIVE", filled=33,
                                           avg=100.0, stop=97.0)]))
        e = [c for c in gw.calls if c[0] == "place_limit_with_stop"][0]
        assert e[3] == 3
        # balance: floor(33 × (50000/100000) × 1.0) = 16
        f2, gw2 = make_follower(tmp_path, {"size_mode": "balance",
                                           "size_multiplier": 1.0},
                                equity=50000.0, name="B")
        await f2.startup()
        await f2.process_snapshot(snap([]))
        await f2.process_snapshot(snap([lot("b1", "ACTIVE", filled=33,
                                            avg=100.0, stop=97.0)],
                                       equity=100000.0))
        e2 = [c for c in gw2.calls if c[0] == "place_limit_with_stop"][0]
        assert e2[3] == 16
        # risk: floor(300×0.1 / |100−97|) = 10；空头 limit = avg×0.995
        f3, gw3 = make_follower(tmp_path, name="R")
        await f3.startup()
        await f3.process_snapshot(snap([]))
        await f3.process_snapshot(snap([lot("r1", "ACTIVE", side="short",
                                            filled=33, avg=100.0, stop=103.0)]))
        e3 = [c for c in gw3.calls if c[0] == "place_limit_with_stop"][0]
        assert e3[3] == 10 and e3[4] == 99.5 and e3[5] == 103.0
    run(go())


def test_qty_below_one_failure(tmp_path):
    """⑥ qty<1 → 记失败「股数不足 1」，不下单。"""
    async def go():
        f, gw = make_follower(tmp_path, {"risk_usd": 10, "size_multiplier": 0.1})
        await f.startup()
        await f.process_snapshot(snap([]))
        await f.process_snapshot(snap([lot("s1", "ACTIVE", filled=33,
                                           avg=100.0, stop=97.0)]))
        assert not gw.calls
        assert "s1" not in f.map
        fails = [x for x in f.failures if x["master_lot_id"] == "s1"]
        assert len(fails) == 1 and fails[0]["action"] == "entry"
        assert fails[0]["error"] == "股数不足 1"
        assert fails[0]["dismissed"] is False
    run(go())


def test_drawdown_halt_blocks_entry_not_close(tmp_path):
    """⑦ 回撤 4%>3% 熔断：新开仓拦截，已有仓位平仓同步仍执行。"""
    async def go():
        f, gw = make_follower(tmp_path)
        await f.startup()                       # baseline = 100000
        await f.process_snapshot(snap([]))
        await f.process_snapshot(snap([lot("h1", "ACTIVE", filled=33,
                                           avg=100.0, stop=97.0)]))
        assert any(c[0] == "place_limit_with_stop" for c in gw.calls)
        gw.equity = 96000.0   # 净值 100000 → 96000，回撤 4% > 3%
        f._equity_ts = 0.0
        await f.tick()
        assert f.halted is True and f.drawdown_pct == 4.0
        assert "copy_halted" in (tmp_path / "copy-T-events.jsonl").read_text()
        n_before = len([c for c in gw.calls if c[0] == "place_limit_with_stop"])
        await f.process_snapshot(snap([lot("h1", "ACTIVE", filled=33, avg=100.0,
                                           stop=97.0),
                                       lot("h2", "ACTIVE", filled=33, avg=50.0,
                                           stop=48.0)]))
        assert len([c for c in gw.calls
                    if c[0] == "place_limit_with_stop"]) == n_before
        assert "h2" not in f.map
        await f.process_snapshot(snap([lot("h1", "CLOSED", avg=100.0, stop=97.0,
                                           exit_px=99.0)]))
        assert any(c[0] == "place_close" for c in gw.calls)
        assert f.map["h1"]["state"] == "CLOSED"
    run(go())


def test_cmd_retry_dismiss_unhalt(tmp_path):
    """⑧ cmd retry（重放入场）/ dismiss（标记忽略）/ unhalt（解除熔断）。"""
    async def go():
        f, gw = make_follower(tmp_path, {"risk_usd": 10, "size_multiplier": 0.1})
        await f.startup()
        await f.process_snapshot(snap([]))
        # 制造一个 entry 失败（股数不足 1）
        await f.process_snapshot(snap([lot("c1", "ACTIVE", filled=33,
                                           avg=100.0, stop=97.0)]))
        assert f._active_failure("c1", "entry")
        # retry：修好参数后人工重放 → 入场成功
        f.cfg["risk_usd"] = 300
        (tmp_path / "copy-T-cmd.json").write_text(json.dumps({"retry": ["c1"]}))
        await f.process_snapshot(snap([lot("c1", "ACTIVE", filled=33,
                                           avg=100.0, stop=97.0)]))
        await f.tick()
        assert any(c[0] == "place_limit_with_stop" for c in gw.calls)
        assert f.map["c1"]["state"] == "ACTIVE"
        assert not f._active_failure("c1", "entry")
        assert not (tmp_path / "copy-T-cmd.json").exists()   # 消费后删除
        # dismiss：制造 close 失败 → dismiss 标记忽略
        gw.fail_close = True
        await f.process_snapshot(snap([lot("c1", "CLOSED", avg=100.0,
                                           stop=97.0, exit_px=99.0)]))
        assert f._active_failure("c1", "close")
        assert f.map["c1"]["state"] == "CLOSING"
        (tmp_path / "copy-T-cmd.json").write_text(json.dumps({"dismiss": ["c1"]}))
        await f.tick()
        d = [x for x in f.failures if x["master_lot_id"] == "c1"
             and x["action"] == "close"]
        assert d and d[0]["dismissed"] is True
        gw.fail_close = False
        # unhalt：手动置 halted，cmd 解除
        f.halted = True
        (tmp_path / "copy-T-cmd.json").write_text(json.dumps({"unhalt": True}))
        await f.tick()
        assert f.halted is False
        assert "copy_unhalted" in (tmp_path / "copy-T-events.jsonl").read_text()
    run(go())


def test_close_retry_forever_and_no_historical(tmp_path):
    """⑨ close 无限重试 + copy_existing_on_start=false 不追历史仓。"""
    async def go():
        f, gw = make_follower(tmp_path)
        await f.startup()
        await f.process_snapshot(snap([]))
        await f.process_snapshot(snap([lot("x1", "ACTIVE", filled=33,
                                           avg=100.0, stop=97.0)]))
        gw.fail_close = True
        await f.process_snapshot(snap([lot("x1", "CLOSED", avg=100.0,
                                           stop=97.0, exit_px=99.0)]))
        assert f.map["x1"]["state"] == "CLOSING"
        assert "x1" in f._close_retry
        f._close_retry["x1"] = 0.0   # 强制到期重试一次，仍失败 → 仍在队列
        await f.tick()
        assert f.map["x1"]["state"] == "CLOSING" and "x1" in f._close_retry
        assert len([c for c in gw.calls if c[0] == "place_close"]) == 2
        gw.fail_close = False        # 恢复后重试成功
        f._close_retry["x1"] = 0.0
        await f.tick()
        assert f.map["x1"]["state"] == "CLOSED"
        # 启动时已有 ACTIVE 仓 → 已见但不跟（止损变动也不跟）
        f2, gw2 = make_follower(tmp_path, name="T2")
        await f2.startup()
        await f2.process_snapshot(snap([lot("old1", "ACTIVE", filled=33,
                                            avg=100.0, stop=97.0)]))
        assert not gw2.calls and "old1" in f2.skip
        await f2.process_snapshot(snap([lot("old1", "ACTIVE", filled=33,
                                            avg=100.0, stop=95.0)]))
        assert not gw2.calls
        # 启动后新出现的 ACTIVE（首帧未见）→ 正常跟单
        await f2.process_snapshot(snap([lot("old1", "ACTIVE", filled=33,
                                            avg=100.0, stop=95.0),
                                        lot("new1", "ACTIVE", filled=33,
                                            avg=100.0, stop=97.0)]))
        assert any(c[0] == "place_limit_with_stop" for c in gw2.calls)
    run(go())


def test_long_only_and_status_schema(tmp_path):
    """⑩ direction=long_only 空单跳过；状态文件 schema 完整。"""
    async def go():
        f, gw = make_follower(tmp_path, {"direction": "long_only"})
        await f.startup()
        await f.process_snapshot(snap([]))
        await f.process_snapshot(snap([lot("d1", "ACTIVE", side="short",
                                           filled=33, avg=100.0, stop=103.0),
                                       lot("d2", "ACTIVE", side="long",
                                           filled=33, avg=100.0, stop=97.0)]))
        assert "d1" not in f.map and "d1" in f.skip
        assert "d2" in f.map
        await f.tick()
        st = json.loads((tmp_path / "copy-T-status.json").read_text())
        for key in ("name", "ts", "connected", "equity", "master_ws", "synced",
                    "pending_map", "failed", "last_sync_ts", "last_error",
                    "drawdown_pct", "halted", "orders", "failures"):
            assert key in st, key
        row = st["orders"][0]
        for key in ("master_lot_id", "symbol", "side", "master_state",
                    "follower_state", "qty", "stop_px", "error"):
            assert key in row, key
        assert row["master_lot_id"] == "d2" and row["master_state"] == "ACTIVE"
        assert st["name"] == "T" and st["halted"] is False
    run(go())


def test_managed_follow_uses_actual_fills_and_reduces_to_target(tmp_path):
    from execution.order_lot import OrderManager
    from execution.risk import RiskConfig, RiskGate

    follower, gw = make_follower(tmp_path, {
        "size_mode": "multiplier", "size_multiplier": 1.0,
    })
    gw.enabled = False

    async def realized_pnl():
        return 0.0

    gw.realized_pnl = realized_pnl
    manager = OrderManager(gw, RiskGate(RiskConfig(), tmp_path), tmp_path / "managed.json")
    follower = CopyFollower(
        "managed", follower.cfg, dry_run=True, logs_dir=tmp_path,
        gateway=gw, order_manager=manager,
    )

    async def scenario():
        await follower.startup()
        await follower.process_snapshot(snap([]))
        external = lot("external", "ACTIVE", qty=10, filled=10, avg=100, stop=99)
        await follower.process_snapshot(snap([dict(external, external_managed=True)]))
        assert not manager.list_lots()
        await follower.process_snapshot(snap([
            lot("a", "ACTIVE", qty=40, filled=10, avg=100, stop=99),
        ]))
        row, = manager.list_lots()
        assert row["filled_qty"] == 10
        await follower.process_snapshot(snap([
            lot("a", "ACTIVE", qty=40, filled=7, avg=100, stop=99),
        ]))
        assert manager.get(row["lot_id"]).filled_qty == 7
        await follower.process_snapshot(snap([
            lot("a", "CLOSED", qty=40, filled=7, avg=100, stop=99),
        ]))
        assert manager.get(row["lot_id"]).state == "CLOSED"
        await follower.process_snapshot(snap([
            lot("a", "ACTIVE", qty=40, filled=10, avg=100, stop=99),
        ]))
        assert len(manager.list_lots()) == 1

    run(scenario())


def test_managed_map_corruption_preserves_file_and_blocks_entries(tmp_path):
    from execution.order_lot import OrderManager
    from execution.risk import RiskConfig, RiskGate

    follower, gw = make_follower(tmp_path)
    manager = OrderManager(gw, RiskGate(RiskConfig(), tmp_path), tmp_path / "managed.json")
    follower = CopyFollower("managed", follower.cfg, logs_dir=tmp_path, gateway=gw, order_manager=manager)
    follower.map_path.write_text("{broken", encoding="utf-8")

    async def scenario():
        await follower.startup()
        frame = snap([lot("a", "ACTIVE", filled=10, avg=100, stop=99)])
        frame["status"]["dry_run"] = False
        await follower.process_snapshot(frame)
        assert not manager.list_lots()
        assert follower.map_path.read_text(encoding="utf-8") == "{broken"
        assert "待" in follower._build_status()["last_error"] or "损坏" in follower._build_status()["last_error"]

    run(scenario())


def test_managed_disconnect_keeps_real_position_and_pending_order(tmp_path):
    from execution.order_lot import OrderLot, OrderManager
    from execution.risk import RiskConfig, RiskGate

    _, gw = make_follower(tmp_path)
    gw._connected = False
    manager = OrderManager(gw, RiskGate(RiskConfig(), tmp_path), tmp_path / "managed.json")
    active = OrderLot("active", "NASDAQ:NVDA", "long", 10, 100, 99, 0,
                      state="ACTIVE", avg_fill=100, filled_qty=10)
    pending = OrderLot("pending", "NASDAQ:NVDA", "long", 10, 100, 99, 0,
                       state="PENDING")
    manager._lots = {row.lot_id: row for row in (active, pending)}
    assert not run(manager.close_lot("active", 101))[0]
    assert not run(manager.close_half("active", 101))[0]
    assert not run(manager.cancel_lot("pending"))[0]
    assert (active.state, active.filled_qty, pending.state) == ("ACTIVE", 10, "PENDING")
    assert not gw.calls
