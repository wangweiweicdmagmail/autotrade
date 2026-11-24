"""Validate the shared confirmed one-minute TD output; never recompute it."""
import math

TD_TREND_THRESHOLD = 3


def confirmed_td(track, bar_time):
    td = track.get('td')
    if (not isinstance(td, dict) or td.get('version') != 1 or td.get('comparison_bars') != 4
            or td.get('trend_threshold') != TD_TREND_THRESHOLD or not isinstance(td.get('counts'), list)
            or any(isinstance(td.get(k), bool) for k in ('version', 'comparison_bars', 'trend_threshold'))):
        return None
    matching = [p for p in td['counts'] if isinstance(p, list) and len(p) == 2 and p[0] == bar_time]
    if len(matching) != 1:
        return None
    value = matching[0][1]
    if (isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)
            or int(value) != value or abs(value) > 2**53 - 1):
        return None
    return int(value)
