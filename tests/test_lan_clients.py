# -*- coding: utf-8 -*-
"""局域网多客户端回归（console/app.py 注册/审批/token 闸门）。

守的口径：本机=master 免确认；非本机注册→pending→写操作 403→approve 放行；
同名 pending 幂等返回原 token、已审批名称 409；reject 后 token 失效；
clients.json 落盘 + 重启加载保持有效；lot.trader=下单交易员名；旧
lots_state（无 trader 字段）加载兼容。TestClient 不起服务、不落真实文件。
"""
import json

import pytest
from starlette.testclient import TestClient

from .conftest import run


@pytest.fixture
def app(console_app, monkeypatch):
    monkeypatch.setattr(console_app.clients, "_clients", {"tokens": {}})
    return console_app


@pytest.fixture
def clients(app):
    local = TestClient(app.app, client=("127.0.0.1", 50000))   # 本机 = master
    remote = TestClient(app.app)                                # 非本机
    return local, remote


def test_local_is_master(app, clients):
    """① 本机 self=master；本机注册被拒；非本机无 token=guest。"""
    local, remote = clients
    r = local.get("/api/clients/self")
    assert r.status_code == 200 and r.json() == {
        "role": "master", "name": "master", "approved": True}
    r = local.post("/api/clients/register", json={"name": "x"})
    assert r.status_code == 400
    r = remote.get("/api/clients/self")
    assert r.json() == {"role": "guest", "approved": False}


def test_register_approve_flow(app, clients, tmp_path):
    """② 注册→pending→闸门403→approve→放行；同名 pending 幂等；落盘+重启有效。"""
    local, remote = clients
    r = remote.post("/api/clients/register", json={"name": "alice"})
    assert r.status_code == 200 and r.json()["approved"] is False
    tok = r.json()["token"]
    assert len(tok) == 32

    r = remote.get("/api/clients/self", headers={"X-Trader-Token": tok})
    assert r.json()["role"] == "trader" and r.json()["approved"] is False

    r2 = remote.post("/api/clients/register", json={"name": "alice"})
    assert r2.status_code == 200 and r2.json()["token"] == tok   # 幂等

    r = remote.post("/api/lots/lot-x/cancel", headers={"X-Trader-Token": tok})
    assert r.status_code == 403 and "等待 master 审批" in r.text
    r = remote.post("/api/auto/stop")
    assert r.status_code == 403                                  # 无 token

    r = remote.post(f"/api/clients/{tok}/approve")
    assert r.status_code == 403                                  # 非本机不可审批

    r = local.post(f"/api/clients/{tok}/approve")
    assert r.status_code == 200 and r.json()["approved"] is True

    # 已审批 token → 闸门放行（lot 不存在走业务 400，说明过了闸门）
    r = remote.post("/api/lots/lot-x/cancel", headers={"X-Trader-Token": tok})
    assert r.status_code == 400 and "lot 不存在" in r.text
    r = remote.get("/api/clients/self", headers={"X-Trader-Token": tok})
    assert r.json()["approved"] is True

    # 落盘结构 + 模拟重启加载保持有效
    saved = json.loads(app.clients._CLIENTS_PATH.read_text(encoding="utf-8"))
    assert saved["tokens"][tok]["approved"] is True
    assert saved["tokens"][tok]["approved_at"] > 0
    app.clients._clients.clear()
    app.clients._clients.update(app.clients._load_clients())
    r = remote.post("/api/lots/lot-x/cancel", headers={"X-Trader-Token": tok})
    assert r.status_code == 400   # 400=业务错误非 403，token 仍有效


def test_name_conflicts(app, clients):
    """③ 已审批名称再注册 409；空/超长/特殊字符 400。"""
    local, remote = clients
    tok = remote.post("/api/clients/register", json={"name": "alice"}).json()["token"]
    local.post(f"/api/clients/{tok}/approve")
    r = remote.post("/api/clients/register", json={"name": "alice"})
    assert r.status_code == 409 and "名称已被使用" in r.text
    for bad in ("  ", "a" * 21, "bad name!"):
        r = remote.post("/api/clients/register", json={"name": bad})
        assert r.status_code == 400, bad


def test_reject_invalidates_token(app, clients):
    """⑤ reject 后闸门 403。"""
    local, remote = clients
    tok = remote.post("/api/clients/register", json={"name": "bob"}).json()["token"]
    local.post(f"/api/clients/{tok}/approve")
    r = local.post(f"/api/clients/{tok}/reject")
    assert r.status_code == 200
    r = remote.post("/api/lots/lot-x/cancel", headers={"X-Trader-Token": tok})
    assert r.status_code == 403


def test_snapshot_clients_section(app, clients):
    """快照 clients 段：pending/approved 分列，只暴露 token 前 6 位。"""
    local, remote = clients
    tok = remote.post("/api/clients/register", json={"name": "alice"}).json()["token"]
    local.post(f"/api/clients/{tok}/approve")
    snap = run(app.snapshot.build_snapshot())
    c = snap["clients"]
    assert any(x["name"] == "alice" and len(x["token_prefix"]) == 6
               for x in c["approved"])
    assert not any(len(str(x.get("token", ""))) > 6
                   for x in c["approved"] + c["pending"])


def test_lot_trader_field(app, tmp_path):
    """lot.trader=下单交易员名；旧 lots_state（无 trader）加载兼容 trader=''。"""
    from execution.order_lot import OrderManager

    class _StubGateway:
        enabled = False
        _connected = False

        async def realized_pnl(self):
            return 0.0

        async def place_limit_with_stop(self, symbol, side, qty, limit_px, stop_px):
            return {"ok": True, "dry_run": True, "entry_order_id": 1,
                    "stop_order_id": 2, "trade": None, "stop_trade": None}

    class _StubRisk:
        def check_new_order(self, *a, **k):
            return True, ""

    mgr = OrderManager(_StubGateway(), _StubRisk(),
                       state_path=tmp_path / "lots_state.json")
    lot, err = run(mgr.place_limit_stop_lot(
        "NASDAQ:NVDA", "long", 10, 100.0, 98.0, trader="alice"))
    assert lot is not None and lot.trader == "alice", err

    old_lot = {
        "lot_id": "lot-old0001", "symbol": "NASDAQ:NVDA", "side": "long",
        "qty": 10, "entry_px": 100.0, "stop_px": 98.0, "trail_amount": 0.0,
        "state": "CLOSED",
    }
    (tmp_path / "old_lots_state.json").write_text(
        json.dumps({"lots": [old_lot], "saved_at": 0}), encoding="utf-8")
    mgr2 = OrderManager(_StubGateway(), _StubRisk(),
                        state_path=tmp_path / "old_lots_state.json")
    loaded = mgr2.get("lot-old0001")
    assert loaded is not None and loaded.trader == ""
