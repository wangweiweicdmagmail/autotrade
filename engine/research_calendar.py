"""Research-only calendar. Unknown schedules never imply complete coverage.

Cash-equity dates verified against ICE's 2025-12-23 publication:
https://ir.theice.com/press/news-details/2025/NYSE-Group-Announces-2026-2027-and-2028-Holiday-and-Early-Closings-Calendar/
"""
from datetime import datetime, time
from zoneinfo import ZoneInfo

ET = ZoneInfo('America/New_York')
HOLIDAYS = {
    2026: '01-01 01-19 02-16 04-03 05-25 06-19 07-03 09-07 11-26 12-25',
    2027: '01-01 01-18 02-15 03-26 05-31 06-18 07-05 09-06 11-25 12-24',
    2028: '01-17 02-21 04-14 05-29 06-19 07-04 09-04 11-23 12-25',
}
EARLY = {'2026-11-27', '2026-12-24', '2027-11-26', '2028-07-03', '2028-11-24'}
VERSION = 'nyse-ice-2026-2028-v1'


def schedule(day):
    date = datetime.strptime(day, '%Y-%m-%d').date()
    if date.year not in HOLIDAYS:
        return None
    if date.weekday() >= 5 or day[5:] in HOLIDAYS[date.year].split():
        return {'closed': True, 'version': VERSION}
    stamp = lambda h, m=0: int(datetime.combine(date, time(h, m), ET).timestamp())
    return {'closed': False, 'version': VERSION, 'regular_open': stamp(9, 30),
            'regular_close': stamp(13 if day in EARLY else 16),
            'extended_open': stamp(4), 'extended_close': stamp(17 if day in EARLY else 20)}
