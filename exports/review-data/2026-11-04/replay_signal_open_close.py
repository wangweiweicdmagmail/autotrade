"""Offline OHLC execution replay. No production account, broker, or order state."""
import json
import argparse
from collections import Counter
from pathlib import Path
from datetime import datetime
from statistics import mean
from zoneinfo import ZoneInfo

from domain.contracts import StopAmendment

parser = argparse.ArgumentParser()
parser.add_argument("--current-rules", action="store_true")
parser.add_argument("--day", default="2026-11-04")
parser.add_argument("--cache-dir", type=Path)
parser.add_argument("--midpoint-stop", action="store_true")
parser.add_argument("--before1030", action="store_true")
parser.add_argument("--min-body-fraction", type=float, default=0)
parser.add_argument("--max-wick-fraction", type=float)
parser.add_argument("--midpoint-beyond-previous", action="store_true")
parser.add_argument("--no-ema7-body-cross3", action="store_true")
parser.add_argument("--min-slope-atr", type=float)
one_r_options = parser.add_mutually_exclusive_group()
one_r_options.add_argument("--no-one-r", dest="early_one_r", action="store_false")
one_r_options.add_argument("--keep-one-r", dest="early_one_r", action="store_true")
parser.set_defaults(early_one_r=None)
args = parser.parse_args()
if args.current_rules:
    args.midpoint_stop = True
    args.before1030 = True
    args.early_one_r = False
    args.min_slope_atr = 1.5
    args.max_wick_fraction = 0.2
    args.midpoint_beyond_previous = True
    args.no_ema7_body_cross3 = True
if not 0 <= args.min_body_fraction <= 1:
    parser.error("实体占比阈值须在0到1之间")
if args.max_wick_fraction is not None and not 0 <= args.max_wick_fraction <= 1:
    parser.error("单侧影线占比阈值须在0到1之间")
if args.early_one_r is None:
    args.early_one_r = not args.midpoint_stop
output_stem = "execution_replay_signal_open_close"
if args.midpoint_stop:
    output_stem = "execution_replay_midpoint"
if args.before1030:
    output_stem += "_before1030"
if not args.early_one_r:
    output_stem += "_no_one_r"
if args.min_body_fraction:
    output_stem += f"_body_gt{args.min_body_fraction * 100:g}"
if args.max_wick_fraction is not None:
    output_stem += f"_wicks_le{args.max_wick_fraction * 100:g}"

if args.midpoint_beyond_previous:
    output_stem += "_midprev"
if args.no_ema7_body_cross3:
    output_stem += "_ema7nc3"

if args.min_slope_atr is not None:
    output_stem += "_slope_gt" + format(args.min_slope_atr, "g").replace(".", "p")

FOLDER = args.cache_dir.resolve() if args.cache_dir else Path(__file__).resolve().parent
ET = ZoneInfo("America/New_York")
START = int(datetime.fromisoformat(args.day).replace(hour=9, minute=30, tzinfo=ET).timestamp())
END = START + 78 * 300
source = json.loads((FOLDER / "bars.json").read_text())
bars = {symbol: {row["time"]: row for row in rows} for symbol, rows in source.items()}
signal_stem = "breakout_signals_body_ema_engulf4_signal_open_stop"
if args.min_body_fraction:
    signal_stem += f"_body_gt{args.min_body_fraction * 100:g}"
if args.max_wick_fraction is not None:
    signal_stem += f"_wicks_le{args.max_wick_fraction * 100:g}"
if args.midpoint_beyond_previous:
    signal_stem += "_midprev"
if args.no_ema7_body_cross3:
    signal_stem += "_ema7nc3"
if args.min_slope_atr is not None:
    signal_stem += "_slope_gt" + format(args.min_slope_atr, "g").replace(".", "p")
if args.current_rules:
    signal_stem = "breakout_signals_current_v1"
    output_stem = "execution_replay_current_v1"
signals = json.loads((FOLDER / f"{signal_stem}.json").read_text())


def initial_stop(signal, midpoint_stop=False):
    value = (signal["open"] + signal["close"]) / 2 if midpoint_stop else signal["open"]
    return round(value, 2)


def replay(signal, future, *, midpoint_stop=False, early_one_r=True):
    side = 1 if signal["side"] == "long" else -1
    entry = future[0]["open"]
    initial = initial_stop(signal, midpoint_stop)
    risk = side * (entry - initial)
    if risk <= 0:
        return entry, risk, []
    cost = round(entry, 2)
    goal = round(signal["close"], 2)
    # A state is the existing stop and its source before the next candle.
    active = {(initial, "初始止损")}
    exits = set()
    for index, bar in enumerate(future):
        following = set()
        for original_stop, original_reason in active:
            for points in ((bar["high"], bar["low"]), (bar["low"], bar["high"])):
                stop, reason = original_stop, original_reason
                price = bar["open"]
                if side * (price - stop) <= 0:
                    exits.add((side * (price-entry)/risk, bar["time"], reason, price))
                    continue
                if early_one_r and side * (price-entry) >= risk:
                    tightened = StopAmendment(signal["side"], stop, cost).tightened_price()
                    if tightened != stop:
                        stop, reason = tightened, "1R保本止损"
                for price in (*points, bar["close"]):
                    if side * (price-stop) <= 0:
                        exits.add((side * (stop-entry)/risk, bar["time"]+300, reason, stop))
                        break
                    if early_one_r and side * (price-entry) >= risk:
                        tightened = StopAmendment(signal["side"], stop, cost).tightened_price()
                        if tightened != stop:
                            stop, reason = tightened, "1R保本止损"
                else:
                    if index == 0:
                        tightened = StopAmendment(signal["side"], stop, goal).tightened_price()
                        if tightened != stop:
                            stop, reason = tightened, "信号收盘价止损"
                            if side * (bar["close"]-stop) <= 0:
                                exits.add((side * (bar["close"]-entry)/risk,
                                           bar["time"]+300, "首根收盘直接退出", bar["close"]))
                                continue
                    following.add((stop, reason))
        active = following
    for stop, reason in active:
        price = future[-1]["close"]
        exits.add((side * (price-entry)/risk, future[-1]["time"]+300, "15分钟平仓", price))
    return entry, risk, sorted(exits)


first = {}
for signal in sorted(signals, key=lambda row: (row["time"], -row["body_atr"], row["symbol"])):
    first.setdefault(signal["symbol"], signal)
rows = []
for signal in first.values():
    if args.before1030 and signal["open_et"] >= "10:30":
        continue
    t = signal["time"]
    if args.current_rules:
        from engine.opening_breakout import entry_window
        if not entry_window(t):
            continue
    row = dict(symbol=signal["symbol"], ticker=signal["ticker"], side=signal["side"],
               signal_time=signal["open_et"], entry_time=t, entry_et=signal["close_et"],
               stop=initial_stop(signal, args.midpoint_stop), protection_target=round(signal["close"], 2),
               upper_wick_fraction=signal.get("upper_wick_fraction"),
               lower_wick_fraction=signal.get("lower_wick_fraction"),
               signal_slope_atr=signal.get("signal_slope_atr"),
               signal_midpoint=signal.get("signal_midpoint"), previous_high=signal.get("previous_high"),
               previous_low=signal.get("previous_low"), ema7_previous3=signal.get("ema7_previous3"),
               body_fraction=signal.get("body_fraction"), body_ratio=signal["body_ema_ratio"], body_atr=signal["body_atr"], st_aligned=signal["st_aligned"])
    rows.append(row)
    if t+900 > END:
        row["status"] = "观察窗口不足"
        continue
    future = [bars[signal["symbol"]].get(stamp) for stamp in range(t, t+900, 300)]
    if not all(future):
        row["status"] = "行情缺失"
        continue
    entry, risk, outcomes = replay(signal, future, midpoint_stop=args.midpoint_stop, early_one_r=args.early_one_r)
    row.update(entry=entry, risk_price=risk)
    if not outcomes:
        row["status"] = "入场价越过初始止损"
        continue
    low, high = outcomes[0], outcomes[-1]
    row.update(status="已回放", low_r=low[0], high_r=high[0],
               low_exit=low[1], high_exit=high[1], low_reason=low[2], high_reason=high[2],
               low_price=low[3], high_price=high[3], possible_outcomes=outcomes)


def stats(group, mode):
    values = [row[f"{mode}_r"] for row in group]
    wins = [v for v in values if v > 1e-9]
    losses = [-v for v in values if v < -1e-9]
    return dict(trades=len(group), win=len(wins), loss=len(losses),
                flat=len(values)-len(wins)-len(losses), net_r=sum(values),
                average_r=mean(values) if values else None, win_rate=len(wins)/len(values) if values else None,
                payoff_ratio=mean(wins)/mean(losses) if wins and losses else None,
                exit_reasons=dict(Counter(row[f"{mode}_reason"] for row in group)))


summary = {"independent": {}, "portfolio": {}}
valid = [row for row in rows if row["status"] == "已回放"]
for mode in ("low", "high"):
    summary["independent"][mode] = stats(valid, mode)
    active = []
    accepted = []
    decisions = []
    for row in sorted(valid, key=lambda r: (r["entry_time"], -r["body_atr"], r["symbol"])):
        if row["entry_time"] >= START+3600:
            decisions.append((row["ticker"], row["signal_time"], "超过开仓时限"))
            continue
        if not row["st_aligned"]:
            decisions.append((row["ticker"], row["signal_time"], "ST不同向"))
            continue
        active = [end_time for end_time in active if end_time > row["entry_time"]]
        if len(active) >= 5:
            decisions.append((row["ticker"], row["signal_time"], "已满五笔"))
            continue
        accepted.append(row)
        active.append(row[f"{mode}_exit"])
        assert len(active) <= 5
        decisions.append((row["ticker"], row["signal_time"], "入场"))
    summary["portfolio"][mode] = dict(stats(accepted, mode), decisions=decisions)

report = dict(day_et=args.day,
              rules=dict(rules_version="opening-breakout-2026-11-05" if args.current_rules else "research", directional_slope=args.current_rules, first_signal_only=True, min_signal_slope_atr_strict=args.min_slope_atr, midpoint_beyond_previous=args.midpoint_beyond_previous, no_ema7_body_cross3=args.no_ema7_body_cross3, min_body_fraction_strict=args.min_body_fraction,
                         max_wick_fraction_inclusive=args.max_wick_fraction,
                         initial_stop="signal_open_close_midpoint" if args.midpoint_stop else "signal_open",
                         signal_window="entry_before_10:30_et_last_signal_10:20" if args.current_rules else "before_10:30_bar_open_et" if args.before1030 else "regular_session",
                         timed_stop="signal_close_after_entry_bar_closes", early_breakeven_r=1 if args.early_one_r else None,
                         max_hold_minutes=15, simulated_market_fill="next_5m_open",
                         fees_and_extra_slippage=False),
              summary=summary, trades=rows)
(FOLDER / f"{output_stem}.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
print(json.dumps(summary, ensure_ascii=False, indent=2))

lines = [
    "# 信号中点止损与入场K线收盘后保护的执行回放" if args.midpoint_stop else "# 信号开盘价止损与入场K线收盘后保护的执行回放", "",
    f"{args.day}美东交易日，实体EMA10及近四根开盘后逆向反包过滤，每标的只做首次合格信号。",
    (f"增加实体占比严格大于{args.min_body_fraction:.0%}过滤，先逐根判断完整条件，再选各标的首次合格信号。" if args.min_body_fraction else "实体占比未设置额外门槛。"),
    (f"上影线及下影线分别不超过高低跨度的{args.max_wick_fraction:.0%}，等于阈值通过。" if args.max_wick_fraction is not None else "单侧影线未设置额外门槛。"),
    ("多头实体中点严格高于前高，空头实体中点严格低于前低。" if args.midpoint_beyond_previous else "实体中点未设置前高前低门槛。"),
    ("信号前3根K线的开收盘不能分处各自Pine EMA7两侧；等于EMA7不算穿越。" if args.no_ema7_body_cross3 else "前三根未设置EMA7实体穿越门槛。"),
    ("Pine斜率多头严格大于1.5，空头严格小于-1.5。" if args.current_rules else f"信号K线上的Pine EMA_Slope_ATR严格大于{args.min_slope_atr:g}。" if args.min_slope_atr is not None else "斜率未设置额外门槛。"),
    ("初始止损取信号开盘价和收盘价中点。" if args.midpoint_stop else "初始止损取信号开盘价。")
    + "信号收盘确认后用下一根5分钟K线开盘价模拟市价成交。该入场K线收盘后，止损收紧至原信号收盘价。"
    + ("保留先到1R提前移至成交价的规则。" if args.early_one_r else "不启用1R提前保本，保护只在入场K线收盘后激活。")
    + "最多持有15分钟。",
    ("10:30起停止下单，最后信号开盘10:20。" if args.current_rules else "逐笔表只保留信号K线开盘时间早于10:30的首次信号。" if args.before1030 else "逐笔表为全天首次信号回放。")
    + "逐笔独立回放不限制同时持仓数，另给出最多同时五笔、10:30起停止开仓的组合统计，同刻按界面现有的实体/ATR倍数排序。跳过本标的首次合格信号后，该标的不再补位入场。",
    "一根K线内有两种高低点先后路径。表中列出所有枚举路径的最小和最大结果，实际路径未知。组合按逐笔两种结果分别演算，仓位释放时间也随路径变化，两个组合情形不是全局最优与最差收益的严格边界。",
    "新旧止损只收紧不放松。价差跨过已有止损按该根开盘价退出；首根收盘时已越过新目标则按该收盘价退出。未扣手续费及额外滑点，CFD仅以R度量。", "",
    ("| 信号K线时间 | 标的 | 方向 | 信号斜率 | 模拟入场 | 初始止损 | 后续保护目标 | 回放结果 | 退出原因 |"
     if args.min_slope_atr is not None else
     "| 信号K线时间 | 标的 | 方向 | 模拟入场 | 初始止损 | 后续保护目标 | 回放结果 | 退出原因 |"),
    ("|---|---|---|---:|---:|---:|---:|---:|---|"
     if args.min_slope_atr is not None else "|---|---|---|---:|---:|---:|---:|---|"),
]
for row in sorted(rows, key=lambda r: (r["entry_time"], r["ticker"])):
    if row["status"] != "已回放":
        result, reason = "—", row["status"]
    else:
        a, b = row["low_r"], row["high_r"]
        def display_r(value):
            return "0R" if abs(value) < 1e-9 else f"{value:+.3f}R" if abs(value) < 0.005 else f"{value:+.2f}R"
        result = display_r(a) if abs(a-b)<1e-9 else f"{display_r(a)}～{display_r(b)}"
        reason = row["low_reason"] if row["low_reason"]==row["high_reason"] else f"{row['low_reason']} / {row['high_reason']}"
    slope_column = f" {row['signal_slope_atr']:+.4f} |" if args.min_slope_atr is not None else ""
    line = f"| {row['signal_time']} | {row['ticker']} | {'多' if row['side']=='long' else '空'} |{slope_column} {row.get('entry',0):.3f} | {row['stop']:.2f} | {row['protection_target']:.2f} | {result} | {reason} |"
    print(line)
    lines.append(line)
lines += ["", "## 汇总", "", "| 范围 | 情形 | 笔数 | 盈利/持平/亏损 | 合计R | 胜率 |", "|---|---|---:|---|---:|---:|"]
for scope in ("independent", "portfolio"):
    for mode in ("low","high"):
        s=summary[scope][mode]
        win_rate = f"{s['win_rate']:.1%}" if s["win_rate"] is not None else "—"
        lines.append(f"| {'逐信号独立' if scope=='independent' else '五仓/首小时组合'} | {mode} | {s['trades']} | {s['win']}/{s['flat']}/{s['loss']} | {s['net_r']:+.3f}R | {win_rate} |")
(FOLDER / f"{output_stem.upper()}.md").write_text("\n".join(lines)+"\n")
