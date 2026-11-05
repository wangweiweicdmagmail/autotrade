"""Replay existing confirmed-breakout detection against the immutable daily cache."""
import json
from decimal import Decimal
import sys
import subprocess
from collections import Counter, defaultdict, deque
from datetime import datetime
from pathlib import Path
from statistics import mean, median
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from console.services.signals import SignalsService
from domain.contracts import finite_number
from engine.opening_breakout import BASE_RULES, CURRENT_RULES, BreakoutRules, evaluate_breakout, replay_context

day_et = sys.argv[sys.argv.index("--day")+1] if "--day" in sys.argv else "2026-11-04"
FOLDER = (Path(sys.argv[sys.argv.index("--cache-dir")+1]).resolve()
          if "--cache-dir" in sys.argv else Path(__file__).resolve().parent)
ET = ZoneInfo("America/New_York")
START = int(datetime.fromisoformat(day_et).replace(hour=9, minute=30, tzinfo=ET).timestamp())
END = START + 78 * 300
data = json.loads((FOLDER / "bars.json").read_text())
current_rules = "--current-rules" in sys.argv
min_body_fraction = 0
if "--min-body-fraction" in sys.argv:
    min_body_fraction = float(sys.argv[sys.argv.index("--min-body-fraction") + 1])
    if not 0 <= min_body_fraction <= 1:
        raise ValueError("实体占比阈值须在0到1之间")
max_wick_fraction = None
if "--max-wick-fraction" in sys.argv:
    max_wick_fraction = float(sys.argv[sys.argv.index("--max-wick-fraction") + 1])
    if not 0 <= max_wick_fraction <= 1:
        raise ValueError("单侧影线占比阈值须在0到1之间")
min_slope_atr = None
if "--min-slope-atr" in sys.argv:
    min_slope_atr = float(sys.argv[sys.argv.index("--min-slope-atr") + 1])
    if finite_number(min_slope_atr) is None:
        raise ValueError("斜率阈值必须为有限数值")
midpoint_beyond_previous = "--midpoint-beyond-previous" in sys.argv
no_ema7_body_cross3 = "--no-ema7-body-cross3" in sys.argv
legacy_atr = "--legacy-atr" in sys.argv
simple_mean = "--simple-mean" in sys.argv
if legacy_atr and simple_mean:
    raise ValueError("Choose one baseline")
rule = "legacy_atr" if legacy_atr else "prior10_body_mean" if simple_mean else "prior_body_ema10"
suffix = "" if legacy_atr else "_body_mean" if simple_mean else "_body_ema"
extreme_stop = legacy_atr or simple_mean or "--extreme-stop" in sys.argv
apply_engulfing = not (legacy_atr or simple_mean or "--no-engulf-filter" in sys.argv)
if apply_engulfing:
    suffix += "_engulf4"
    script = """
const fs = require('fs');
const { priorRegularEngulfing4 } = require('./service/candle_metrics.js');
const bars = JSON.parse(fs.readFileSync(process.argv[1], 'utf8'));
const result = {};
for (const [symbol, rows] of Object.entries(bars)) {
  const periods = rows.slice().reverse();
  result[symbol] = Object.fromEntries(periods.map((row, index) => [
    row.time, priorRegularEngulfing4(periods, index),
  ]));
}
process.stdout.write(JSON.stringify(result));
"""
    completed = subprocess.run(
        ["node", "-e", script, str(FOLDER / "bars.json")],
        cwd=FOLDER.parents[2], check=True, capture_output=True, text=True,
    )
    engulfing_flags = json.loads(completed.stdout)
if not extreme_stop:
    suffix += "_signal_open_stop"
if min_body_fraction:
    suffix += f"_body_gt{min_body_fraction * 100:g}"
if max_wick_fraction is not None:
    suffix += f"_wicks_le{max_wick_fraction * 100:g}"
if midpoint_beyond_previous:
    suffix += "_midprev"
if no_ema7_body_cross3:
    suffix += "_ema7nc3"
if min_slope_atr is not None:
    suffix += "_slope_gt" + format(min_slope_atr, "g").replace(".", "p")
for symbol, rows in data.items():
    prior = deque(maxlen=10)
    ema = None
    count = 0
    seed = 0
    for row in rows:
        if legacy_atr:
            baseline = min(
                value for value in (row["ts"].get("atr"), row.get("atr_sma10"))
                if value is not None and value > 0
            )
        elif simple_mean:
            baseline = mean(prior) if len(prior) == 10 else None
        else:
            baseline = ema
        row["prior_engulfing4"] = engulfing_flags[symbol][str(row["time"])] if apply_engulfing else 0
        row["body_ema10"] = baseline
        body = abs(row["close"] - row["open"])
        prior.append(body)
        count += 1
        if count <= 10:
            seed += body
            if count == 10:
                ema = seed / 10
        else:
            ema = (2 / 11) * body + (9 / 11) * ema
indexed = {symbol: {row["time"]: row for row in rows} for symbol, rows in data.items()}


research_policy = BreakoutRules(
    max_wick_fraction=max_wick_fraction, midpoint_beyond_previous=midpoint_beyond_previous,
    no_ema7_body_cross3=no_ema7_body_cross3, min_slope_atr=min_slope_atr,
    directional_slope="--directional-slope" in sys.argv,
    stop_mode="extreme" if extreme_stop else "open",
)
policy = CURRENT_RULES if current_rules else research_policy
if current_rules:
    suffix = "_current_v1"


class ReplaySignalsService(SignalsService):
    _breakout_policy = policy

    @staticmethod
    def _evaluate_opening_breakout(symbol, bar, study, zone):
        return evaluate_breakout(symbol, bar, study, zone, policy)


scanner = ReplaySignalsService(SimpleNamespace(events=SimpleNamespace(record_event=lambda *a, **kw: None)))
for stamp in range(START + 600, END, 300):
    market = {}
    for symbol, bars in indexed.items():
        row = bars[stamp]
        market[symbol] = dict(bar={"time": stamp + 300}, bar_c=dict(row, **replay_context(row, bars)),
                              ts_c=dict(row["ts"], time=stamp), attack_c=row["attack"])
    scanner._scan_opening_breakouts(market, stamp + 300)

signals = sorted(scanner.opening_breakouts(END), key=lambda row: (row["time"], row["symbol"]))
seen = Counter()
seen_side = Counter()
for signal in signals:
    symbol = signal["symbol"]
    stamp = signal["time"]
    candle = indexed[symbol][stamp - 300]
    side = 1 if signal["side"] == "long" else -1
    reference = signal["close"]
    risk = side * (reference - signal["stop_px"])
    seen[symbol] += 1
    seen_side[symbol, signal["side"]] += 1
    signal.update(
        open_et=datetime.fromtimestamp(stamp - 300, ET).strftime("%H:%M"),
        close_et=datetime.fromtimestamp(stamp, ET).strftime("%H:%M"),
        bar_number=(stamp - START) // 300,
        occurrence=seen[symbol],
        side_occurrence=seen_side[symbol, signal["side"]],
        risk_price=risk,
        risk_atr=risk / signal["atr"],
        st_aligned=candle["ts"]["direction"] == side,
        qqq_st_aligned=indexed["NASDAQ:QQQ"][stamp - 300]["ts"]["direction"] == side,
        window=("首小时" if stamp < START + 3600 else
                "10:30–11:30" if stamp < START + 7200 else
                "11:30–13:00" if stamp < START + 12600 else "13:00–16:00"),
        body_bin=("1–1.25" if signal["body_atr"] < 1.25 else
                  "1.25–1.5" if signal["body_atr"] < 1.5 else
                  "1.5–2" if signal["body_atr"] < 2 else "≥2"),
    )
    if risk <= 0:
        raise ValueError("Invalid initial risk")
    for minutes in (5, 15, 30, 60):
        horizon = minutes * 60
        future = [indexed[symbol].get(t) for t in range(stamp, stamp + horizon, 300)]
        if stamp + horizon > END or not all(future):
            signal[f"r_{minutes}"] = None
            continue
        signal[f"r_{minutes}"] = side * (future[-1]["close"] - reference) / risk
        favorable = max(side * (b["high" if side == 1 else "low"] - reference) / risk for b in future)
        adverse = min(side * (b["low" if side == 1 else "high"] - reference) / risk for b in future)
        signal[f"mfe_{minutes}"] = favorable
        signal[f"mae_{minutes}"] = adverse
        first_stop = next((i for i, b in enumerate(future)
                           if side * (b["low" if side == 1 else "high"] - reference) / risk <= -1), None)
        for target in (1, 2):
            first_target = next((i for i, b in enumerate(future)
                                 if side * (b["high" if side == 1 else "low"] - reference) / risk >= target), None)
            label = "均未触及"
            if first_target is not None and first_stop is not None and first_target == first_stop:
                label = "同根不确定"
            elif first_target is not None and (first_stop is None or first_target < first_stop):
                label = "目标先到"
            elif first_stop is not None:
                label = "止损先到"
            signal[f"first_{target}r_{minutes}"] = label
        # A close back across the broken edge, rather than merely touching it.
        signal[f"back_inside_{minutes}"] = any(side * (b["close"] - signal["edge"]) <= 0 for b in future)

def stats(rows):
    result = {"signals": len(rows)}
    for minutes in (5, 15, 30, 60):
        eligible = [r for r in rows if r[f"r_{minutes}"] is not None]
        if not eligible:
            continue
        returns = [r[f"r_{minutes}"] for r in eligible]
        result[str(minutes)] = dict(
            n=len(eligible), favorable=sum(r > 0 for r in returns),
            favorable_rate=sum(r > 0 for r in returns) / len(returns),
            mean_r=mean(returns), median_r=median(returns),
            reached_1r=sum(r[f"mfe_{minutes}"] >= 1 for r in eligible),
            reached_2r=sum(r[f"mfe_{minutes}"] >= 2 for r in eligible),
            back_inside=sum(r[f"back_inside_{minutes}"] for r in eligible),
            first_1r=dict(Counter(r[f"first_1r_{minutes}"] for r in eligible)),
            first_2r=dict(Counter(r[f"first_2r_{minutes}"] for r in eligible)),
        )
    return result

groupings = {}
for name, key in [
    ("时段", lambda s: s["window"]),
    ("方向", lambda s: s["side"]),
    ("实体ATR", lambda s: s["body_bin"]),
    ("标的ST同向", lambda s: str(s["st_aligned"])),
    ("QQQ_ST同向", lambda s: str(s["qqq_st_aligned"])),
    ("首次与重复", lambda s: "首次" if s["occurrence"] == 1 else "重复"),
]:
    groups = defaultdict(list)
    for signal in signals:
        groups[key(signal)].append(signal)
    groupings[name] = {key: stats(rows) for key, rows in groups.items()}

batches = defaultdict(list)
for signal in signals:
    batches[signal["open_et"]].append(signal)
batch_summary = [{"time": key, "signals": len(rows),
                  "long": [r["ticker"] for r in rows if r["side"] == "long"],
                  "short": [r["ticker"] for r in rows if r["side"] == "short"],
                  "forward": stats(rows)}
                 for key, rows in batches.items()]
counts = Counter(s["symbol"] for s in signals)
summary = dict(rules_version=policy.name, directional_slope=policy.directional_slope, min_signal_slope_atr_strict=policy.min_slope_atr, midpoint_beyond_previous=policy.midpoint_beyond_previous, no_ema7_body_cross3=policy.no_ema7_body_cross3, max_wick_fraction_inclusive=policy.max_wick_fraction, min_body_fraction_strict=min_body_fraction, stop_rule=policy.stop_mode, display_time="bar_open_et", engulfing_filter="regular_prior4_opposite" if apply_engulfing else None, body_rule=rule, day_et=day_et, total_signals=len(signals), unique_symbols=len(counts),
               repeats={k: v for k, v in counts.items() if v > 1},
               overall=stats(signals), groups=groupings, batches=batch_summary)
(FOLDER / f"breakout_signals{suffix}.json").write_text(json.dumps(signals, ensure_ascii=False, indent=2))
(FOLDER / f"breakout_statistics{suffix}.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2))
print(json.dumps(summary, ensure_ascii=False, indent=2))
