from datetime import datetime
from types import SimpleNamespace

from console.services.strategy_shadow import ET, StrategyShadow

from .conftest import run


def test_shadow_uses_pine_signal_and_isolated_simulation(tmp_path, monkeypatch):
    now = datetime(2026, 11, 2, 10, 0, 1, tzinfo=ET)
    monkeypatch.setattr("console.runtime.strategy.time.time", lambda: now.timestamp())
    settings = SimpleNamespace(
        _pyramid_auto_breakeven=lambda: True,
        _pyramid_breakeven_r=lambda: 3.0,
        _pyramid_add_unlock_r=lambda: 2.0,
    )
    symbol = "NASDAQ:NVDA"
    shadow = StrategyShadow(tmp_path, [symbol], settings)

    def snapshot(stamp, direction=1, price=100, signal=1):
        bar_time = int(stamp.timestamp()) // 300 * 300
        return {
            "bar": {"time": bar_time, "close": price},
            "bar_c": {"time": bar_time - 300, "close": price},
            "attack_c": {"time": bar_time - 300, "opening3High": 99.5, "opening3Low": 98},
            "price_observed_at": stamp.timestamp(),
            "e9": {
                "time": bar_time, "direction": direction, "touch": signal,
                "ema_9": 100, "long_stop": 99, "short_stop": 101,
            },
        }

    market = {symbol: snapshot(now), "NASDAQ:QQQ": snapshot(now, direction=-1)}
    run(shadow.step(market, now, stale=False))
    assert shadow.report("2026-11-02")["orders"] == []
    market["NASDAQ:QQQ"] = snapshot(now)
    run(shadow.step(market, now, stale=True))
    assert shadow.report("2026-11-02")["orders"] == []
    run(shadow.step(market, now, stale=False))
    orders = shadow.report("2026-11-02")["orders"]
    assert len(orders) == 1
    assert orders[0]["entry_order_type"] == "MKT" and orders[0]["avg_fill"] == 100

    end = now.replace(hour=15, minute=45)
    run(shadow.step({symbol: snapshot(end, price=101)}, end, stale=False))
    restarted = StrategyShadow(tmp_path, [symbol], settings)
    report = restarted.report("2026-11-02")
    assert report["orders"][0]["exit_reason"] == "shadow_eod"
    assert report["realized_pnl"] == report["orders"][0]["qty"]
