# -*- coding: utf-8 -*-
"""tests/ 全局规矩（对本套件所有用例生效，写新测试前先读这里）
================================================================

1. 状态隔离：所有落盘路径（OrderManager state_path / settings / clients /
   copy_servers / RiskGate halt_dir / 事件日志 record_event）一律指向
   pytest tmp_path 或 monkeypatch 成内存桩——严禁读写项目 logs/ 真实文件。
   这是实盘真钱系统，8000 控制台正在后台跑，lots_state.json / settings.json
   被测试污染过（教训），绝无下次。
2. hermetic：不连 TV、不连 TWS、不依赖 8000 控制台或任何运行中进程。
   唯一例外：test_breakout_replay 只读 logs/research.db（缺失即 skip，
   绝不写入）。网络/凭证类测试放 tests/live/ 并标 pytest.mark.live。
3. 防护：autouse fixture 在每个用例前后快照 logs/ 文件清单，测试若新建或
   删除 logs/ 下的文件立即 fail（内容追加由 record_event 桩拦截，不在此列——
   生产进程自己也会持续写 logs/，内容级比对会误伤）。
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 提前 import console.app：ib_async → eventkit 在 import 时调
# asyncio.get_event_loop()；一旦有测试先跑过 asyncio.run（主线程事件循环
# 被关闭清空），之后再 import console.app 会 RuntimeError。在 conftest
# 加载期（尚无事件循环操作）完成首次 import，后续全部走 sys.modules 缓存。
import console.app  # noqa: F401,E402

LOGS = ROOT / "logs"


@pytest.fixture(autouse=True)
def _logs_dir_guard():
    """logs/ 防污染护栏：用例前后比对文件清单（只查新建/删除，不查内容——
    生产进程在跑，内容会持续变化）。"""
    before = {p.name for p in LOGS.glob("*")} if LOGS.exists() else set()
    yield
    after = {p.name for p in LOGS.glob("*")} if LOGS.exists() else set()
    created = after - before
    deleted = before - after
    assert not created and not deleted, (
        f"测试污染 logs/：新建 {sorted(created)} 删除 {sorted(deleted)}")


def run(coro):
    """无 pytest-asyncio 环境的异步用例入口：每个 test 独立事件循环。"""
    import asyncio
    return asyncio.run(coro)


@pytest.fixture
def console_app(tmp_path, monkeypatch):
    """console.app 测试基座：所有落盘路径指 tmp_path、record_event 内存桩，
    用例结束后 _settings 内容还原（monkeypatch 负责还原被替换的对象本身，
    dict 原位变更须手工还原）。不触发 startup、不碰 8000 端口。"""
    import console.app as app
    monkeypatch.setattr(app, "record_event", lambda *a, **k: None)
    monkeypatch.setattr(app, "_SETTINGS_PATH", tmp_path / "settings.json")
    monkeypatch.setattr(app, "_CLIENTS_PATH", tmp_path / "clients.json")
    monkeypatch.setattr(app, "_COPY_SERVERS_PATH", tmp_path / "copy_servers.json")
    monkeypatch.setattr(app, "_COPY_TRADER_PY", tmp_path / "copy_trader.py")
    settings_backup = dict(app._settings)
    try:
        yield app
    finally:
        app._settings.clear()
        app._settings.update(settings_backup)
