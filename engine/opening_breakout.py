"""Shared opening-range breakout rules, used by streaming and offline replay.

Pine supplies the opening range, EMA7 and its normalized slope. This module
combines the existing candle filters without reconstructing Pine indicators.
"""
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from domain.contracts import finite_number

ET = ZoneInfo("America/New_York")
RULES_VERSION = "opening-breakout-2026-11-05"


@dataclass(frozen=True)
class BreakoutRules:
    name: str = "research-base"
    max_wick_fraction: float | None = None
    midpoint_beyond_previous: bool = False
    no_ema7_body_cross3: bool = False
    min_slope_atr: float | None = None
    directional_slope: bool = False
    stop_mode: str = "open"
    morning_only: bool = False
    first_signal_only: bool = False


BASE_RULES = BreakoutRules()
CURRENT_RULES = BreakoutRules(
    name=RULES_VERSION, max_wick_fraction=0.2, midpoint_beyond_previous=True,
    no_ema7_body_cross3=True, min_slope_atr=1.5, directional_slope=True,
    stop_mode="midpoint", morning_only=True, first_signal_only=True,
)


def replay_context(bar, indexed):
    """Adapt cached Pine outputs to the same context sent by the Node collector."""
    stamp = bar["time"]
    previous = indexed.get(stamp - 300) or {}
    output = bar.get("e9") or {}
    samples = []
    for k in range(1, 4):
        row = indexed.get(stamp - k * 300) or {}
        model = row.get("e9") or {}
        samples.append({
            "time": row.get("time"), "open": row.get("open"), "close": row.get("close"),
            "ema7": model.get("ema_9") if model.get("time") == row.get("time") else None,
        })
    return {
        "context_time": stamp, "previous_time": previous.get("time"),
        "previous_high": previous.get("high"), "previous_low": previous.get("low"),
        "ema7_previous3": samples, "ema_slope_atr": output.get("ema_slope_atr"),
        "ema_slope_time": output.get("time"),
    }


def entry_window(stamp):
    local = datetime.fromtimestamp(stamp, ET)
    return local.weekday() < 5 and 585 <= local.hour * 60 + local.minute < 630


def first_signal_window(stamp):
    local = datetime.fromtimestamp(stamp, ET)
    return local.weekday() < 5 and 585 <= local.hour * 60 + local.minute < 625


def evaluate_breakout(symbol, bar, study, zone, policy=BASE_RULES):
    bar_time = bar["time"]
    close = finite_number(bar.get("close"))
    open_px = finite_number(bar.get("open"))
    bar_high = finite_number(bar.get("high"))
    bar_low = finite_number(bar.get("low"))
    wilder = finite_number(study.get("atr")) if study.get("time") == bar_time else None
    sma = finite_number(bar.get("atr_sma10"))
    body_ema = finite_number(bar.get("body_ema10"))
    engulfing = bar.get("prior_engulfing4")
    candidates = [value for value in (wilder, sma) if value is not None and value > 0]
    if (type(engulfing) is not int or engulfing not in (0, 1, 2, 3)
            or close is None or close <= 0 or not candidates or body_ema is None or body_ema < 0
            or open_px is None or open_px <= 0
            or bar_high is None or bar_low is None or not 0 < bar_low <= min(open_px, close) <= max(open_px, close) <= bar_high):
        return None
    atr = min(candidates)
    high, low = zone
    if close >= high + 0.7 * atr:
        side, edge, threshold = "long", high, high + 0.7 * atr
        body, extreme = close - open_px, bar_low
    elif close <= low - 0.7 * atr:
        side, edge, threshold = "short", low, low - 0.7 * atr
        body, extreme = open_px - close, bar_high
    else:
        return None, None
    opposite_engulfing = 2 if side == "long" else 1
    if engulfing & opposite_engulfing:
        return side, None
    if body <= body_ema or not edge - atr <= extreme <= edge + atr:
        return side, None
    if policy.morning_only and not first_signal_window(bar_time):
        return side, None
    o, c, h, l = (Decimal(str(value)) for value in (open_px, close, bar_high, bar_low))
    span = h - l
    if span <= 0:
        return side, None
    upper, lower = h - max(o, c), min(o, c) - l
    if policy.max_wick_fraction is not None:
        limit = Decimal(str(policy.max_wick_fraction)) * span
        if upper > limit or lower > limit:
            return side, None
    midpoint = (o + c) / 2
    previous_high = finite_number(bar.get("previous_high"))
    previous_low = finite_number(bar.get("previous_low"))
    if policy.midpoint_beyond_previous:
        if (bar.get("context_time") != bar_time or bar.get("previous_time") != bar_time - 300
                or previous_high is None or previous_low is None
                or not 0 < previous_low <= previous_high):
            return None
        if not (midpoint > Decimal(str(previous_high)) if side == "long"
                else midpoint < Decimal(str(previous_low))):
            return side, None
    samples = bar.get("ema7_previous3")
    crossed = False
    if policy.no_ema7_body_cross3:
        if not isinstance(samples, list) or len(samples) != 3:
            return None
        for k, sample in enumerate(samples, 1):
            if not isinstance(sample, dict):
                return None
            values = [finite_number(sample.get(key)) for key in ("open", "close", "ema7")]
            if sample.get("time") != bar_time - k * 300 or any(v is None or v <= 0 for v in values):
                return None
            po, pc, pe = map(lambda value: Decimal(str(value)), values)
            crossed |= (po - pe) * (pc - pe) < 0
        if crossed:
            return side, None
    slope = finite_number(bar.get("ema_slope_atr"))
    if policy.min_slope_atr is not None:
        if slope is None or bar.get("ema_slope_time") != bar_time:
            return None
        strength = slope * (1 if side == "long" else -1) if policy.directional_slope else slope
        if strength <= policy.min_slope_atr:
            return side, None
    stop_source = float(midpoint) if policy.stop_mode == "midpoint" else (
        extreme if policy.stop_mode == "extreme" else open_px
    )
    stop = round(stop_source, 2)
    if not (0 < stop < close if side == "long" else 0 < close < stop):
        return side, None
    return side, {
        "id": f"{symbol}:{bar_time}:opening-breakout",
        "symbol": symbol, "ticker": symbol.split(":")[-1], "side": side,
        "time": bar_time + 300, "bar_time": bar_time, "confirmed_at": bar_time + 300,
        "open": open_px, "close": close, "atr": atr,
        "atr_wilder": wilder, "atr_sma10": sma, "edge": edge,
        "threshold": threshold, "distance_atr": abs(close - edge) / atr,
        "body_atr": body / atr, "body_size": body, "body_ema10": body_ema,
        "body_ema_ratio": body / body_ema if body_ema > 0 else None,
        "body_rule": "prior_body_ema10", "stop_px": stop, "signal_extreme": extreme,
        "prior_engulfing4": engulfing, "engulfing_filter": "regular_prior4_opposite",
        "rules_version": policy.name, "initial_stop_rule": policy.stop_mode,
        "signal_midpoint": float(midpoint), "previous_high": previous_high, "previous_low": previous_low,
        "upper_wick_fraction": float(upper / span), "lower_wick_fraction": float(lower / span),
        "body_fraction": float(abs(c-o)/span), "signal_slope_atr": slope,
        "ema7_previous3": samples, "ema7_cross_pass": not crossed,
    }
