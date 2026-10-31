# -*- coding: utf-8 -*-
"""EMA9 自动策略入场线与研究库迁移。"""
import sqlite3

import pytest

import research_db
from engine.strategies import build_strategy


def _ctx(direction, ema_9):
    return {
        "ts_c": {
            "direction": direction,
            "atr": 2.0,
            "ema_fast": 100.0,
            "ema_9": ema_9,
            "channel_edge": 90.0 if direction == 1 else 110.0,
        },
        "fvg_c": {},
    }


def test_long_entry_can_trigger_on_ema9_only():
    st = build_strategy("trend_pullback", use_fvg_line=False)
    plan = st.entry_trigger(_ctx(1, 105.0), "NASDAQ:NVDA", 104.0)
    assert plan is not None
    assert plan.side == "long" and plan.line == "ema9"
    assert plan.limit_px == 105.0 and plan.stop_px == 103.0


def test_short_entry_can_trigger_on_ema9_only():
    st = build_strategy("trend_pullback", use_fvg_line=False)
    plan = st.entry_trigger(_ctx(-1, 95.0), "NASDAQ:NVDA", 96.0)
    assert plan is not None
    assert plan.side == "short" and plan.line == "ema9"
    assert plan.limit_px == 95.0 and plan.stop_px == 97.0


def test_old_research_db_is_migrated_and_roundtrips_ema9(tmp_path):
    db = tmp_path / "research.db"
    with sqlite3.connect(db) as conn:
        conn.executescript(research_db._SCHEMA.replace("ema_9 REAL, ", ""))
    research_db.init(db)
    columns = {row[1] for row in sqlite3.connect(db).execute("PRAGMA table_info(bars)")}
    assert "ema_9" in columns

    row = {"time": 1793367000, "open": 1, "high": 2, "low": 0.5, "close": 1.5,
           "volume": 100, "ts": {"ema_9": 1.25}, "fvg": {}}
    assert research_db.upsert_bars("NASDAQ:QQQ", [row], db) == 1
    loaded = research_db.load_day("NASDAQ:QQQ", "2026-10-30", db)
    assert loaded[0]["ema_9"] == pytest.approx(1.25)
