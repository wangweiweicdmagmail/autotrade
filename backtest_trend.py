# -*- coding: utf-8 -*-
"""
trend_state 历史回测 (backtest_trend.py)
=========================================

把某一天（如昨天）的 5M bar + trend_state 指标值逐根喂给回测引擎
（engine/shadow_tracker.ShadowTracker，与实时影子跟踪同一套规则），
产生每一次入场信号的统计报告。

用法：
    python3 backtest_trend.py --symbol NASDAQ:NVDA --day 2026-10-16
    python3 backtest_trend.py --symbol NASDAQ:NVDA          # 默认上个交易日
    python3 backtest_trend.py --symbol NASDAQ:NVDA --json   # 输出 JSON

规则（与实时影子一致）：
- 事件：确认 bar 上 ema_fast / channel_edge 任一条线被刺穿收回（影线跨线）
  且 ts_c.trend_state 同向（1 多 / -1 空），入场窗口 9:30–15:50 ET；
- 入场 = 线值，止损 = 入场 ∓ 1×ATR，1R = 1×ATR；
- 逐 bar 推演：high/low 判止损与 MFE，15:55 ET bar 收盘了结；
- 统计：止损前/收盘前最大盈亏比的 ≥1R..≥5R 分布、未达 1R 直接止损占比。
"""

import argparse
import asyncio
import config
import json
import logging
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))

from engine.shadow_tracker import (ENTRY_END_HM, ENTRY_START_HM, SETTLE_HM,
                                   ShadowTracker)

logger = logging.getLogger("backtest_trend")

ET = ZoneInfo("America/New_York")
PROJECT_ROOT = Path(__file__).resolve().parent
DUMP_JS = PROJECT_ROOT / "service" / "dump_trend_state.js"


def prev_trading_day(day: datetime) -> datetime:
    """上一个工作日（周一~周五）。"""
    d = day - timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


async def fetch_history(symbol: str, bars: int = 500,
                        day: str = "") -> list:
    """调 dump_trend_state.js 拉取 5M bar + trend_state（正序）。
    指定 day（YYYY-MM-DD）时以该日 16:00 ET 为锚点往前拉 bars 根——
    回测历史日期不从最新收盘倒推，且指标 warmup 锚定在目标日
    （与当日实盘所见一致）。"""
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        out_path = f.name
    cmd = ["node", str(DUMP_JS), "--symbols", symbol,
           "--range", str(bars), "--out", out_path,
           "--default-session-symbols", ",".join(sorted(config.TV_DEFAULT_SESSION_SYMBOLS)),
           "--session", config.TV_MARKET_SESSION]
    if day:
        anchor = datetime.fromisoformat(day).replace(
            hour=16, minute=0, tzinfo=ET)
        cmd += ["--to", str(int(anchor.timestamp()))]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=str(PROJECT_ROOT / "service"),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await asyncio.wait_for(proc.communicate(), timeout=90)
    if proc.returncode != 0:
        raise RuntimeError(f"数据导出失败: {stderr.decode('utf-8', 'ignore')[-300:]}")
    data = json.loads(Path(out_path).read_text(encoding="utf-8"))
    Path(out_path).unlink(missing_ok=True)
    rows = data.get(symbol)
    if not rows:
        raise RuntimeError(f"{symbol} 无数据")
    return rows


def run_backtest(rows: list, day: str) -> dict:
    """逐 bar 喂入回测引擎，返回 {trades, stats}。day = 'YYYY-MM-DD'。"""
    tracker = ShadowTracker(persist=False)
    symbol = None
    fed = 0
    for row in rows:
        d = datetime.fromtimestamp(row["time"], ET)
        if d.date().isoformat() != day:
            continue
        hm = d.hour * 60 + d.minute
        if not (ENTRY_START_HM <= hm < 16 * 60):
            continue   # 只喂常规时段 bar
        symbol = symbol or "SYM"
        bar = {k: row[k] for k in ("time", "open", "high", "low", "close", "volume")}
        tracker.process({"SYM": {"bar_c": bar, "ts_c": row.get("ts") or {}}})
        fed += 1
    trades = tracker.events(limit=10000)
    return {"day": day, "bars_fed": fed, "trades": trades,
            "stats": tracker.stats()}


def _hm(ts: float) -> str:
    return datetime.fromtimestamp(ts, ET).strftime("%H:%M")


def print_report(symbol: str, result: dict) -> None:
    trades = result["trades"]
    stats = result["stats"]
    print(f"\n===== {symbol} {result['day']} 回测（喂入 {result['bars_fed']} 根 5M bar）=====")
    if not trades:
        print("  无回踩事件")
    # events() 是倒序，报告按时间正序
    for t in sorted(trades, key=lambda x: x["bar_time"]):
        status = "已止损" if t["stopped"] else ("收盘了结" if t["settled"] else "跟踪中")
        print(f"  {_hm(t['bar_time'])} {t['side']:<5} 踩{t['line_type']:<7} "
              f"入{t['entry']:.2f} 损{t['stop']:.2f} | "
              f"止损前MFE {t['mfe_rr']:+.2f}R 收盘前MFE {t['mfe_rr_eod']:+.2f}R | "
              f"结果 {t['outcome_rr']:+.2f}R [{status}]")
    print(f"\n----- 统计（已结算 {stats['settled']} / 事件 {stats['total_events']}）-----")
    print(f"  止损数: {stats['stopped']}  |  平均结果 RR: {stats['avg_outcome_rr']:+.2f}")
    print(f"  未达 1R 直接止损: {stats['no_1r_count']} 笔"
          f"（占比 {stats['no_1r_pct']}%）")
    print(f"  平均止损前 MFE: {stats['avg_mfe_before_stop']:+.2f}R  |"
          f"  平均收盘前 MFE: {stats['avg_mfe_eod']:+.2f}R")
    print("  N倍盈亏比分布（占已结算 %）:")
    print(f"    {'N':>3} {'止损前≥N':>10} {'收盘前≥N':>10}")
    for n in ("1", "2", "3", "4", "5"):
        bs = stats["dist_before_stop"][n]
        ed = stats["dist_eod"][n]
        print(f"    {n:>3}R {bs['count']:>5} ({bs['pct']:>5}%)"
              f" {ed['count']:>5} ({ed['pct']:>5}%)")
    for lt, d in stats["by_line"].items():
        if d["count"]:
            print(f"  {lt:<8}: {d['count']} 笔, ≥1R {d['ge_1r']} 笔"
                  f"（{d['pct_ge_1r']}%）, 平均止损前 MFE {d['avg_mfe']:+.2f}R")
    for sd, d in stats["by_side"].items():
        if d["count"]:
            print(f"  {sd:<8}: {d['count']} 笔, ≥1R 占比 {d['pct_ge_1r']}%")


def main() -> None:
    logging.basicConfig(level=logging.WARNING)
    ap = argparse.ArgumentParser(description="trend_state 历史回测")
    ap.add_argument("--symbol", required=True, help="如 NASDAQ:NVDA")
    ap.add_argument("--day", default="", help="YYYY-MM-DD，默认上个交易日")
    ap.add_argument("--bars", type=int, default=500,
                    help="拉取的 5M bar 数（含 warmup，默认 500）")
    ap.add_argument("--json", action="store_true", help="输出 JSON 而非文本报告")
    args = ap.parse_args()

    day = args.day or prev_trading_day(datetime.now(ET)).date().isoformat()
    rows = asyncio.run(fetch_history(args.symbol, args.bars, day))
    result = run_backtest(rows, day)
    if args.json:
        print(json.dumps({"symbol": args.symbol, **result}, ensure_ascii=False))
    else:
        print_report(args.symbol, result)


if __name__ == "__main__":
    main()
