"""Current rules: real Pine values, mirrored shorts, time and data boundaries."""
from copy import deepcopy
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from engine.opening_breakout import CURRENT_RULES, RULES_VERSION, evaluate_breakout, entry_window

T = int(datetime(2026, 11, 4, 9, 50, tzinfo=ZoneInfo('America/New_York')).timestamp())


def candle(side='long', time=T):
    bar = dict(time=time, open=102, high=106.5, low=101.5, close=106,
               atr_sma10=2, body_ema10=1, prior_engulfing4=0,
               context_time=time, previous_time=time-300, previous_high=103, previous_low=101,
               ema_slope_atr=2, ema_slope_time=time,
               ema7_previous3=[dict(time=time-k*300, open=102, close=103, ema7=101) for k in range(1,4)])
    if side == 'short':
        for a,b in [('open','open'),('close','close'),('high','low'),('low','high'),
                    ('previous_high','previous_low'),('previous_low','previous_high')]:
            bar[a] = 200 - candle('long', time)[b]
        bar['ema_slope_atr'] = -2
        for row in bar['ema7_previous3']:
            for key in ('open','close','ema7'):
                row[key] = 200-row[key]
    return bar


def evaluate(bar):
    return evaluate_breakout('TEST', bar, {}, (102,98), CURRENT_RULES)


@pytest.mark.parametrize('side', ['long','short'])
def test_current_pass_midpoint_and_signed_slope(side):
    bar = candle(side)
    signal = evaluate(bar)[1]
    assert signal['side'] == side
    assert signal['rules_version'] == RULES_VERSION
    assert signal['stop_px'] == (104 if side == 'long' else 96)
    bar['ema_slope_atr'] = 1.5 if side == 'long' else -1.5
    assert evaluate(bar)[1] is None
    bar['ema_slope_atr'] = -2 if side == 'long' else 2
    assert evaluate(bar)[1] is None


@pytest.mark.parametrize('field', ['ema_slope_time','context_time','previous_time'])
def test_missing_or_misaligned_pine_context_waits(field):
    bar = candle()
    bar[field] -= 300
    assert evaluate(bar) is None


def test_each_previous_candle_uses_own_pine_ema_and_touch_is_allowed():
    bar = candle()
    bar['ema7_previous3'][1]['ema7'] = 102.5
    assert evaluate(bar)[1] is None
    bar['ema7_previous3'][1]['ema7'] = 102
    assert evaluate(bar)[1] is not None
    bar['ema7_previous3'][1]['ema7'] = None
    assert evaluate(bar) is None


def test_wick_20_percent_equality_and_strict_midpoint():
    bar = candle()
    bar.update(high=107, low=102)  # upper 1 / range 5 = 20%
    assert evaluate(bar)[1] is not None
    bar['high'] = 107.001
    assert evaluate(bar)[1] is None
    bar = candle()
    bar['previous_high'] = 104
    assert evaluate(bar)[1] is None


def test_last_signal_1020_and_actual_submission_before1030_only():
    assert evaluate(candle(time=T+30*60))[1] is not None
    assert evaluate(candle(time=T+35*60))[1] is None
    assert entry_window(T+40*60-1)
    assert not entry_window(T+40*60)


def test_live_first_signal_once_and_history_rebuild(console_app, monkeypatch):
    service = console_app.signals
    monkeypatch.setattr(console_app.events, 'record_event', lambda *a,**k: None)
    service._opening_breakout_day = None
    for time in [T,T+300]:
        bar = candle(time=time)
        data = dict(bar=dict(time=time+300), bar_c=bar,
                    attack_c=dict(time=time,opening3High=102,opening3Low=98))
        service._scan_opening_breakouts({'TEST':data},time+300)
    assert len(service.opening_breakouts(T+600)) == 1
    assert service.opening_breakouts(T+600)[0]['bar_time'] == T


@pytest.mark.parametrize('outcome', ['confirmed','invalid','unarmed','late'])
def test_current_preview_reservation_requires_final_full_rules(console_app,monkeypatch,outcome):
    import asyncio
    from console.schemas import OpeningBreakoutPrepareRequest
    service=console_app.signals
    service._opening_breakout_day=None
    now=[T+120]
    calls=[]
    monkeypatch.setattr('console.services.signals.time.time',lambda:now[0])
    monkeypatch.setattr(console_app.events,'record_event',lambda *a,**k:None)
    async def submit(req,trader,*,deadline):
        calls.append((req.signal_id,deadline))
    monkeypatch.setattr(console_app.trading,'api_place_opening_breakout',submit)
    previous=dict(time=T-300,open=102,close=102,high=103,low=101,
                  atr_sma10=2,body_ema10=1,prior_engulfing4=0)
    data=dict(bar=candle(),bar_c=previous,attack_c=dict(time=T-300,opening3High=102,opening3Low=98))
    market={'TEST':data}
    monkeypatch.setattr(console_app.collector,'latest',lambda:market)
    async def update():
        service._scan_opening_breakouts(market,now[0])
        await service._update_opening_previews(market,now[0])
    asyncio.run(update())
    preview,=service.opening_breakout_display(now[0])
    assert preview['stop_px']==104
    if outcome!='unarmed':
        service.prepare_opening_breakout(OpeningBreakoutPrepareRequest(signal_id=preview['id'],side='long'),'master')
    assert calls==[]
    data['bar_c']=candle()
    if outcome=='invalid':
        data['bar_c']['ema_slope_atr']=1.5
    data['bar']={'time':T+300,'close':107}
    data['attack_c']['time']=T
    now[0]=T+300+(21 if outcome=='late' else 1)
    asyncio.run(update())
    asyncio.run(update())
    assert len(calls)==(1 if outcome=='confirmed' else 0)
