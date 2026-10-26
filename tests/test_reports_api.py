# -*- coding: utf-8 -*-
"""盘前报告工作流 API 回归（console/app.py /api/reports*）。

守的口径：历史列表（日期倒序 + 文件头元信息解析）；报告读取的日期格式
校验与路径穿越防护；生成异步化（立即返回 running、在跑 409、冷却 429）、
完成落 last_result + record_event("news_report_generated")。生成链路的
采集/LLM/落盘全 monkeypatch（不真跑 2 分钟采集），报告文件写 tmp_path。
"""
import time

import pytest

from .conftest import run

HEAD = """# 盘前交易分析报告 2026-10-23

新闻窗口：10-22 16:00 ~ 10-23 09:30 ET
条目：大盘 11 条 / 自选股 49 票 238 条
模型：deepseek/deepseek-v4-flash · 耗时 16.2s · tokens 1234

---

## 一、隔夜市场综述
多。
"""

HEAD_DEGRADED = HEAD.replace(
    "模型：deepseek/deepseek-v4-flash · 耗时 16.2s · tokens 1234",
    "⚠️ LLM 降级（no key）：纯标题汇编")


@pytest.fixture
def app(console_app, monkeypatch, tmp_path):
    """报告目录指 tmp_path；生成状态/冷却计数器归零并还原。"""
    reports = tmp_path / "reports"
    reports.mkdir()
    monkeypatch.setattr(console_app, "_REPORTS_DIR", reports)
    monkeypatch.setattr(console_app, "_report_state",
                        {"running": False, "started_at": 0.0,
                         "last_result": None, "last_error": None})
    monkeypatch.setattr(console_app, "_report_last_done", 0.0)
    return console_app


def _write(app, date, text=HEAD):
    (app._REPORTS_DIR / f"premarket-{date}.md").write_text(text,
                                                           encoding="utf-8")


def test_list_reports_meta_and_order(app):
    """历史列表：日期倒序 + 文件头元信息（条目数/模型/耗时/tokens/degraded）。"""
    _write(app, "20261023", HEAD)
    _write(app, "20261024", HEAD_DEGRADED.replace("2026-10-23", "2026-10-24"))
    rows = run(app.api_reports_list())["reports"]
    assert [r["date"] for r in rows] == ["20261024", "20261023"]
    r23 = next(r for r in rows if r["date"] == "20261023")
    assert r23["meta"]["market_items"] == 11
    assert r23["meta"]["ticker_count"] == 49
    assert r23["meta"]["ticker_items"] == 238
    assert r23["meta"]["model"] == "deepseek/deepseek-v4-flash"
    assert r23["meta"]["tokens"] == "1234"
    assert r23["meta"]["degraded"] is False
    r24 = next(r for r in rows if r["date"] == "20261024")
    assert r24["meta"]["degraded"] is True
    assert "model" not in r24["meta"]


def test_get_report_validation_and_traversal(app):
    """读取：格式校验（非 8 位数字 400）、不存在 404、路径穿越无路可走。"""
    _write(app, "20261023")
    r = run(app.api_reports_get("20261023"))
    assert r["date"] == "20261023" and "## 一、隔夜市场综述" in r["markdown"]
    assert r["meta"]["market_items"] == 11
    for bad in ("2026-10-23", "2026102A", "2023", "", "..%2F..%2Fetc"):
        with pytest.raises(app.HTTPException) as ei:
            run(app.api_reports_get(bad))
        assert ei.value.status_code == 400, bad
    with pytest.raises(app.HTTPException) as ei:
        run(app.api_reports_get("20261022"))   # 合法格式但无文件
    assert ei.value.status_code == 404
    # _report_file 纯函数层：穿越串直接 None
    assert app._report_file("../../etc/passwd") is None
    assert app._report_file("20261023") is not None


def test_generate_async_409_429(app, monkeypatch):
    """生成：立即返回 running；在跑 409；冷却 429；完成落 last_result+事件。"""
    events = []
    monkeypatch.setattr(app, "record_event",
                        lambda t, **d: events.append({"type": t, **d}))
    import engine.news_digest as nd
    monkeypatch.setattr(nd, "collect_market", lambda **kw: [])
    monkeypatch.setattr(nd, "collect_tickers", lambda *a, **kw: {})
    monkeypatch.setattr(nd, "generate_report",
                        lambda day, m, tn, videos=None: ("## 一、隔夜市场综述\nx",
                                            {"model": "mock/test",
                                             "usage": {"total_tokens": 10},
                                             "latency_s": 0.1,
                                             "degraded": False}))
    def fake_save(day, md, m, tn, meta, videos=None):
        p = app._REPORTS_DIR / f"premarket-{day.replace('-', '')}.md"
        p.write_text(md, encoding="utf-8")
        return p

    monkeypatch.setattr(nd, "save_report", fake_save)

    async def go():
        r = await app.api_reports_generate(trader="master")
        assert r["ok"] and r["running"] is True
        # 在跑 → 409
        with pytest.raises(app.HTTPException) as ei:
            await app.api_reports_generate(trader="master")
        assert ei.value.status_code == 409
        # status 进度态
        st = await app.api_reports_status()
        assert st["running"] is True and st["elapsed_s"] is not None
        await app._report_task
        st = await app.api_reports_status()
        assert st["running"] is False
        assert st["last_result"]["degraded"] is False
        assert st["last_result"]["model"] == "mock/test"
        assert st["last_result"]["tokens"] == 10
        assert any(e["type"] == "news_report_generated" for e in events)
        # 冷却 → 429
        with pytest.raises(app.HTTPException) as ei:
            await app.api_reports_generate(trader="master")
        assert ei.value.status_code == 429
        # 生成落盘后列表能查到
        rows = (await app.api_reports_list())["reports"]
        assert rows and rows[0]["date"] == st["last_result"]["date"]

    run(go())


def test_generate_failure_records_error(app, monkeypatch):
    """生成失败：last_error 落状态 + record_event(news_report_failed)。"""
    events = []
    monkeypatch.setattr(app, "record_event",
                        lambda t, **d: events.append({"type": t, **d}))
    import engine.news_digest as nd

    def boom(**kw):
        raise OSError("network down")

    monkeypatch.setattr(nd, "collect_market", boom)

    async def go():
        await app.api_reports_generate(trader="master")
        await app._report_task
        st = await app.api_reports_status()
        assert st["running"] is False
        assert "network down" in st["last_error"]
        assert st["last_result"] is None
        assert any(e["type"] == "news_report_failed" for e in events)

    run(go())
