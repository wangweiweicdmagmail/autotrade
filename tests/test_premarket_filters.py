from console import schemas
"""盘前区间过滤开关回归（console/app.py）。

守的口径：screen_premarket_filter / fvg_premarket_filter 两个独立开关
（settings 持久化，选股默认关、FVG默认开）；开关开时多头要求现价 > attack_c.pmh、空头要求
现价 < attack_c.pml（严格不等）；attack 无数据/非法区间/现价缺失一律保留
（不误杀）；两开关互不影响；/api/settings 部分更新/持久化/空请求 400/
越界 400。现价 = collector 实时 bar.close。
"""
import json
import pytest
T = 1792764000

def mk(direction, close, pmh=None, pml=None):
    d = {'ts_c': {'direction': direction, 'trend_state': direction, 'divergence': 1.0, 'atr': 1.0}, 'bar': {'time': T, 'close': close}, 'bar_c': {'time': T - 300, 'close': close}, 'fvg_c': {}}
    if pmh is not None:
        d['attack_c'] = {'time': T - 300, 'pmh': pmh, 'pml': pml}
    return d
MARKET = {'NASDAQ:UP_OK': mk(1, 105.0, 100.0, 90.0), 'NASDAQ:UP_NO': mk(1, 95.0, 100.0, 90.0), 'NASDAQ:UP_EQ': mk(1, 100.0, 100.0, 90.0), 'NASDAQ:UP_NA': mk(1, 95.0), 'NASDAQ:DN_OK': mk(-1, 85.0, 100.0, 90.0), 'NASDAQ:DN_NO': mk(-1, 95.0, 100.0, 90.0), 'NASDAQ:DN_NA': mk(-1, 95.0)}

@pytest.fixture
def app(console_app, monkeypatch):
    console_app.settings._settings.pop('screen_premarket_filter', None)
    console_app.settings._settings.pop('fvg_premarket_filter', None)
    monkeypatch.setattr(console_app.collector, 'latest', lambda: MARKET)
    console_app.signals._fvg_events.clear()
    console_app.signals._fvg_retests.clear()
    console_app.signals._fvg_warnings.clear()
    return console_app

def tickers(rows):
    return sorted((r['ticker'] for r in rows))

def test_screen_off_unchanged(app):
    """开关关 = 行为不变（全量候选）。"""
    s = app.signals.compute_screen()
    assert tickers(s['long']) == ['UP_EQ', 'UP_NA', 'UP_NO', 'UP_OK']
    assert tickers(s['short']) == ['DN_NA', 'DN_NO', 'DN_OK']

def test_screen_on_filters_and_keeps_na(app):
    """开关开 = 不满足剔除（含恰好相等）/ 无数据保留 / 行带 pmh/pml。"""
    app.settings._settings['screen_premarket_filter'] = True
    s = app.signals.compute_screen()
    assert tickers(s['long']) == ['UP_NA', 'UP_OK']
    assert tickers(s['short']) == ['DN_NA', 'DN_OK']
    row = next((r for r in s['long'] if r['ticker'] == 'UP_OK'))
    assert row['pmh'] == 100.0 and row['pml'] == 90.0
    row_na = next((r for r in s['long'] if r['ticker'] == 'UP_NA'))
    assert row_na['pmh'] is None and row_na['pml'] is None

def _ev(sym, side, bt):
    return {'id': f'{sym}:{bt}:{side}', 'symbol': sym, 'ticker': sym.split(':')[-1], 'side': side, 'bar_time': bt, 'top': 101.0, 'bottom': 100.0, 'height': 1.0, 'atr': 1.0, 'ratio': 1.0}

def _seed_fvg_buffers(app):
    for sym, side in (('NASDAQ:UP_OK', 'long'), ('NASDAQ:UP_NO', 'long'), ('NASDAQ:DN_OK', 'short'), ('NASDAQ:DN_NO', 'short'), ('NASDAQ:UP_NA', 'long')):
        app.signals._fvg_events.append(_ev(sym, side, T - 300))
        app.signals._fvg_retests.append({**_ev(sym, side, T - 300), 'retest_time': T, 'retest_px': 100.5})
        app.signals._fvg_warnings.append({**_ev(sym, side, T - 300), 'warn_time': 1700000000})

def test_fvg_off_unchanged(app):
    """FVG 开关关 = 事件/回踩/预警全量；screen 开关不影响 FVG。"""
    app.settings._settings['fvg_premarket_filter'] = False
    _seed_fvg_buffers(app)
    snap = app.signals._fvg_monitor_snapshot()
    slot_events = [e for sl in snap['slots'] for e in sl['long'] + sl['short']]
    assert len(slot_events) == 5 and len(snap['retests']) == 5 and (len(snap['warnings']) == 5)

def test_fvg_on_filters_all_channels(app):
    """FVG 开关开 = 槽位事件/首次回踩/预警同口径过滤，无数据保留。"""
    _seed_fvg_buffers(app)
    app.settings._settings['fvg_premarket_filter'] = True
    snap = app.signals._fvg_monitor_snapshot()
    slot_events = [e for sl in snap['slots'] for e in sl['long'] + sl['short']]
    kept = sorted(((e['ticker'], e['side']) for e in slot_events))
    assert kept == [('DN_OK', 'short'), ('UP_NA', 'long'), ('UP_OK', 'long')]
    assert sorted((r['ticker'] for r in snap['retests'])) == ['DN_OK', 'UP_NA', 'UP_OK']
    assert sorted((w['ticker'] for w in snap['warnings'])) == ['DN_OK', 'UP_NA', 'UP_OK']

def test_switches_independent(app):
    """两开关互不影响：fvg 开 + screen 关 → 选股仍全量。"""
    app.settings._settings['fvg_premarket_filter'] = True
    app.settings._settings['screen_premarket_filter'] = False
    s = app.signals.compute_screen()
    assert len(s['long']) == 4 and len(s['short']) == 3

def test_settings_endpoint_partial_update_and_validation(app, tmp_path):
    """/api/settings：部分更新 / 落盘（tmp）/ 空请求400 / 越界400 / GET 全量键。"""
    from .conftest import run
    app.settings._settings.clear()
    app.settings._settings.update({'stop_atr_mult': 1.5})
    resp = run(app.settings.api_update_settings(schemas.SettingsRequest(screen_premarket_filter=True), trader='master'))
    assert resp['ok'] and resp['screen_premarket_filter'] is True
    assert resp['fvg_premarket_filter'] is True
    assert resp['stop_atr_mult'] == 1.5
    on_disk = json.loads(app.settings._SETTINGS_PATH.read_text(encoding='utf-8'))
    assert on_disk['screen_premarket_filter'] is True
    assert on_disk['stop_atr_mult'] == 1.5
    resp = run(app.settings.api_update_settings(schemas.SettingsRequest(stop_atr_mult=2.0), trader='master'))
    assert resp['screen_premarket_filter'] is True
    with pytest.raises(app.HTTPException) as ei:
        run(app.settings.api_update_settings(schemas.SettingsRequest(), trader='master'))
    assert ei.value.status_code == 400
    with pytest.raises(app.HTTPException) as ei:
        run(app.settings.api_update_settings(schemas.SettingsRequest(stop_atr_mult=99.0), trader='master'))
    assert ei.value.status_code == 400
    got = run(app.settings.api_get_settings())
    assert set(got) == {'stop_atr_mult', 'screen_premarket_filter', 'screen_opening_range_filter', 'fvg_premarket_filter', 'imessage_recipient', 'pyramid_auto_breakeven', 'pyramid_breakeven_r', 'pyramid_add_unlock_r', 'candidate_order_protection'}
    assert got['imessage_recipient'] == ''
    assert got['pyramid_auto_breakeven'] is True
    assert got['pyramid_breakeven_r'] == 3.0
    assert got['pyramid_add_unlock_r'] == 2.0
    resp = run(app.settings.api_update_settings(schemas.SettingsRequest(imessage_recipient=' +8613800000000 '), trader='master'))
    assert resp['imessage_recipient'] == '+8613800000000'
    on_disk = json.loads(app.settings._SETTINGS_PATH.read_text(encoding='utf-8'))
    assert on_disk['imessage_recipient'] == '+8613800000000'
    resp = run(app.settings.api_update_settings(schemas.SettingsRequest(imessage_recipient=''), trader='master'))
    assert resp['imessage_recipient'] == ''
    on_disk = json.loads(app.settings._SETTINGS_PATH.read_text(encoding='utf-8'))
    assert 'imessage_recipient' not in on_disk

def test_defaults_screen_off_fvg_on_and_explicit_off_preserved(app):
    """选股默认关，FVG默认开；用户显式关闭仍保留。"""
    assert app.settings._screen_premarket_filter() is False
    assert app.settings._fvg_premarket_filter() is True
    app.settings._settings['fvg_premarket_filter'] = False
    assert app.settings._fvg_premarket_filter() is False


def test_opening_range_screen_requires_aligned_close_and_live_breakout(app, monkeypatch):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    t = datetime(2026, 11, 3, 9, 50, tzinfo=ZoneInfo("America/New_York")).timestamp()
    monkeypatch.setattr("console.services.signals.time.time", lambda: t + 5)

    def row(direction, live, closed, *, offset=0, current=t):
        data = mk(direction, live)
        data["bar"]["time"] = current
        data["bar_c"].update(time=current - 300, close=closed)
        data["attack_c"] = {
            "time": current - 300 + offset, "opening3High": 101, "opening3Low": 99,
        }
        return data

    market = {
        "NASDAQ:UP": row(1, 103, 102),
        "NASDAQ:DOWN": row(-1, 98, 98),
        "NASDAQ:REENTER": row(1, 101, 102),
        "NASDAQ:UNCONFIRMED": row(1, 102, 101),
        "NASDAQ:MISALIGNED": row(1, 103, 102, offset=-300),
        "NASDAQ:STALE": row(1, 103, 102, current=t - 300),
        "NASDAQ:MISSING": mk(1, 103),
    }
    monkeypatch.setattr(app.collector, "latest", lambda: market)
    app.settings._settings["screen_opening_range_filter"] = True
    screen = app.signals.compute_screen()
    assert tickers(screen["long"]) == ["UP"]
    assert tickers(screen["short"]) == ["DOWN"]


def test_momentum_ranks_closed_ema_moves_and_keeps_live_status_separate(app, monkeypatch):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    opened = datetime(2026, 11, 3, 10, 0, tzinfo=ZoneInfo("America/New_York")).timestamp()
    confirmed = opened - 300
    monkeypatch.setattr("console.services.signals.time.time", lambda: opened + 5)

    def row(values, live, *, offset=0):
        return {
            "bar": {"time": opened, "close": live},
            "bar_c": {"time": confirmed},
            "attack_c": {"time": confirmed, "opening3High": 102, "opening3Low": 98},
            "ema7_closed": [
                {"time": confirmed - index * 300 + offset, "ema": value}
                for index, value in enumerate(values)
            ],
        }

    market = {
        "NASDAQ:FAR": row([108, 108, 107, 107], 108),
        "NASDAQ:FAST": row([104, 103, 102, 101], 101),
        "NASDAQ:DOWN": row([96, 97, 98, 99], 95),
        "NASDAQ:REVERSE": row([103, 104, 104, 105], 104),
        "NASDAQ:STALE": row([106, 105, 104, 103], 107, offset=-300),
    }
    for symbol, direction in (("NASDAQ:FAST", 1), ("NASDAQ:DOWN", -1)):
        history = market[symbol]["ema7_closed"]
        history.extend({"time": confirmed - 300 * index, "ema": 100} for index in range(4, 20))
        for index, item in enumerate(history):
            item["close"] = item["ema"] + direction * (1 if index < 15 else 0)
    market["NASDAQ:DOWN"]["ema7_closed"][-1]["time"] -= 300
    monkeypatch.setattr(app.collector, "latest", lambda: market)
    result = app.signals.compute_momentum()
    assert result["ready_count"] == 4
    assert [r["ticker"] for r in result["long"]] == ["FAR", "FAST", "REVERSE"]
    assert result["long"][0]["breakout_distance"] == 3
    assert result["first_breaks"]["long"][0]["ticker"] == "FAR"
    assert result["first_breaks"]["long"][0]["time"] == opened - 900
    assert result["long"][1]["first_break_time"] == opened - 300
    assert result["long"][1]["position"] == 2
    assert result["long"][1]["momentum"] == 1.5
    assert result["long"][1]["outside"] is False
    assert result["short"][0]["momentum"] == -1.5
    assert result["long"][1]["ema7_success_count"] == 15
    assert result["short"][0]["ema7_success_count"] is None


def test_momentum_ranks_at_opening_range_lock_using_premarket_ema(app, monkeypatch):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    locked_at = datetime(2026, 11, 3, 9, 45, tzinfo=ZoneInfo("America/New_York")).timestamp()
    confirmed = locked_at - 300

    def row(values):
        return {
            "bar": {"time": locked_at, "close": 104},
            "bar_c": {"time": confirmed},
            "attack_c": {"time": confirmed, "opening3High": 102, "opening3Low": 98},
            "ema7_closed": [
                {"time": confirmed - index * 300, "ema": ema}
                for index, ema in enumerate(values)
            ],
        }

    market = {
        "NASDAQ:WARM": row([104, 102, 101, 100]),
        "NASDAQ:GAP": row([104, 102, None, 100]),
        "NASDAQ:FAR": row([106]),
        "NASDAQ:UPPER": row([101.5]),
        "NASDAQ:MID": row([100]),
        "NASDAQ:LOWER": row([98.5]),
        "NASDAQ:SHORT": row([97]),
    }
    monkeypatch.setattr(app.collector, "latest", lambda: market)
    monkeypatch.setattr("console.services.signals.time.time", lambda: locked_at - 1)
    assert app.signals.compute_momentum()["ready_count"] == 0

    monkeypatch.setattr("console.services.signals.time.time", lambda: locked_at)
    result = app.signals.compute_momentum()
    assert result["ready_count"] == 7
    assert [r["ticker"] for r in result["long"]] == ["FAR", "WARM", "GAP", "UPPER", "MID"]
    assert [r["momentum"] for r in result["long"][:3]] == [None, 2, None]
    assert [r["ticker"] for r in result["short"]] == ["SHORT", "LOWER"]
    assert [r["breakout_distance"] for r in result["long"][-2:]] == [-0.25, -1]
    assert [r["breakout_distance"] for r in result["short"]] == [0.5, -0.25]
    assert [r["first_break_time"] for r in result["long"]] == [locked_at] * 3 + [None, None]
    assert [r["ticker"] for r in result["first_breaks"]["short"]] == ["SHORT"]


def test_opening_range_breakout_waits_for_aligned_atr_and_rearms(app, monkeypatch, legacy_breakout):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    close_time = datetime(2026, 11, 3, 9, 45, tzinfo=ZoneInfo("America/New_York")).timestamp()
    events = []
    monkeypatch.setattr(app.events, "record_event", lambda kind, **row: events.append((kind, row)))

    def scan(minutes, close, *, body=3, low=100, high=110, atr_offset=0,
             live_close=120, wilder=2, sma=None):
        current = close_time + minutes * 60
        confirmed = current - 300
        app.signals._scan_opening_breakouts({"NASDAQ:QQQ": {
            "bar": {"time": current, "close": live_close},
            "bar_c": {"time": confirmed, "open": close - body, "close": close,
                      "low": low, "high": high, "atr_sma10": sma, "body_ema10": 2, "prior_engulfing4": 0},
            "attack_c": {"time": confirmed, "opening3High": 102, "opening3Low": 98},
            "ts_c": {"time": confirmed + atr_offset, "atr": wilder},
        }}, current)

    scan(-5, 110)
    scan(0, 103.39)
    scan(5, 104, body=2)
    assert events == []
    scan(10, 108, low=104.01)
    assert events == []
    scan(15, 104, atr_offset=-300)
    assert events == []
    scan(15, 103.4, wilder=3, sma=2)
    scan(15, 103.4)
    scan(20, 106, body=1)
    scan(25, 105)
    scan(30, 103.39)
    scan(35, 104.5)
    scan(40, 92, body=-3, low=91, high=95.99)
    assert len(events) == 2
    scan(45, 96.6, body=-3, low=95, high=100, sma=3)
    scan(50, 100, low=96)
    scan(55, 104, body=-3, low=104)
    scan(60, 96, low=92, high=96)
    assert [kind for kind, _ in events] == ["opening_range_breakout"] * 3
    assert [row["side"] for _, row in events] == ["long", "long", "short"]
    assert [row["threshold"] for _, row in events] == pytest.approx([103.4, 103.4, 96.6])
    assert [row["distance_atr"] for _, row in events] == pytest.approx([0.7, 1.25, 0.7])
    assert [row["body_atr"] for _, row in events] == [1.5] * 3
    assert [row["atr"] for _, row in events] == [2, 2, 2]
    assert app.signals.opening_breakouts(close_time + 45 * 60)[0]["time"] == close_time + 45 * 60
    assert app.signals.opening_breakouts(close_time + 86400) == []


def test_opening_breakout_backfills_in_order_after_missing_bar(app, monkeypatch, legacy_breakout):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    first = int(datetime(2026, 11, 3, 9, 40, tzinfo=ZoneInfo("America/New_York")).timestamp())
    symbol = "NASDAQ:QQQ"
    events = []
    monkeypatch.setattr(app.events, "record_event", lambda kind, **row: events.append(row["id"]))

    def row(offset, close):
        return [first + offset * 300, close - 3, close, close - 3, close, 2, 3, 102, 98, None, 2, 0]

    now = first + 900
    market = {symbol: {"opening_breakout_history": [row(2, 104), row(0, 104)]}}
    app.signals._scan_opening_breakouts(market, now)
    assert [r["time"] for r in app.signals.opening_breakouts(now)] == [first + 300]
    assert app.signals.opening_breakout_coverage([symbol], now)["pending"] == [symbol]

    market[symbol]["opening_breakout_history"].append(row(1, 103))
    app.signals._scan_opening_breakouts(market, now)
    app.signals._scan_opening_breakouts(market, now + 1)
    expected = [first + 900, first + 300]
    assert [r["time"] for r in app.signals.opening_breakouts(now)] == expected
    assert len(events) == len(set(events)) == 2
    assert app.signals.opening_breakout_coverage([symbol], now)["pending"] == []

    restarted = type(app.signals)(app.signals.context)
    restarted._scan_opening_breakouts(market, now)
    assert [r["time"] for r in restarted.opening_breakouts(now)] == expected


@pytest.mark.parametrize("outcome", ["confirmed", "invalid", "engulfed", "late", "cancel", "unarmed"])
def test_opening_breakout_preview_only_submits_authorized_confirmed_candle(app, monkeypatch, outcome, legacy_breakout):
    import asyncio
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from fastapi import HTTPException
    from console.schemas import OpeningBreakoutPrepareRequest

    start = int(datetime(2026, 11, 3, 9, 45, tzinfo=ZoneInfo("America/New_York")).timestamp())
    end = start + 300
    now = start + 120
    symbol = "NASDAQ:QQQ"
    calls = []
    monkeypatch.setattr("console.services.signals.time.time", lambda: now)
    monkeypatch.setattr(app.events, "record_event", lambda *args, **kwargs: None)

    async def submit(req, trader, *, deadline):
        calls.append((req.signal_id, trader, deadline))

    monkeypatch.setattr(app.trading, "api_place_opening_breakout", submit)
    market = {symbol: {
        "bar": {"time": start, "open": 101, "high": 105, "low": 101, "close": 105, "atr_sma10": 2, "body_ema10": 2, "prior_engulfing4": 0},
        "bar_c": {"time": start - 300, "open": 101, "high": 102, "low": 100, "close": 101, "atr_sma10": 2, "body_ema10": 2, "prior_engulfing4": 0},
        "attack_c": {"time": start - 300, "opening3High": 102, "opening3Low": 98},
    }}
    monkeypatch.setattr(app.collector, "latest", lambda: market)

    async def update():
        app.signals._scan_opening_breakouts(market, now)
        await app.signals._update_opening_previews(market, now)

    asyncio.run(update())
    preview, = app.signals.opening_breakout_display(now)
    assert preview["status"] == "preview"
    assert preview["bar_time"] == start
    assert preview["confirmed_at"] == end
    assert preview["time"] == end
    assert app.signals.opening_breakouts(now) == []
    with pytest.raises(HTTPException):
        app.signals.prepare_opening_breakout(
            OpeningBreakoutPrepareRequest(signal_id=preview["id"], side="short"), "master"
        )
    req = OpeningBreakoutPrepareRequest(signal_id=preview["id"], side="long")
    if outcome != "unarmed":
        assert app.signals.prepare_opening_breakout(req, "master")["status"] == "armed"
    if outcome == "cancel":
        assert app.signals.prepare_opening_breakout(
            OpeningBreakoutPrepareRequest(signal_id=preview["id"], side="long", cancel=True), "master"
        )["status"] == "cancelled"
    asyncio.run(update())
    assert calls == []

    market[symbol]["bar_c"] = dict(market[symbol]["bar"], low=100.5)
    if outcome == "invalid":
        market[symbol]["bar_c"]["close"] = 101
    if outcome == "engulfed":
        market[symbol]["bar_c"]["prior_engulfing4"] = 2
    market[symbol]["attack_c"]["time"] = start
    market[symbol]["bar"] = {"time": end, "close": 104}
    now = end + (21 if outcome == "late" else 1)
    asyncio.run(update())
    asyncio.run(update())
    result, = app.signals.opening_breakout_display(now)
    expected = {"confirmed": "submitted", "invalid": "invalid", "engulfed": "invalid", "late": "invalid",
                "cancel": "confirmed", "unarmed": "confirmed"}
    assert result["status"] == expected[outcome]
    assert calls == ([(preview["id"], "master", end + 20)] if outcome == "confirmed" else [])
    if outcome == "confirmed":
        assert result["stop_px"] == 101


@pytest.mark.parametrize("side", ["long", "short"])
def test_breakout_body_compares_prior_body_ema_instead_of_atr(side):
    from engine.opening_breakout import evaluate_breakout

    sign = 1 if side == "long" else -1
    close = 103.4 if sign == 1 else 96.6
    bar = dict(time=300, open=close-sign*0.54, close=close,
               high=max(close, close-sign*0.54)+0.01,
               low=min(close, close-sign*0.54)-0.01,
               atr_sma10=2, body_ema10=0.2, prior_engulfing4=0)
    # Keep the relevant extreme near its opening-range edge.
    bar["low" if sign == 1 else "high"] = 102 if sign == 1 else 98
    direction, signal = evaluate_breakout("TEST", bar, {}, (102, 98))
    assert direction == side
    assert signal is not None
    assert signal["stop_px"] == round(bar["open"], 2)
    assert signal["body_atr"] < 1
    assert signal["body_ema_ratio"] == pytest.approx(2.7)
    bar["body_ema10"] = abs(close-bar["open"])
    assert evaluate_breakout("TEST", bar, {}, (102, 98))[1] is None
    del bar["body_ema10"]
    assert evaluate_breakout("TEST", bar, {}, (102, 98)) is None


@pytest.mark.parametrize("side,mask,allowed", [
    ("long", 0, True), ("long", 1, True), ("long", 2, False), ("long", 3, False),
    ("short", 0, True), ("short", 2, True), ("short", 1, False), ("short", 3, False),
])
def test_breakout_filters_only_opposite_regular_engulfing(side, mask, allowed):
    from engine.opening_breakout import evaluate_breakout

    close = 104 if side == "long" else 96
    bar = dict(time=300, open=101 if side == "long" else 99, close=close,
               low=101 if side == "long" else 95, high=105 if side == "long" else 99,
               atr_sma10=2, body_ema10=1, prior_engulfing4=mask)
    result = evaluate_breakout("TEST", bar, {}, (102, 98))
    assert (result[1] is not None) == allowed
    bar["prior_engulfing4"] = None
    assert evaluate_breakout("TEST", bar, {}, (102, 98)) is None


@pytest.fixture
def legacy_breakout(monkeypatch):
    """Archived rearm behavior stays reproducible under the explicit research policy."""
    from engine.opening_breakout import BASE_RULES
    from console.services.signals import SignalsService
    monkeypatch.setattr(SignalsService, "_breakout_policy", BASE_RULES)
