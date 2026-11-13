# -*- coding: utf-8 -*-
"""跟单系统 master 侧回归（console/app.py 跟单 CRUD + CopySupervisor）。

守的口径：/api/copy/servers CRUD 全链路 + clientId 自动分配（13 起避开
保留号段）；名称/参数校验；cmd 合并写入；supervisor reload 增量比对
（新增/改参/停用/崩溃指数退避重启/10次封顶放弃/SIGTERM→SIGKILL 升级）；
快照 copy 段 = 配置 + 状态文件白名单合入。TestClient 不起服务、路径全在
tmp_path、subprocess 全 mock。
"""
from console.services.copy import CopySupervisor
import asyncio
import json
import time

import pytest
from fastapi.testclient import TestClient

from .conftest import run


@pytest.fixture
def app(console_app, monkeypatch):
    monkeypatch.setattr(console_app.clients, "_is_local", lambda request: True)
    console_app._events_test = []
    monkeypatch.setattr(console_app.events, "record_event",
                        lambda t, **d: console_app._events_test.append(
                            {"type": t, **d}))
    return console_app


def mk_cfg(name, **over):
    cfg = {"name": name, "master_ws": "ws://127.0.0.1:8000/ws",
           "host": "127.0.0.1", "port": 7497, "client_id": 13,
           "account_id": "", "fa_group": "", "enabled": True,
           "size_mode": "risk", "risk_usd": 300, "size_multiplier": 0.1,
           "max_drawdown_pct": 3.0, "direction": "both",
           "copy_existing_on_start": False}
    cfg.update(over)
    return cfg


def test_crud_and_client_id_allocation(app, tmp_path):
    """① CRUD 全链路 + clientId 自动分配 + 落盘原子写。"""
    client = TestClient(app.app)   # 不进 with → 不跑 startup（不碰 8000）
    r = client.get("/api/copy/servers")
    assert r.status_code == 200 and r.json() == {"servers": []}

    r = client.post("/api/copy/servers", json={"name": "A"})
    assert r.status_code == 200, r.text
    srv_a = r.json()["server"]
    assert srv_a["client_id"] == 13
    assert srv_a["enabled"] is False
    assert srv_a["connected"] is None and srv_a["equity"] is None
    assert srv_a["process_alive"] is False
    assert srv_a["port"] == 7497 and srv_a["size_mode"] == "risk" \
        and srv_a["direction"] == "both" and srv_a["risk_usd"] == 300

    r = client.post("/api/copy/servers", json={"name": "B", "enabled": True})
    assert r.status_code == 200 and r.json()["server"]["client_id"] == 14

    on_disk = json.loads((tmp_path / "copy_servers.json").read_text())
    assert len(on_disk["servers"]) == 2
    assert not list(tmp_path.glob("*.tmp")), "原子写有 .tmp 残留"

    r = client.post("/api/copy/servers/A/update",
                    json={"risk_usd": 500, "direction": "long"})
    assert r.status_code == 200
    srv_a = r.json()["server"]
    assert srv_a["risk_usd"] == 500 and srv_a["direction"] == "long"
    assert srv_a["client_id"] == 13 and srv_a["port"] == 7497

    r = client.post("/api/copy/servers/A/enable")
    assert r.status_code == 200 and r.json()["server"]["enabled"] is True
    r = client.post("/api/copy/servers/A/disable")
    assert r.json()["server"]["enabled"] is False

    r = client.post("/api/copy/servers/B/delete")
    assert r.status_code == 200
    names = [s["name"] for s in client.get("/api/copy/servers").json()["servers"]]
    assert names == ["A"]

    ev_types = [e["type"] for e in app._events_test]
    for t in ("copy_server_added", "copy_server_updated", "copy_server_enabled",
              "copy_server_disabled", "copy_server_deleted"):
        assert t in ev_types, t


def test_validation(app):
    """② name 冲突/非法、client_id 冲突/占用、size_mode/master_ws 非法、404。"""
    client = TestClient(app.app)
    client.post("/api/copy/servers", json={"name": "A"})
    r = client.post("/api/copy/servers", json={"name": "A"})
    assert r.status_code == 409
    for bad in ("", "x" * 11, "bad name!", "a/b"):
        r = client.post("/api/copy/servers", json={"name": bad})
        assert r.status_code == 400, bad
    r = client.post("/api/copy/servers",
                    json={"name": "C", "client_id": app.config.IBKR_CLIENT_ID})
    assert r.status_code == 400
    r = client.post("/api/copy/servers", json={"name": "C", "client_id": 13})
    assert r.status_code == 400
    r = client.post("/api/copy/servers", json={"name": "C", "size_mode": "x"})
    assert r.status_code == 400
    r = client.post("/api/copy/servers", json={"name": "C", "master_ws": "http://x"})
    assert r.status_code == 400
    r = client.post("/api/copy/servers/NOPE/update", json={"risk_usd": 1})
    assert r.status_code == 404
    r = client.post("/api/copy/servers/A/update",
                    json={"client_id": 20, "port": 7498})
    assert r.status_code == 200 and r.json()["server"]["client_id"] == 20


def test_cmd_merge_write(app, tmp_path):
    """③ cmd 写入合并（去重追加 + 落盘一致 + 空指令 400 + 不存在 404）。"""
    client = TestClient(app.app)
    client.post("/api/copy/servers", json={"name": "A"})
    r = client.post("/api/copy/servers/A/cmd", json={"retry": ["o1"], "unhalt": True})
    assert r.status_code == 200
    assert r.json()["cmd"] == {"retry": ["o1"], "dismiss": [], "unhalt": True}
    r = client.post("/api/copy/servers/A/cmd",
                    json={"retry": ["o1", "o2"], "dismiss": ["o3"]})
    assert r.json()["cmd"] == {"retry": ["o1", "o2"], "dismiss": ["o3"],
                               "unhalt": True}
    cmd_disk = json.loads((tmp_path / "copy-A-cmd.json").read_text())
    assert cmd_disk == {"retry": ["o1", "o2"], "dismiss": ["o3"], "unhalt": True}
    r = client.post("/api/copy/servers/A/cmd", json={})
    assert r.status_code == 400
    r = client.post("/api/copy/servers/NOPE/cmd", json={"unhalt": True})
    assert r.status_code == 404


def test_supervisor_reload_lifecycle(app, tmp_path, monkeypatch):
    """④ supervisor：缺失文件只告警 / reload 增量 / 崩溃退避重启 / 10 次封顶
    / SIGTERM 超时升级 SIGKILL。"""
    fake_procs = []

    class FakeProc:
        def __init__(self, args):
            self.args = args
            self.pid = 1000 + len(fake_procs)
            self.returncode = None

        def terminate(self):
            self.returncode = -15

        def kill(self):
            self.returncode = -9

        async def wait(self):
            return self.returncode

    async def fake_exec(*args, **kw):
        p = FakeProc(args)
        fake_procs.append(p)
        return p

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    fake_trader = tmp_path / "copy_trader.py"
    fake_trader.write_text("# fake\n")
    monkeypatch.setattr(app.copy, "_COPY_TRADER_PY", fake_trader)

    async def go():
        # 文件缺失 → 只告警不启动
        monkeypatch.setattr(app.copy, "_COPY_TRADER_PY", tmp_path / "nope.py")
        sup0 = CopySupervisor(app)
        await sup0.start([mk_cfg("X")])
        assert not sup0._running and not fake_procs
        monkeypatch.setattr(app.copy, "_COPY_TRADER_PY", fake_trader)

        sup = CopySupervisor(app)
        monkeypatch.setattr(app, "copy_supervisor", sup)
        await sup.start([mk_cfg("A", enabled=True), mk_cfg("B", enabled=False)])
        assert sup.process_alive("A") and not sup.process_alive("B")
        assert list(fake_procs[-1].args[:3]) == ["python3", str(fake_trader),
                                                 "--name"]
        assert "A" in fake_procs[-1].args
        assert "--dry-run" in fake_procs[-1].args   # EXECUTION_ENABLED=0

        # reload 无变化 → 不重启
        n0 = len(fake_procs)
        await sup.reload([mk_cfg("A"), mk_cfg("B", enabled=False)])
        assert len(fake_procs) == n0 and sup.process_alive("A")

        # 改参 → 旧进程 SIGTERM + 新进程 spawn
        old_proc = sup._procs["A"]
        await sup.reload([mk_cfg("A", risk_usd=999)])
        assert old_proc.returncode == -15
        assert sup.process_alive("A") and len(fake_procs) == n0 + 1

        # 新增 enabled → spawn；停用 → terminate
        await sup.reload([mk_cfg("A", risk_usd=999), mk_cfg("B")])
        assert sup.process_alive("B")
        proc_b = sup._procs["B"]
        await sup.reload([mk_cfg("A", risk_usd=999), mk_cfg("B", enabled=False)])
        assert not sup.process_alive("B") and proc_b.returncode == -15

        # 崩溃自动重启（指数退避）
        proc_a = sup._procs["A"]
        proc_a.returncode = 1
        await sup._check_once()
        assert any(e["type"] == "copy_process_died" and e.get("name") == "A"
                   and e.get("returncode") == 1 for e in app._events_test)
        assert "A" in sup._pending_restart and not sup.process_alive("A")
        sup._pending_restart["A"] = 0   # 拨快时钟，跳过退避等待
        n1 = len(fake_procs)
        await sup._check_once()
        assert sup.process_alive("A") and len(fake_procs) == n1 + 1

        # 一小时 10 次封顶 → 放弃重启
        sup._restarts["A"] = [time.time()] * 10
        sup._procs["A"].returncode = 1
        await sup._check_once()
        assert "A" not in sup._pending_restart
        assert any(e.get("gave_up") for e in app._events_test
                   if e["type"] == "copy_process_died")

        # SIGTERM 不退出 → SIGKILL
        class StuckProc(FakeProc):
            def terminate(self):
                pass   # 无视 SIGTERM

        stuck = StuckProc(["x"])
        sup._procs["STUCK"] = stuck

        async def _timeout_wait_for(aw, timeout):
            raise asyncio.TimeoutError()

        monkeypatch.setattr(asyncio, "wait_for", _timeout_wait_for)
        await sup.terminate("STUCK")
        assert stuck.returncode == -9

        await sup.stop_all()
        assert not sup._running and not sup._procs

    run(go())


def test_snapshot_copy_section(app, tmp_path, monkeypatch):
    """⑤ 快照 copy 段：配置 + 状态文件白名单合入；无状态文件时字段为 null。"""
    client = TestClient(app.app)
    client.post("/api/copy/servers", json={"name": "A"})
    status_payload = {"connected": True, "equity": 100231.5, "synced": ["NVDA"],
                      "failed": ["o9"], "halted": False, "drawdown_pct": 0.8,
                      "last_sync_ts": 1761000000, "last_error": None,
                      "orders": 7, "failures": 1, "internal_field": "不导出"}
    (tmp_path / "copy-A-status.json").write_text(json.dumps(status_payload))

    async def fake_status():
        return {"equity": None, "daily_pnl": None, "ts": time.time()}

    monkeypatch.setattr(app.accounts, "get_status_cached", fake_status)

    async def go():
        snap = await app.snapshot.build_snapshot()
        assert "copy" in snap and "servers" in snap["copy"]
        row = next(s for s in snap["copy"]["servers"] if s["name"] == "A")
        assert row["client_id"] == 13 and row["size_mode"] == "risk"
        assert row["connected"] is True and row["equity"] == 100231.5
        assert row["synced"] == ["NVDA"] and row["failed"] == ["o9"]
        assert row["drawdown_pct"] == 0.8 and row["orders"] == 7
        assert row["failures"] == 1 and row["last_sync_ts"] == 1761000000
        assert row["last_error"] is None and row["halted"] is False
        assert row["process_alive"] is False
        assert "internal_field" not in row
        (tmp_path / "copy-A-status.json").unlink()
        snap2 = await app.snapshot.build_snapshot()
        row2 = snap2["copy"]["servers"][0]
        assert row2["connected"] is None and row2["orders"] is None
        assert row2["process_alive"] is False

    run(go())


def test_copy_socket_requires_token_and_returns_copy_frame(app, monkeypatch):
    from starlette.websockets import WebSocketDisconnect

    token = "a" * 64
    client = TestClient(app.app)
    async def snapshot():
        return {"type": "snapshot", "ts": 1, "lots": [],
                "status": {"dry_run": False, "equity": 100}, "logs": ["private"]}

    monkeypatch.setattr(app.snapshot, "build_snapshot", snapshot)
    monkeypatch.delenv("COPY_CHANNEL_TOKEN", raising=False)
    with pytest.raises(WebSocketDisconnect), client.websocket_connect("/ws/copy"):
        pass
    monkeypatch.setenv("COPY_CHANNEL_TOKEN", token)
    with pytest.raises(WebSocketDisconnect), client.websocket_connect(
            "/ws/copy", headers={"Authorization": "Bearer invalid"}):
        pass
    with client.websocket_connect("/ws/copy", headers={"Authorization": f"Bearer {token}"}) as ws:
        assert ws.receive_json() == {"type": "snapshot", "ts": 1, "lots": [],
                                     "status": {"dry_run": False, "equity": 100}}
    with client.websocket_connect("/ws") as ws:
        assert ws.receive_json()["logs"] == ["private"]
