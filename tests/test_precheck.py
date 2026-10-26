# -*- coding: utf-8 -*-
"""盘前检查单（console/app.py run_precheck + 调度 + API）回归。

守的口径：七个检查项的红/绿/中性/跳过判定（数据流覆盖率+新鲜度、盘前
注入 09:30 前中性、IBKR 连接+净值、风险预算余量、跟单进程、时钟偏差、
昨日遗留告警）；调度时段判定（周末不跑/09:25 触发/当天只一次/有红项走
notifier critical）；API 手动跑 + 最近一次落盘。全 mock，不落 logs/。
"""
import json
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from engine.notifier import Notifier
from .conftest import run

ET = ZoneInfo("America/New_York")


class FakeCollector:
    def __init__(self, market, latest_bar_time):
        self._market = market
        self.latest_bar_time = latest_bar_time

    def latest(self):
        return self._market


class FakeGateway:
    def __init__(self, connected=True, ibkr_time=0.0):
        self._connected = connected
        self.ibkr_time = ibkr_time


class FakeOrderMgr:
    def __init__(self, total=0.0):
        self._total = total

    def risk_exposure(self):
        return {"total": self._total}


class FakeSupervisor:
    def __init__(self, alive=True):
        self._alive = alive

    def process_alive(self, name):
        return self._alive


@pytest.fixture
def app(console_app, monkeypatch, tmp_path):
    """全绿基线：数据流全+新鲜、attack 全注入、IBKR 连上净值可取、
    敞口 0、无跟单服务器、时钟偏差 0、无昨日日志。"""
    a = console_app
    market = {s: {"attack_c": {"pmh": 100.0, "pml": 98.0}}
              for s in a.config.WATCHLIST}
    monkeypatch.setattr(a, "collector",
                        FakeCollector(market, time.time()))
    monkeypatch.setattr(a, "gateway", FakeGateway(True, time.time()))
    monkeypatch.setattr(a, "order_mgr", FakeOrderMgr(0.0))
    monkeypatch.setattr(a, "copy_supervisor", FakeSupervisor(True))
    monkeypatch.setattr(a, "_load_copy_servers", lambda: [])
    monkeypatch.setattr(a, "_PRECHECK_DIR", tmp_path)
    async def fake_status():
        return {"equity": 100000.0, "daily_pnl": 0.0, "ts": time.time()}
    monkeypatch.setattr(a, "get_status_cached", fake_status)
    n = Notifier(record_event=lambda *_, **k: None,
                 imessage_recipient=lambda: "")
    monkeypatch.setattr(a, "notifier", n)
    return a


def test_all_green(app):
    """全绿路径：overall=ok，七项全 ok/neutral/skip。"""
    rep = run(app.run_precheck())
    assert rep["overall"] == "ok"
    by = {i["key"]: i for i in rep["items"]}
    assert by["data_flow"]["status"] == "ok"
    assert by["ibkr"]["status"] == "ok"
    assert by["risk_budget"]["status"] == "ok"
    assert by["copy"]["status"] == "skip"
    assert by["clock"]["status"] == "ok"
    assert by["leftover"]["status"] == "neutral"   # 无昨日日志
    # 周日：盘前注入项 = 中性「待注入」（09:30 前口径）
    assert by["attack_zone"]["status"] in ("neutral", "ok")


def test_data_flow_red(app, monkeypatch):
    """数据流红：标的缺失 / bar 陈旧。"""
    monkeypatch.setattr(app.collector, "_market",
                        {"NASDAQ:NVDA": {"attack_c": {"pmh": 1, "pml": 0.5}}})
    rep = run(app.run_precheck())
    by = {i["key"]: i for i in rep["items"]}
    assert by["data_flow"]["status"] == "bad" and "缺" in by["data_flow"]["detail"]
    assert rep["overall"] == "bad"
    # bar 陈旧（>10 分钟）
    monkeypatch.setattr(app.collector, "_market",
                        {s: {"attack_c": {"pmh": 100.0, "pml": 98.0}}
                         for s in app.config.WATCHLIST})
    monkeypatch.setattr(app.collector, "latest_bar_time", time.time() - 700)
    rep = run(app.run_precheck())
    by = {i["key"]: i for i in rep["items"]}
    assert by["data_flow"]["status"] == "bad" and "陈旧" in by["data_flow"]["detail"]


def test_ibkr_red(app, monkeypatch):
    """IBKR 红：未连接 / 净值取不到（fail-open 标注）。"""
    monkeypatch.setattr(app, "gateway", FakeGateway(False))
    rep = run(app.run_precheck())
    by = {i["key"]: i for i in rep["items"]}
    assert by["ibkr"]["status"] == "bad"
    assert by["clock"]["status"] == "skip"       # 未连接时钟比对跳过

    async def no_equity():
        return {"equity": None, "daily_pnl": None, "ts": time.time()}
    monkeypatch.setattr(app, "gateway", FakeGateway(True, time.time()))
    monkeypatch.setattr(app, "get_status_cached", no_equity)
    rep = run(app.run_precheck())
    by = {i["key"]: i for i in rep["items"]}
    assert by["ibkr"]["status"] == "bad" and "fail-open" in by["ibkr"]["detail"]


def test_risk_budget_and_clock(app, monkeypatch):
    """风险预算：超预算红；时钟：偏差 >30s 红。"""
    monkeypatch.setattr(app, "order_mgr",
                        FakeOrderMgr(app.config.RISK_MAX_TOTAL_RISK_USD + 1))
    rep = run(app.run_precheck())
    by = {i["key"]: i for i in rep["items"]}
    assert by["risk_budget"]["status"] == "bad" and "超预算" in by["risk_budget"]["detail"]

    monkeypatch.setattr(app, "order_mgr", FakeOrderMgr(0.0))
    monkeypatch.setattr(app, "gateway", FakeGateway(True, time.time() - 60))
    rep = run(app.run_precheck())
    by = {i["key"]: i for i in rep["items"]}
    assert by["clock"]["status"] == "bad" and "偏差" in by["clock"]["detail"]


def test_copy_servers(app, monkeypatch):
    """跟单进程：enabled 但进程死 → 红；无启用 → skip。"""
    monkeypatch.setattr(app, "_load_copy_servers",
                        lambda: [{"name": "A", "enabled": True}])
    monkeypatch.setattr(app, "copy_supervisor", FakeSupervisor(False))
    rep = run(app.run_precheck())
    by = {i["key"]: i for i in rep["items"]}
    assert by["copy"]["status"] == "bad" and "进程不在" in by["copy"]["detail"]
    monkeypatch.setattr(app, "copy_supervisor", FakeSupervisor(True))
    rep = run(app.run_precheck())
    by = {i["key"]: i for i in rep["items"]}
    assert by["copy"]["status"] == "ok"


def test_leftover_alerts(app, tmp_path):
    """昨日遗留：有未恢复 critical → 红；全部已恢复 → 绿；无日志 → 中性。"""
    yday = (datetime.now(ET).date()).strftime("%Y%m%d")
    # 用「昨天」日期写事件日志（tmp 目录）
    import datetime as _dt
    y = (_dt.datetime.now(ET) - _dt.timedelta(days=1)).strftime("%Y%m%d")
    ev = tmp_path / f"events-{y}.jsonl"
    ev.write_text("\n".join([
        json.dumps({"type": "alert", "key": "tws_down", "level": "critical",
                    "title": "TWS 断线", "msg": "x"}),
        json.dumps({"type": "alert", "key": "data_stale", "level": "warning",
                    "title": "数据陈旧", "msg": "x"}),
        json.dumps({"type": "alert_recovered", "key": "data_stale"}),
    ]) + "\n", encoding="utf-8")
    rep = run(app.run_precheck())
    by = {i["key"]: i for i in rep["items"]}
    assert by["leftover"]["status"] == "bad" and "TWS 断线" in by["leftover"]["detail"]
    # 补上恢复记录 → 绿
    with open(ev, "a", encoding="utf-8") as f:
        f.write(json.dumps({"type": "alert_recovered", "key": "tws_down"}) + "\n")
    rep = run(app.run_precheck())
    by = {i["key"]: i for i in rep["items"]}
    assert by["leftover"]["status"] == "ok"


def test_schedule_timing(app):
    """调度：周末不跑；09:24 不跑；09:25 触发且当天只一次；有红项走
    notifier critical；落盘 tmp。"""
    done = set()
    sat = datetime(2026, 10, 24, 9, 30, tzinfo=ET)    # 周六
    assert run(app._precheck_check_once(sat, done)) is False
    fri_early = datetime(2026, 10, 23, 9, 24, tzinfo=ET)
    assert run(app._precheck_check_once(fri_early, done)) is False
    fri = datetime(2026, 10, 23, 9, 25, tzinfo=ET)
    assert run(app._precheck_check_once(fri, done)) is True
    path = app._PRECHECK_DIR / "precheck-20261023.json"
    assert path.exists()
    rep = json.loads(path.read_text(encoding="utf-8"))
    assert rep["overall"] == "ok"                    # 全绿基线
    assert not app.notifier.is_active("precheck:2026-10-23")   # 无红不告警
    assert run(app._precheck_check_once(fri, done)) is False   # 当天只一次


def test_schedule_red_alert(app, monkeypatch):
    """调度：有红项 → notifier critical（含红项摘要）。"""
    monkeypatch.setattr(app.collector, "_market", {})   # 数据流全缺 → 红
    done = set()
    fri = datetime(2026, 10, 23, 9, 25, tzinfo=ET)
    assert run(app._precheck_check_once(fri, done)) is True
    a = app.notifier._active.get("precheck:2026-10-23")
    assert a and a["level"] == "critical" and "数据流" in a["msg"]


def test_api(app, monkeypatch):
    """API：手动跑返回结构；last 无记录 404、有记录返回最近一份。"""
    rep = run(app.api_precheck())
    assert rep["overall"] == "ok" and len(rep["items"]) == 7
    with pytest.raises(app.HTTPException) as ei:
        run(app.api_precheck_last())
    assert ei.value.status_code == 404
    (app._PRECHECK_DIR / "precheck-20261023.json").write_text(
        json.dumps(rep), encoding="utf-8")
    last = run(app.api_precheck_last())
    assert last["overall"] == "ok"
