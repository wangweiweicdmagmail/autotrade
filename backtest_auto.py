# -*- coding: utf-8 -*-
"""
全自动策略回测 (backtest_auto.py)
==================================

把历史 5M bar（含 trend_state 指标值）逐根喂给与实盘**同一个**
StrategyEngine（+ SimBroker 触价成交网关），验证「同一策略 + 同一数据
= 同一结果」。

用法：
    python3 backtest_auto.py --strategy trend_pullback --symbol NASDAQ:NVDA --day 2026-10-16
    python3 backtest_auto.py --strategy trend_pullback --symbol NASDAQ:NVDA --range 5
    python3 backtest_auto.py --strategy trend_pullback --symbol NASDAQ:NVDA --day 2026-10-15,2026-10-16 --json

口径（与实盘 tick 触发的统一约定）：
- bar 内 tick 路径不可知：多头用 bar.low、空头用 bar.high 模拟触线/触止损
  （on_bar_extremes）；进场指标 = 上一根 bar 的确认值；
- ACTIVE 按 tick/极值达到 2R 立即保本；每根 bar 收盘执行 ST 跟踪/反向平仓，
  指标 = 本根确认值；
- 收盘（15:55 ET 的 bar）后仍持仓 → 按最后收盘价了结（eod）。
"""

import argparse
import asyncio
import json
import logging
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))

import config
from backtest_trend import ET, ENTRY_START_HM, fetch_history, prev_trading_day
from engine.auto_engine import ACTIVE, DONE, PENDING, SimBroker, StrategyEngine
from engine.strategies import build_strategy
from execution.risk import RiskConfig, RiskGate

logger = logging.getLogger("backtest_auto")

SETTLE_HM = 15 * 60 + 55


def _make_risk_gate() -> RiskGate:
    return RiskGate(RiskConfig(
        per_trade_pct=config.RISK_PER_TRADE_PCT,
        per_trade_usd=config.RISK_PER_TRADE_USD,
        max_order_qty=config.RISK_MAX_ORDER_QTY,
        max_open_positions=config.RISK_MAX_OPEN_POSITIONS,
        daily_loss_limit_usd=config.RISK_DAILY_LOSS_LIMIT_USD,
    ))


async def run_day(rows: list, symbol: str, day: str,
                  strategy_name: str, params: dict) -> dict:
    """单日回测：逐 bar 喂引擎，返回该日成交与统计。"""
    strategy = build_strategy(strategy_name, **params)
    broker = SimBroker()
    events: list = []
    engine = StrategyEngine(
        broker, risk_gate=_make_risk_gate(), gateway=None,
        emit=lambda t, **d: events.append({"type": t, **d}))
    engine.start(strategy, [symbol])

    day_rows = []
    for i, row in enumerate(rows):
        d = datetime.fromtimestamp(row["time"], ET)
        if d.date().isoformat() == day:
            hm = d.hour * 60 + d.minute
            if ENTRY_START_HM <= hm < 16 * 60:
                day_rows.append((i, row, hm))

    prev_row = None
    for i, row, hm in day_rows:
        bar = {k: row[k] for k in ("time", "open", "high", "low", "close", "volume")}
        ts = row.get("ts") or {}
        if prev_row is not None:
            prev_bar = {k: prev_row[k] for k in
                        ("time", "open", "high", "low", "close", "volume")}
            prev_ts = prev_row.get("ts") or {}
            # 1) bar 内 tick（极值建模）：触线进场 / 触价成交 / 触止损
            await engine.on_bar_extremes(symbol, bar, prev_ts, prev_bar, hm,
                                         fvg_c=prev_row.get("fvg"))
        # 2) bar 收盘管理（保本 / ST 跟踪 / 反向平仓 / 过期）
        await engine.on_bar(symbol, bar, ts)
        prev_row = row

    # EOD 了结：收盘后仍持仓/挂单的一律按最后收盘价处理
    if prev_row is not None:
        last_close = prev_row["close"]
        last_bt = prev_row["time"]
        for inst in engine._instances.values():
            if inst.state == DONE or not inst.lot_id:
                continue
            lot = broker.get(inst.lot_id)
            if lot is None:
                continue
            if lot.state == ACTIVE:
                await broker.close(inst.lot_id, last_close, "eod", ts=last_bt)
                engine._finish(inst, "eod")
            elif lot.state == PENDING:
                await broker.cancel(inst.lot_id)
                engine._finish(inst, "eod_cancel")

    trades = []
    for lot in broker.closed_results():
        risk_amt = abs(lot.entry_px - (lot.init_stop_px or lot.stop_px))
        rr = round(lot.realized_pnl / (risk_amt * lot.qty), 3) \
            if risk_amt > 0 and lot.qty > 0 else 0.0
        mfe_r = round(lot.mfe / (risk_amt * lot.qty), 2) \
            if risk_amt > 0 and lot.qty > 0 else 0.0
        trades.append({
            "lot_id": lot.lot_id, "symbol": lot.symbol, "side": lot.side,
            "qty": lot.qty, "entry": lot.avg_fill or lot.entry_px,
            "stop": lot.init_stop_px or lot.stop_px,
            "exit": lot.exit_px,
            "reason": lot.exit_reason, "pnl": lot.realized_pnl,
            "rr": rr, "mfe_r": mfe_r,
            "entry_time": lot.fill_time, "exit_time": lot.exit_time,
        })
    return {"day": day, "bars": len(day_rows), "trades": trades,
            "events": events}


def summarize(trades: list) -> dict:
    n = len(trades)
    if not n:
        return {"count": 0}
    wins = [t for t in trades if t["pnl"] > 0]
    stopped = [t for t in trades if t["reason"] == "stop"]
    no_1r = [t for t in stopped if t["mfe_r"] < 1]
    dist = {}
    for k in range(1, 6):
        c = sum(1 for t in trades if t["mfe_r"] >= k)
        dist[str(k)] = {"count": c, "pct": round(c / n * 100, 1)}
    return {
        "count": n,
        "wins": len(wins),
        "win_rate": round(len(wins) / n * 100, 1),
        "total_pnl": round(sum(t["pnl"] for t in trades), 2),
        "avg_rr": round(sum(t["rr"] for t in trades) / n, 2),
        "expectancy": round(sum(t["pnl"] for t in trades) / n, 2),
        "avg_mfe_r": round(sum(t["mfe_r"] for t in trades) / n, 2),
        "max_mfe_r": round(max(t["mfe_r"] for t in trades), 2),
        "stopped": len(stopped),
        "no_1r_count": len(no_1r),
        "no_1r_pct": round(len(no_1r) / n * 100, 1),
        "dist_mfe": dist,
    }


def _hm(ts: float) -> str:
    return datetime.fromtimestamp(ts, ET).strftime("%H:%M")


def print_report(symbol: str, strategy_name: str, day_results: list) -> None:
    all_trades = []
    for res in day_results:
        all_trades.extend(res["trades"])
        print(f"\n===== {symbol} {res['day']} [{strategy_name}]"
              f"（{res['bars']} 根 bar）=====")
        if not res["trades"]:
            print("  无成交")
        for t in res["trades"]:
            print(f"  {_hm(t['entry_time'])} {t['side']:<5} x{t['qty']:<4} "
                  f"入{t['entry']:.2f} 损{t['stop']:.2f} → "
                  f"{_hm(t['exit_time'])} 出{t['exit']:.2f} [{t['reason']}] "
                  f"PnL {t['pnl']:+.2f} RR {t['rr']:+.2f} MFE {t['mfe_r']:+.2f}R")
    s = summarize(all_trades)
    if not s.get("count"):
        return
    print(f"\n----- 汇总（{s['count']} 笔）-----")
    print(f"  胜率 {s['win_rate']}% | 总 PnL {s['total_pnl']:+.2f} | "
          f"平均 RR {s['avg_rr']:+.2f} | 期望 {s['expectancy']:+.2f}")
    print(f"  止损 {s['stopped']} 笔 | 未达 1R 直接止损 {s['no_1r_count']} 笔"
          f"（{s['no_1r_pct']}%）| 平均 MFE {s['avg_mfe_r']:+.2f}R"
          f" | 最大 MFE {s['max_mfe_r']:+.2f}R")
    print("  MFE ≥N 分布:",
          "  ".join(f"{k}R:{v['count']}({v['pct']}%)" for k, v in s["dist_mfe"].items()))


def main() -> None:
    logging.basicConfig(level=logging.WARNING)
    ap = argparse.ArgumentParser(description="全自动策略回测")
    ap.add_argument("--strategy", default="trend_pullback")
    ap.add_argument("--symbol", required=True)
    ap.add_argument("--day", default="", help="YYYY-MM-DD，逗号分隔多日；默认上个交易日")
    ap.add_argument("--range", type=int, default=0, help="近 N 个交易日（覆盖 --day）")
    ap.add_argument("--bars", type=int, default=1500, help="拉取的 5M bar 数")
    ap.add_argument("--param", action="append", default=[],
                    help="覆盖策略参数，如 breakeven_r=1.5（可多次）")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    params = {}
    for kv in args.param:
        k, _, v = kv.partition("=")
        try:
            params[k] = float(v)
        except ValueError:
            params[k] = v

    if args.day:
        day_list = [d.strip() for d in args.day.split(",") if d.strip()]
    elif args.range > 0:
        day_list = []        # 拉取后按数据中实际可用的交易日切片
    else:
        day_list = [prev_trading_day(datetime.now(ET)).date().isoformat()]

    # 数据锚点 = 回测区间最后一天 16:00 ET：从该日尾盘往前拉，
    # warmup 与当日实盘所见一致（不从最新收盘倒推）
    anchor_day = (max(day_list) if day_list
                  else prev_trading_day(datetime.now(ET)).date().isoformat())
    # 按区间跨度补足 bar 数（extended 会话一天约 192 根 + warmup 200 根）
    from datetime import date as _date
    span = (_date.fromisoformat(anchor_day)
            - _date.fromisoformat(min(day_list) if day_list else anchor_day)).days
    need = (max(span, args.range) + 1) * 200 + 200
    bars = max(args.bars, need)
    rows = asyncio.run(fetch_history(args.symbol, bars, anchor_day))
    if args.range > 0:
        days = sorted({datetime.fromtimestamp(r["time"], ET).date().isoformat()
                       for r in rows})
        day_list = days[-args.range:]

    async def _run():
        out = []
        for day in day_list:
            out.append(await run_day(rows, args.symbol, day,
                                     args.strategy, params))
        return out

    results = asyncio.run(_run())
    if args.json:
        all_trades = [t for r in results for t in r["trades"]]
        print(json.dumps({"symbol": args.symbol, "strategy": args.strategy,
                          "days": [{"day": r["day"], "bars": r["bars"],
                                    "trades": r["trades"]} for r in results],
                          "summary": summarize(all_trades)},
                         ensure_ascii=False))
    else:
        print_report(args.symbol, args.strategy, results)


if __name__ == "__main__":
    main()
