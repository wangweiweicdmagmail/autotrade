"""应用工厂、任务归属、API 兼容与执行边界；不启动外部服务。"""

import asyncio
import ast
import json
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from console.factory import create_app
from console.runtime.bootstrap import build_services, RuntimePaths
from console.runtime.tasks import TaskRegistry
from engine.brokers import LiveBroker
from execution.order_lot import OrderManager
from execution.risk import RiskConfig, RiskGate
from execution.writer_lock import acquire_writer_lock, release_writer_lock
from .conftest import run
from .test_tv_data_health import _FakeGateway


def test_importing_asgi_app_does_not_load_production_state():
    script = """
import asyncio, builtins
asyncio.run(asyncio.sleep(0))
original = builtins.open
def guarded(file, *args, **kwargs):
    if any(name in str(file) for name in ('lots_state.json', 'settings.json', 'clients.json', 'creds.json')):
        raise AssertionError('application import read production state: ' + str(file))
    return original(file, *args, **kwargs)
builtins.open = guarded
import console.app
assert console.app.app.state.services is None
assert 'execution.ibkr' not in __import__('sys').modules
"""
    subprocess.run(
        [sys.executable, "-c", script], check=True, capture_output=True, text=True
    )


def test_apps_have_independent_services_state_and_paths(tmp_path):
    first = build_services(RuntimePaths(tmp_path / "one"))
    second = build_services(RuntimePaths(tmp_path / "two"))
    try:
        one = TestClient(create_app(first), client=("127.0.0.1", 50000))
        two = TestClient(create_app(second), client=("127.0.0.1", 50000))
        assert one.get("/api/auto/models").json()["models"][0]["name"] == "EMA9-ST-ATR"
        first.automatic._auto_enabled = True
        first.settings._settings["stop_atr_mult"] = 2.0
        assert one.get("/api/auto/status").json()["enabled"] is True
        assert two.get("/api/auto/status").json()["enabled"] is False
        assert "stop_atr_mult" not in second.settings._settings
        assert first.order_mgr._state_path.parent != second.order_mgr._state_path.parent
        assert not (tmp_path / "one" / "lots_state.json").exists()
    finally:
        first.close_log_handler()
        second.close_log_handler()


def test_tasks_deduplicate_and_are_awaited_on_shutdown():
    async def scenario():
        registry = TaskRegistry()
        stopped = []

        async def worker():
            try:
                await asyncio.Event().wait()
            finally:
                stopped.append(True)

        task = registry.start("quotes", worker)
        assert registry.start("quotes", worker) is task
        await asyncio.sleep(0)
        await registry.stop_all()
        await registry.stop_all()
        assert stopped == [True]
        assert registry.status()["quotes"]["cancelled"] is True

    run(scenario())


def test_startup_failure_cleans_started_tasks_and_preserves_error(
    console_app, monkeypatch
):
    context = console_app
    stopped = []

    async def worker():
        try:
            await asyncio.Event().wait()
        finally:
            stopped.append("task")

    async def failing_start():
        context.tasks.start("partial_startup", worker)
        await asyncio.sleep(0)
        raise RuntimeError("fixture startup failure")

    async def stop_news():
        stopped.append("news")

    async def stop_resources():
        stopped.append("resources")

    monkeypatch.setattr(context.lifecycle, "on_startup", failing_start)
    monkeypatch.setattr(context.news, "_news_shutdown", stop_news)
    monkeypatch.setattr(context.lifecycle, "on_shutdown", stop_resources)
    with pytest.raises(RuntimeError, match="fixture startup failure"):
        with TestClient(create_app(context)):
            pass
    assert stopped == ["task", "news", "resources"]


def test_writer_lock_cannot_be_shared_by_two_applications(tmp_path):
    first, second = object(), object()
    path = str(tmp_path / "writer.lock")
    try:
        assert acquire_writer_lock(path, owner=first)
        assert acquire_writer_lock(path, owner=first)
        assert not acquire_writer_lock(path, owner=second)
        release_writer_lock(owner=second)
        assert not acquire_writer_lock(path, owner=second)
    finally:
        release_writer_lock(owner=first)
    assert acquire_writer_lock(path, owner=second)
    release_writer_lock(owner=second)


def test_execution_dedup_is_scoped_to_model_instance(tmp_path):
    manager = OrderManager(
        _FakeGateway(),
        RiskGate(RiskConfig(max_open_positions=0), halt_dir=tmp_path),
        tmp_path / "lots.json",
    )
    broker = LiveBroker(manager)
    meta = {
        "strategy_id": "ema9_pyramid",
        "strategy_instance_id": "default",
        "group_id": "one",
        "entry_index": 1,
        "risk_budget": 10,
    }
    first = run(broker.place_limit_stop("NASDAQ:NVDA", "long", 10, 100, 99, meta=meta))
    second = run(
        broker.place_limit_stop(
            "NASDAQ:NVDA",
            "long",
            10,
            100,
            99,
            meta=meta | {"strategy_id": "fixture_model", "group_id": "two"},
        )
    )
    assert first.lot_id != second.lot_id
    with pytest.raises(RuntimeError, match="重复下单"):
        run(broker.place_limit_stop("NASDAQ:NVDA", "long", 10, 100, 99, meta=meta))


def test_legacy_signal_guards_do_not_apply_to_other_pine_models(console_app):
    monitor = console_app.monitoring
    lot = {
        "limit_type": "auto_fixture_model",
        "signal_snapshot": {"strategy_id": "fixture_model"},
    }
    assert monitor._is_pyramid_lot(lot)
    assert not monitor._uses_legacy_guards(lot)
    assert monitor._uses_legacy_guards(
        {"signal_snapshot": {"strategy_id": "ema9_pyramid"}}
    )


def test_services_and_core_do_not_import_application_entrypoint():
    for root in (Path("console/services"), Path("console/runtime"), Path("engine")):
        for file in root.rglob("*.py"):
            tree = ast.parse(file.read_text())
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    assert node.module != "console.app", file
                elif isinstance(node, ast.Import):
                    assert all(alias.name != "console.app" for alias in node.names), (
                        file
                    )


def test_existing_http_and_websocket_route_contract_is_preserved():
    path = Path(__file__).parent / "fixtures" / "phase1_routes.json"
    expected = json.loads(path.read_text())
    actual = set()
    routes = list(create_app().routes)
    while routes:
        route = routes.pop()
        included = getattr(route, "original_router", None)
        if included is not None:
            routes.extend(included.routes)
            continue
        path = getattr(route, "path", "")
        methods = getattr(route, "methods", None)
        if methods:
            actual.update((method, path) for method in methods)
        elif path == "/ws":
            actual.add(("WEBSOCKET", path))
    assert set(map(tuple, expected)) <= actual


def test_websocket_resolves_the_current_application_services(console_app, monkeypatch):
    context = console_app

    async def snapshot():
        return {"type": "snapshot", "fixture": "isolated-app"}

    monkeypatch.setattr(context.snapshot, "build_snapshot", snapshot)
    client = TestClient(context.app)
    with client.websocket_connect("/ws") as websocket:
        assert websocket.receive_json()["fixture"] == "isolated-app"
        assert len(context.snapshot._ws_clients) == 1
    assert not context.snapshot._ws_clients
