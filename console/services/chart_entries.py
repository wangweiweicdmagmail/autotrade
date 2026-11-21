"""Display-only position segments; prices come from the execution ledger."""
import math
from datetime import datetime
from zoneinfo import ZoneInfo


def entry_markers(lots, *, now, limit=200):
    result = []
    for lot in lots:
        if lot.get('state') not in ('ACTIVE', 'CLOSING', 'PENDING'):
            continue
        qty = lot.get('filled_qty') or 0
        price = lot.get('avg_fill') or 0
        stop = lot.get('stop_px') or 0
        stamp = lot.get('entry_broker_time') or lot.get('entry_time') or 0
        if (not all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and v > 0
                    for v in (qty, price, stop, stamp)) or stamp > now
                or lot.get('side') not in ('long', 'short') or not lot.get('lot_id')
                or not isinstance(lot.get('symbol'), str)):
            continue
        end = datetime.fromtimestamp(stamp, ZoneInfo('America/New_York')).replace(
            hour=16, minute=0, second=0, microsecond=0).timestamp()
        if stamp >= end:
            continue
        result.append({'id': lot['lot_id'], 'symbol': lot['symbol'], 'side': lot['side'],
                       'time': stamp, 'price': price, 'stop': stop, 'qty': qty, 'session_end': end})
    return sorted(result, key=lambda e: (e['time'], e['id']))[-limit:]
