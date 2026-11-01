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

# 提前导入执行依赖，保证 ib_async 的首次导入处于有效主线程事件循环。
# 这不会创建应用服务、加载生产状态或连接外部系统。
from console.runtime.bootstrap import build_services, RuntimePaths
from console.factory import create_app

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
    """每个测试一个应用实例；所有生产状态路径在加载前即指向 tmp_path。"""
    context = build_services(RuntimePaths(data_dir=tmp_path))
    context.events.record_event = lambda *a, **k: None
    context.copy._COPY_TRADER_PY = tmp_path / "copy_trader.py"
    create_app(context)
    try:
        yield context
    finally:
        context.close_log_handler()
