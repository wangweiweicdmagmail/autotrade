# -*- coding: utf-8 -*-
"""账户与 QQQ 日收益对比：归档、净值推算、累计收益与 API 口径。"""
import json
import time
from datetime import date, datetime
from pathlib import Path

import pytest

from engine import returns_comparison as rc
from .conftest import run


def _ts(day, hour=16):
    return datetime.fromisoformat(day).replace(hour=hour, tzinfo=rc.ET).timestamp()


def test_parse_range_defaults_and_validation():
    today = date(2026, 10, 31)
    assert rc.parse_range("", "", today) == (date(2026, 10, 2), today)
    assert rc.parse_range("2026-10-01", "2026-10-03") == (date(2026, 10, 1), date(2026, 10, 3))
    for start, end in (("2026/10/01", ""), ("2026-10-03", "2026-10-01")):
        with pytest.raises(ValueError):
            rc.parse_range(start, end, today)


def test_load_account_daily_dedup_and_anchors(tmp_path):
    first = {"lot_id": "a", "state": "CLOSED", "closed_at": _ts("2026-10-20"),
             "realized_pnl": 100}
    duplicate = dict(first, realized_pnl=120)
    second = {"lot_id": "b", "state": "CLOSED", "closed_at": _ts("2026-10-20"),
              "realized_pnl": -20}
    pending = {"lot_id": "c", "state": "PENDING", "closed_at": _ts("2026-10-20"),
               "realized_pnl": 999}
    (tmp_path / "trades-20261020.json").write_text(json.dumps([first, second, pending]))
    (tmp_path / "trades-20261021.json").write_text(json.dumps([duplicate]))
    (tmp_path / "equity_sod-20261020.json").write_text(json.dumps({"equity": 10000}))
    (tmp_path / "equity_daily.json").write_text(json.dumps({"anchors": {"2026-10-21": 10100}}))

    pnl, anchors = rc.load_account_daily(tmp_path)
    assert pnl == {"2026-10-20": 100.0}
    assert anchors == {"2026-10-20": 10000.0, "2026-10-21": 10100.0}


def test_account_start_equity_forward_backward_and_missing():
    pnl = {"2026-10-20": 100, "2026-10-21": -30}
    anchors = {"2026-10-20": 10000, "2026-10-23": 11000}
    assert rc.account_start_equity("2026-10-20", pnl, anchors) == (10000, False)
    assert rc.account_start_equity("2026-10-21", pnl, anchors) == (10100, True)
    assert rc.account_start_equity("2026-10-22", pnl, anchors) == (11000, True)
    assert rc.account_start_equity("2026-10-19", pnl, anchors) == (10000, True)
    assert rc.account_start_equity("2026-10-19", {**pnl, "2026-10-19": 50}, anchors) == (9950, True)
    assert rc.account_start_equity("2026-10-20", pnl, {}) == (None, False)


def test_build_rows_uses_previous_qqq_close_and_compounds():
    rows, summary = rc.build_rows(
        date(2026, 10, 20), date(2026, 10, 22),
        {"2026-10-20": 100, "2026-10-21": -50},
        {"2026-10-20": 10000},
        {"2026-10-19": 100, "2026-10-20": 102,
         "2026-10-21": 101, "2026-10-22": 103})
    assert [r["date"] for r in rows] == ["2026-10-20", "2026-10-21", "2026-10-22"]
    assert rows[0]["account_return"] == pytest.approx(0.01)
    assert rows[0]["qqq_return"] == pytest.approx(0.02)
    assert rows[1]["account_start_equity"] == 10100
    assert rows[1]["account_estimated"] is True
    assert rows[2]["account_pnl"] == 0
    assert rows[-1]["account_cumulative"] == pytest.approx(1.01 * (1 - 50 / 10100) - 1)
    assert rows[-1]["qqq_cumulative"] == pytest.approx(0.03)
    assert summary["account_pnl"] == 50
    assert summary["qqq_return"] == pytest.approx(0.03)


def test_save_equity_anchor_is_persistent(tmp_path):
    rc.save_equity_anchor(tmp_path, "2026-10-20", 10000.126)
    rc.save_equity_anchor(tmp_path, "2026-10-21", 10100)
    payload = json.loads((tmp_path / "equity_daily.json").read_text())
    assert payload["anchors"] == {"2026-10-20": 10000.13, "2026-10-21": 10100}


def test_api_returns_comparison(console_app, monkeypatch):
    app = console_app
    monkeypatch.setattr(rc, "load_account_daily",
                        lambda _p: ({"2026-10-20": 100}, {"2026-10-20": 10000}))
    monkeypatch.setattr(rc, "load_qqq_daily",
                        lambda _p: ({"2026-10-19": 100, "2026-10-20": 102}, time.time()))
    monkeypatch.setattr(app.gateway, "_daily_pnl_session_active", lambda _now: True)

    async def status():
        return {"equity": 10200, "daily_pnl": 100}
    monkeypatch.setattr(app, "get_status_cached", status)
    saved = []
    monkeypatch.setattr(rc, "save_equity_anchor", lambda _p, d, e: saved.append((d, e)))

    result = run(app.api_returns_compare("2026-10-20", "2026-10-20"))
    assert result["rows"][0]["account_return"] == pytest.approx(0.01)
    assert result["rows"][0]["qqq_return"] == pytest.approx(0.02)
    assert result["summary"]["excess_return"] == pytest.approx(-0.01)
    assert result["account_basis"] == "realized_pnl"
    assert saved == [(datetime.now(rc.ET).date().isoformat(), 10100)]


def test_api_rejects_invalid_range_before_io(console_app, monkeypatch):
    async def must_not_query():
        raise AssertionError("非法日期不应查询账户状态")
    monkeypatch.setattr(console_app, "get_status_cached", must_not_query)
    with pytest.raises(console_app.HTTPException) as exc:
        run(console_app.api_returns_compare("2026-10-21", "2026-10-20"))
    assert exc.value.status_code == 400
