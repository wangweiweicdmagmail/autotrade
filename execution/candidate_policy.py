"""Per-order candidate protection. Consumes ingestion values; never computes indicators."""
from datetime import datetime
from zoneinfo import ZoneInfo

from domain.contracts import finite_number

ET = ZoneInfo("America/New_York")
POLICY_KEY = "candidate_order_policy"
TIMER_RULE = "full_minutes_after_entry_minute"


def breakeven_due_at(entry, minutes):
    """Exclude the entry minute, then wait for N complete one-minute candles."""
    return int(entry)//60*60 + (minutes+1)*60


def confirmed_minute_end(data, now):
    bar = (data.get('market_1m') or {}).get('bar_c') or {}
    stamp = finite_number(bar.get('time'))
    if stamp is None or stamp % 60 or not 0 <= now-(stamp+60) <= 105:
        return None
    return stamp+60


def valid_policy(policy):
    return (isinstance(policy, dict) and type(policy.get("version")) is int and policy["version"] == 1
            and all(type(policy.get(k)) is bool for k in
                    ("timed_breakeven_enabled", "vwap_outside_only", "trailing_enabled"))
            and type(policy.get("breakeven_minutes")) is int and 1 <= policy["breakeven_minutes"] <= 120
            and policy.get("trailing_source") in ("ema9_5m", "st_1m_10_3"))


def vwap_entry_check(evidence, side, now):
    """Current observed price must be beyond the same-direction confirmed VWAP band."""
    evidence = evidence if isinstance(evidence, dict) else {}
    price = finite_number(evidence.get("ema9_price"))
    observed = finite_number(evidence.get("ema9_observed_at"))
    confirmed = finite_number(evidence.get("confirmed_at"))
    mid, sigma = finite_number(evidence.get("vwap")), finite_number(evidence.get("sigma"))
    at = datetime.fromtimestamp(now, ET)
    fresh = (observed is not None and 0 <= now-observed <= 45
             and confirmed is not None and 0 <= now-confirmed <= 120
             and at.weekday() < 5 and 570 <= at.hour*60+at.minute < 960
             and datetime.fromtimestamp(confirmed, ET).date() == at.date())
    ready = bool(fresh and price is not None and price > 0 and mid is not None and mid > 0
                 and sigma is not None and sigma > 0 and mid-sigma > 0)
    allowed = bool(ready and ((side == "long" and price > mid+sigma)
                             or (side == "short" and price < mid-sigma)))
    reason = ("" if allowed else "VWAP区间过滤：等待新鲜的确认轨道与实时价格" if not ready else
              f"VWAP区间过滤：现价{price:g}，轨道[{mid-sigma:g}, {mid+sigma:g}]，"
              + ("做多必须高于上轨" if side == "long" else "做空必须低于下轨"))
    return {"ok": allowed, "reason": reason, "price": price, "vwap": mid, "sigma": sigma,
            "confirmed_at": confirmed, "observed_at": observed}


def protection_price(data, now):
    data = data if isinstance(data, dict) else {}
    bar = data.get("bar") or {}
    stamp, observed = finite_number(bar.get("time")), finite_number(data.get("price_observed_at"))
    price = finite_number(bar.get("close"))
    if (stamp is None or stamp % 300 or not 0 <= now-stamp < 300
            or observed is None or not stamp <= observed <= now or now-observed > 45
            or price is None or price <= 0):
        return None
    return price


def confirmed_candidate_st(data, now):
    row = data.get('candidate_st_1m_c') or {}
    stamp, value = finite_number(row.get('time')), finite_number(row.get('price'))
    if (type(row.get('version')) is not int or row['version'] != 1
            or row.get('atr_period') != 10 or row.get('multiplier') != 3
            or row.get('interval_seconds') != 60 or row.get('session') not in ('regular','extended','24h')
            or isinstance(row.get('direction'), bool) or row.get('direction') not in (-1,1)
            or stamp is None or stamp % 60 or not 0 <= now-(stamp+60) < 105
            or value is None or value <= 0
            or finite_number(((data.get('market_1m') or {}).get('bar_c') or {}).get('time')) != stamp):
        return None
    return {'bar_time':stamp,'price':round(value,2),'direction':row['direction']}


def trailing_reference(data, source, side, now):
    """Only completed references; opposite ST is handled by explicit opt-in exit policy."""
    if source == "ema9_5m":
        bar, row, period = data.get("bar_c") or {}, data.get("ts_c") or {}, 300
        stamp = finite_number(bar.get("time"))
        value = finite_number(row.get("ema_9"))
        if finite_number(row.get("time")) != stamp:
            return None
    else:
        row = confirmed_candidate_st(data, now)
        if not row or row['direction'] != (1 if side=='long' else -1):
            return None
        stamp,value,period = row['bar_time'],row['price'],60
    if (stamp is None or stamp % period or not 0 <= now-(stamp+period) < period+45
            or value is None or value <= 0):
        return None
    return {"bar_time": stamp, "source": source, "price": round(value, 2)}
