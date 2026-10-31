"""账户与 QQQ 日收益对比。

账户日收益 = 当日平仓归档已实现盈亏 / 当日起始净值。起始净值优先使用
当日净值锚点，其余日期由最近锚点和平仓盈亏推算，因此会标记 estimated。
QQQ 日收益 = 当日常规时段收盘价 / 前一交易日收盘价 - 1。
"""

import asyncio
import json
import math
import sqlite3
import tempfile
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
QQQ_SYMBOL = "NASDAQ:QQQ"


def parse_range(start: str, end: str, today: date | None = None) -> tuple[date, date]:
    today = today or datetime.now(ET).date()
    try:
        end_day = date.fromisoformat(end) if end else today
        start_day = date.fromisoformat(start) if start else end_day - timedelta(days=29)
    except ValueError as exc:
        raise ValueError("日期格式必须为 YYYY-MM-DD") from exc
    if start_day > end_day:
        raise ValueError("起始日期不能晚于结束日期")
    if (end_day - start_day).days > 3660:
        raise ValueError("日期范围不能超过 10 年")
    return start_day, end_day


def _read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return default


def load_account_daily(logs_dir: Path) -> tuple[dict[str, float], dict[str, float]]:
    """读取平仓盈亏和 SOD 净值锚点；重复 lot 只保留最后归档版本。"""
    records = {}
    for path in sorted(logs_dir.glob("trades-*.json")):
        rows = _read_json(path, [])
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict) or row.get("state") != "CLOSED":
                continue
            key = row.get("lot_id") or f"{path.name}:{len(records)}"
            records[key] = row

    daily_pnl = {}
    for row in records.values():
        closed_at = row.get("closed_at")
        pnl = row.get("realized_pnl")
        if not closed_at or not isinstance(pnl, (int, float)) or not math.isfinite(pnl):
            continue
        day = datetime.fromtimestamp(closed_at, ET).date().isoformat()
        daily_pnl[day] = daily_pnl.get(day, 0.0) + float(pnl)

    anchors = {}
    for path in sorted(logs_dir.glob("equity_sod-*.json")):
        try:
            day = datetime.strptime(path.stem.removeprefix("equity_sod-"), "%Y%m%d").date().isoformat()
        except ValueError:
            continue
        payload = _read_json(path, {})
        equity = payload.get("equity") if isinstance(payload, dict) else None
        if isinstance(equity, (int, float)) and equity > 0:
            anchors[day] = float(equity)

    saved = _read_json(logs_dir / "equity_daily.json", {})
    saved_anchors = saved.get("anchors", {}) if isinstance(saved, dict) else {}
    for day, equity in saved_anchors.items():
        try:
            date.fromisoformat(day)
        except ValueError:
            continue
        if isinstance(equity, (int, float)) and equity > 0:
            anchors[day] = float(equity)
    return daily_pnl, anchors


def save_equity_anchor(logs_dir: Path, day: str, equity: float) -> None:
    """保存当日 SOD 净值，供以后查询不再依赖推算。"""
    if equity <= 0:
        return
    date.fromisoformat(day)
    path = logs_dir / "equity_daily.json"
    payload = _read_json(path, {})
    anchors = payload.get("anchors", {}) if isinstance(payload, dict) else {}
    if anchors.get(day) == equity:
        return
    anchors[day] = round(float(equity), 2)
    logs_dir.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"anchors": anchors}, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    tmp.replace(path)


def _between(daily_pnl: dict[str, float], start: str, end: str) -> float:
    return sum(pnl for day, pnl in daily_pnl.items() if start <= day < end)


def account_start_equity(day: str, daily_pnl: dict[str, float],
                         anchors: dict[str, float]) -> tuple[float | None, bool]:
    """返回 (日起始净值, 是否推算)；锚点之间不计入起止日本身。"""
    if day in anchors:
        return anchors[day], False
    if not anchors:
        return None, False
    anchor_day = min(anchors, key=lambda d: abs((date.fromisoformat(d) - date.fromisoformat(day)).days))
    equity = anchors[anchor_day]
    if anchor_day < day:
        equity += _between(daily_pnl, anchor_day, day)
    else:
        equity -= _between(daily_pnl, day, anchor_day)
    return (equity if equity > 0 else None), True


def load_qqq_daily(logs_dir: Path) -> tuple[dict[str, float], float]:
    """合并研究库 5M 常规时段收盘价与已缓存的 TradingView 日线。"""
    closes = {}
    db_path = logs_dir / "research.db"
    if db_path.exists():
        try:
            with sqlite3.connect(db_path) as conn:
                rows = conn.execute(
                    "SELECT time, close FROM bars WHERE symbol=? ORDER BY time",
                    (QQQ_SYMBOL,)).fetchall()
            regular = {}
            for ts, close in rows:
                if close is None:
                    continue
                dt = datetime.fromtimestamp(ts, ET)
                hm = dt.hour * 60 + dt.minute
                if 9 * 60 + 30 <= hm < 16 * 60:
                    regular[dt.date().isoformat()] = float(close)
            closes.update(regular)
        except (OSError, sqlite3.Error):
            pass

    cache = _read_json(logs_dir / "qqq_daily.json", {})
    bars = cache.get("bars", []) if isinstance(cache, dict) else []
    for row in bars:
        try:
            day = datetime.fromtimestamp(float(row["time"]), ET).date().isoformat()
            close = float(row["close"])
        except (KeyError, TypeError, ValueError, OSError):
            continue
        if close > 0:
            closes[day] = close
    fetched_at = cache.get("fetched_at", 0.0) if isinstance(cache, dict) else 0.0
    return closes, float(fetched_at or 0.0)


def save_qqq_daily(logs_dir: Path, bars: list) -> None:
    path = logs_dir / "qqq_daily.json"
    payload = {"fetched_at": time.time(), "bars": bars}
    logs_dir.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    tmp.replace(path)


async def fetch_qqq_daily(project_root: Path, range_days: int) -> list:
    """从 TradingView 拉 QQQ 日线；空指标表避免加载四个策略 Study。"""
    service_dir = project_root / "service"
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", encoding="utf-8") as f:
        json.dump([], f)
        f.flush()
        proc = await asyncio.create_subprocess_exec(
            "node", str(service_dir / "tv_cloud.js"),
            "--symbols", QQQ_SYMBOL, "--timeframe", "1D",
            "--range", str(max(30, min(5000, range_days + 20))),
            "--session", "regular", "--indicators", f.name,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            cwd=str(service_dir))
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60)
    if proc.returncode != 0:
        raise RuntimeError(stderr.decode("utf-8", "ignore")[-300:] or "QQQ 日线拉取失败")
    try:
        payload = json.loads(stdout)
        bars = payload["symbols"][QQQ_SYMBOL]["bars"]
    except (ValueError, KeyError, TypeError) as exc:
        raise RuntimeError("QQQ 日线返回格式无效") from exc
    if not bars:
        reason = payload.get("symbols", {}).get(QQQ_SYMBOL, {}).get("reason", "无数据")
        raise RuntimeError(f"QQQ 日线为空：{reason}")
    return [{"time": row["time"], "close": row["close"]} for row in bars
            if isinstance(row.get("close"), (int, float)) and row["close"] > 0]


def build_rows(start_day: date, end_day: date, daily_pnl: dict[str, float],
               anchors: dict[str, float], qqq_closes: dict[str, float]) -> tuple[list, dict]:
    first_account_day = min(daily_pnl) if daily_pnl else None
    days = sorted({day for day in set(daily_pnl) | set(qqq_closes)
                   if start_day.isoformat() <= day <= end_day.isoformat()})
    rows = []
    account_factor = 1.0
    qqq_factor = 1.0
    account_started = False
    qqq_started = False
    prior_days = [day for day in qqq_closes if day < start_day.isoformat()]
    previous_qqq = qqq_closes[max(prior_days)] if prior_days else None
    for day in days:
        pnl = daily_pnl.get(day, 0.0) if first_account_day and day >= first_account_day else None
        start_equity, estimated = account_start_equity(day, daily_pnl, anchors)
        account_return = pnl / start_equity if pnl is not None and start_equity else None
        close = qqq_closes.get(day)
        qqq_return = close / previous_qqq - 1 if close is not None and previous_qqq else None
        if account_return is not None:
            account_factor *= 1 + account_return
            account_started = True
        if qqq_return is not None:
            qqq_factor *= 1 + qqq_return
            qqq_started = True
        rows.append({
            "date": day,
            "account_pnl": round(pnl, 2) if pnl is not None else None,
            "account_start_equity": round(start_equity, 2) if start_equity else None,
            "account_return": account_return,
            "account_estimated": estimated if start_equity else False,
            "qqq_close": close,
            "qqq_return": qqq_return,
            "account_cumulative": account_factor - 1 if account_started else None,
            "qqq_cumulative": qqq_factor - 1 if qqq_started else None,
        })
        if close is not None:
            previous_qqq = close
    account_total = account_factor - 1 if account_started else None
    qqq_total = qqq_factor - 1 if qqq_started else None
    summary = {
        "account_return": account_total,
        "qqq_return": qqq_total,
        "excess_return": account_total - qqq_total if account_total is not None and qqq_total is not None else None,
        "account_pnl": round(sum(row["account_pnl"] or 0 for row in rows), 2),
        "days": len(rows),
    }
    return rows, summary
