"""Consume the confirmed 5M Attack opening range; never derive it from OHLC."""

from datetime import datetime
from zoneinfo import ZoneInfo

from domain.contracts import finite_number

ET = ZoneInfo("America/New_York")


def opening_range(data: dict, *, as_of: float | None = None, include_session_close: bool = False):
    """Return (high, low) only for the current session's aligned, closed bar."""
    current = finite_number((data.get("bar") or {}).get("time"))
    confirmed = finite_number((data.get("bar_c") or {}).get("time"))
    attack = data.get("attack_c") or {}
    if not current or not confirmed or confirmed + 300 != current:
        return None
    if finite_number(attack.get("time")) != confirmed:
        return None
    if as_of is not None:
        # Reject a cached bar from an earlier session or an outdated live bar.
        if not current <= as_of < current + 300:
            return None
    try:
        current_et = datetime.fromtimestamp(current, ET)
    except (OverflowError, OSError, ValueError):
        return None
    last_minute = 1600 if include_session_close else 1559
    if current_et.weekday() >= 5 or not 945 <= current_et.hour * 100 + current_et.minute <= last_minute:
        return None
    high = finite_number(attack.get("opening3High"))
    low = finite_number(attack.get("opening3Low"))
    return (high, low) if high is not None and low is not None and high >= low > 0 else None


def beyond_opening_range(zone, direction, price) -> bool:
    price = finite_number(price)
    if zone is None or price is None or price <= 0:
        return False
    high, low = zone
    return (direction == 1 and price > high) or (direction == -1 and price < low)


def opening_entry_block(data: dict, direction, price, *, as_of: float) -> str:
    zone = opening_range(data, as_of=as_of)
    if zone is None:
        return "开盘小区间未确认或数据缺失"
    if not beyond_opening_range(zone, direction, price):
        return "现价未突破开盘小区间的同向边界"
    return ""
