"""研究数据库（research_db.py）
==============================
本地缓存每根 5M K线 + 全部指标值，供离线研究读取（不再依赖 TV 在线）。

存储：SQLite（默认 logs/research.db），表 bars 主键 (symbol, time)，
幂等 upsert（重复缓存同一天是安全的）。

字段（全部拍平）：time/open/high/low/close/volume
  + trend_state 指标：trend_state/direction/ema_fast/ema_slow/supertrend/
    channel_edge/atr/divergence/st_duration_bars/st_push_count
  + smart_stop 指标：smart_stop_long/smart_stop_short
  + FVG 指标：fvg_cum_count/fvg_last_top/fvg_last_bottom/fvg_event/fvg_last_dir

用法：
    from research_db import upsert_bars, load_day, symbols
    upsert_bars("NASDAQ:NVDA", dump_rows)          # rows 来自 dump_trend_state.js
    bars = load_day("NASDAQ:NVDA", "2026-10-23")   # 按美东交易日取
"""

import json
import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

DEFAULT_DB = Path(__file__).resolve().parent / "logs" / "research.db"
ET = ZoneInfo("America/New_York")

# ts/fvg 子对象字段 → 列名映射（列名 = 原字段名）
_TS_FIELDS = ("trend_state", "direction", "ema_fast", "ema_slow", "supertrend",
              "channel_edge", "atr", "divergence", "st_duration_bars",
              "st_push_count", "smart_stop_long", "smart_stop_short")
_FVG_FIELDS = ("fvg_cum_count", "fvg_last_top", "fvg_last_bottom",
               "fvg_event", "fvg_last_dir")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS bars (
  symbol TEXT NOT NULL,
  time   INTEGER NOT NULL,          -- bar 起点 epoch 秒（UTC）
  open REAL, high REAL, low REAL, close REAL, volume REAL,
  trend_state INTEGER, direction INTEGER,
  ema_fast REAL, ema_slow REAL, supertrend REAL, channel_edge REAL,
  atr REAL, divergence REAL,
  st_duration_bars INTEGER, st_push_count INTEGER,
  smart_stop_long REAL, smart_stop_short REAL,
  fvg_cum_count INTEGER, fvg_last_top REAL, fvg_last_bottom REAL,
  fvg_event INTEGER, fvg_last_dir INTEGER,
  cached_at REAL NOT NULL,
  PRIMARY KEY (symbol, time)
);
CREATE INDEX IF NOT EXISTS idx_bars_time ON bars(time);
"""


def init(db_path: Path = DEFAULT_DB) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.executescript(_SCHEMA)


def upsert_bars(symbol: str, rows: list, db_path: Path = DEFAULT_DB) -> int:
    """把 dump_trend_state.js 的行结构 upsert 进库。返回写入行数。"""
    init(db_path)
    now = time.time()
    recs = []
    for r in rows:
        ts = r.get("ts") or {}
        fvg = r.get("fvg") or {}
        recs.append((
            symbol, int(r["time"]),
            r.get("open"), r.get("high"), r.get("low"),
            r.get("close"), r.get("volume"),
            *(ts.get(f) for f in _TS_FIELDS),
            *(fvg.get(f) for f in _FVG_FIELDS),
            now,
        ))
    cols = ("symbol,time,open,high,low,close,volume,"
            + ",".join(_TS_FIELDS) + "," + ",".join(_FVG_FIELDS)
            + ",cached_at")
    ph = ",".join("?" for _ in recs[0]) if recs else ""
    with sqlite3.connect(db_path) as conn:
        conn.executemany(
            f"INSERT OR REPLACE INTO bars ({cols}) VALUES ({ph})", recs)
    return len(recs)


def _rows_to_dicts(cur) -> list:
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def load_bars(symbol: str, start_ts: int, end_ts: int,
              db_path: Path = DEFAULT_DB) -> list:
    """按 epoch 区间读（[start_ts, end_ts)，时间正序）。"""
    with sqlite3.connect(db_path) as conn:
        cur = conn.execute(
            "SELECT * FROM bars WHERE symbol=? AND time>=? AND time<? "
            "ORDER BY time", (symbol, start_ts, end_ts))
        return _rows_to_dicts(cur)


def load_day(symbol: str, day: str, db_path: Path = DEFAULT_DB) -> list:
    """按美东交易日读全天（含盘前盘后）：day 形如 2026-10-23。"""
    d0 = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=ET)
    d1 = d0 + timedelta(days=1)
    return load_bars(symbol, int(d0.timestamp()), int(d1.timestamp()), db_path)


def symbols(db_path: Path = DEFAULT_DB) -> list:
    with sqlite3.connect(db_path) as conn:
        return [r[0] for r in conn.execute(
            "SELECT DISTINCT symbol FROM bars ORDER BY symbol")]


def day_summary(db_path: Path = DEFAULT_DB) -> list:
    """库内容概览：每标的的 bar 数与首日/末日（研究前先摸清库存）。"""
    with sqlite3.connect(db_path) as conn:
        cur = conn.execute(
            "SELECT symbol, COUNT(*), MIN(time), MAX(time) FROM bars "
            "GROUP BY symbol ORDER BY symbol")
        out = []
        for sym, n, t0, t1 in cur.fetchall():
            out.append({
                "symbol": sym, "bars": n,
                "first": datetime.fromtimestamp(t0, ET).strftime("%Y-%m-%d %H:%M"),
                "last": datetime.fromtimestamp(t1, ET).strftime("%Y-%m-%d %H:%M"),
            })
        return out


if __name__ == "__main__":
    print(json.dumps(day_summary(), ensure_ascii=False, indent=1))
