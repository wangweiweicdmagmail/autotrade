"""Manual-start backtests use only post-start bars and private simulation state."""

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from engine.backtest_session import BacktestSession
from engine.pine_models import PineModelRegistry

from .conftest import run
from .test_pine_execution import fixture_output, registry

DAY = "2026-10-30"
SYMBOL = "NASDAQ:NVDA"


def bar(hm, *, signal=1, direction=1, day=DAY):
    stamp = datetime.fromisoformat(f"{day}T{hm}:00").replace(
        tzinfo=ZoneInfo("America/New_York")
    ).timestamp()
    return {"time": stamp, "open": 100, "high": 101, "low": 99.5, "close": 100.5,
            "volume": 100, "fixture": fixture_output(stamp, direction, signal)}


def test_manual_start_skips_old_signals_and_uses_only_completed_startup_state():
    rows = [bar("09:55", signal=0, direction=-1),
            bar("10:00", direction=1), bar("10:05", signal=0)]
    session = BacktestSession(rows, SYMBOL, DAY, "fixture_pullback",
                              {"initial_risk_usd": 100}, registry=registry(),
                              start_time="10:02")
    state = run(session.run_all())
    assert state["total_bars"] == 1
    assert state["params"]["start_direction"] == -1  # 10:00 bar is not closed at 10:02
    assert state["run_context"]["first_bar_time"] == "10:05"
    assert state["validation"]["signal_bars"] == 0
    assert state["orders"] == []  # the 10:00 entry never ran
    assert state["summary"]["count"] == 0
    assert state["summary"]["win_rate"] is None


def test_signal_at_exact_start_is_executed_and_reported():
    session = BacktestSession([bar("09:55", signal=0), bar("10:00")],
                              SYMBOL, DAY, "fixture_pullback",
                              {"initial_risk_usd": 100, "equity": 100000},
                              registry=registry(), start_time="10:00")
    state = run(session.run_all())
    assert state["done"]
    assert state["validation"]["signal_bars"] == 1
    assert state["validation"]["placed_orders"] == 1
    assert state["orders"][0]["placed"]["hm"] == "10:00"
    assert state["orders"][0]["entry_order_type"] == "MKT"
    assert state["orders"][0]["filled"]["px"] == 101
    assert state["orders"][0]["qty"] == 50
    assert state["summary"]["total_pnl"] == -25
    assert state["replay_bars"][0]["hm"] == "10:00"
    assert state["replay_bars"][0]["execution_stop"] == 99
    assert len(state["run_context"]["model_revision"]) == 16
    assert len(state["run_context"]["execution_revision"]) == 16
    assert len(state["run_context"]["data_revision"]) == 16


def test_private_run_endpoint_does_not_replace_shared_session(console_app, monkeypatch):
    calls = []

    async def fetch(*args):
        calls.append(args)
        return [bar("09:55", signal=0), bar("10:00")]

    monkeypatch.setattr("backtest_trend.fetch_history", fetch)
    models = registry()
    monkeypatch.setattr(PineModelRegistry, "load", lambda: models)
    sentinel = object()
    console_app.research._bt_session = sentinel
    client = TestClient(console_app.app)
    request = {"symbol": SYMBOL, "day": DAY, "strategy": "fixture_pullback",
               "start_time": "10:00", "params": {"initial_risk_usd": 100, "equity": 100000}}
    second = client.post("/api/bt/run", json={**request, "start_time": "10:02"})
    first = client.post("/api/bt/run", json=request)
    assert first.status_code == 200
    assert first.json()["done"]
    assert first.json()["history_reused"] is True
    assert len(calls) == 1
    assert first.json()["summary"]["total_pnl"] == -25
    assert second.status_code == 400  # no complete post-start bar; not a stale result
    assert console_app.research._bt_session is sentinel


@pytest.mark.parametrize("now, expected", [
    ("2026-11-02T15:59:00", DAY),
    ("2026-11-02T16:01:00", "2026-11-02"),
    ("2026-11-03T00:01:00", "2026-11-02"),
])
def test_default_backtest_uses_latest_closed_session(console_app, monkeypatch, now, expected):
    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls.fromisoformat(now).replace(tzinfo=tz)

    async def fetch(symbol, bars, day):
        assert day == ""
        return [bar(hm, day=date) for date in (DAY, "2026-11-02") for hm in ("09:40", "09:45")]

    monkeypatch.setattr("datetime.datetime", FixedDateTime)
    monkeypatch.setattr("backtest_trend.fetch_history", fetch)
    models = registry()
    monkeypatch.setattr(PineModelRegistry, "load", lambda: models)
    response = TestClient(console_app.app).post(
        "/api/bt/run", json={"symbol": SYMBOL, "strategy": "fixture_pullback"},
    )
    assert response.status_code == 200
    assert response.json()["day"] == expected


@pytest.mark.parametrize("changes,status", [
    ({"start_time": "25:00"}, 422),
    ({"bars": 0}, 422),
    ({"day": "2026-99-01"}, 400),
    ({"symbol": " "}, 400),
    ({"strategy": "missing_model"}, 400),
    ({"params": {"initial_risk_usd": -1}}, 400),
])
def test_bad_run_inputs_fail_before_fetch(console_app, monkeypatch, changes, status):
    async def forbidden(*args):
        raise AssertionError("invalid input attempted historical fetch")

    monkeypatch.setattr("backtest_trend.fetch_history", forbidden)
    request = {"symbol": SYMBOL, "day": DAY, "start_time": "10:00", **changes}
    assert TestClient(console_app.app).post("/api/bt/run", json=request).status_code == status


def test_ema7_reuses_persistent_pine_outputs_when_start_changes(console_app, monkeypatch):
    from datetime import timedelta

    from engine import local_pine_history as local

    first_bar = datetime.fromisoformat(f"{DAY}T09:30:00").replace(tzinfo=local.ET)
    history = []
    for index in range(-523, 78):
        stamp = int((first_bar + timedelta(minutes=5 * index)).timestamp())
        price = 100 + index * 0.03
        history.append({"time": stamp, "open": price, "high": price + 0.25,
                        "low": price - 0.25, "close": price + 0.05, "volume": 100})
    dataset = {"metadata": {"timezone": "America/New_York", "session": "extended",
                            "minmove": 1, "pricescale": 100}, "bars": history}
    calls = []
    run_node = local._node

    async def node(script, *args, **kwargs):
        calls.append(script)
        if script == "tv_backtest_bars.js":
            assert args == (SYMBOL, "601", str(int(first_bar.timestamp()) + 78 * 300))
            return dataset
        return await run_node(script, *args, **kwargs)

    async def forbid_cloud(*args):
        pytest.fail("EMA7 called the legacy cloud study downloader")

    monkeypatch.setattr(local, "_node", node)
    monkeypatch.setattr("backtest_trend.fetch_history", forbid_cloud)
    client = TestClient(console_app.app)
    request = {"symbol": SYMBOL, "day": DAY, "strategy": local.EMA7_MODEL}
    first = client.post("/api/bt/run", json={**request, "start_time": "09:45"})
    assert first.status_code == 200, first.text
    assert first.json()["total_bars"] == 75
    h1 = first.json()["run_context"]["history"]
    assert h1["source"] == "pinets"
    assert h1["pine_inputs"] == {"offsetMultiplier": 0}
    assert h1["candles_reused"] is False
    assert h1["calculation_reused"] is False

    # Recreate the loader to show that a process-local cache is not required.
    store = console_app.research._local_pine_history
    console_app.research._local_pine_history = local.LocalPineHistory(store.path)
    second = client.post("/api/bt/run", json={**request, "start_time": "10:02"})
    assert second.status_code == 200, second.text
    assert second.json()["run_context"]["first_bar_time"] == "10:05"
    assert second.json()["total_bars"] == 71
    h2 = second.json()["run_context"]["history"]
    assert h2["calculation_revision"] == h1["calculation_revision"]
    assert h2["candles_reused"] and h2["calculation_reused"]
    assert second.json()["history_reused"] is True
    assert calls == ["tv_backtest_bars.js", "pinets_backtest.js"]
    overlap = [r for r in first.json()["replay_bars"] if r["hm"] >= "10:05"]
    outputs = ("entry_signal", "direction", "entry_price", "long_stop", "short_stop")
    assert [tuple(row[key] for key in outputs) for row in overlap] == [
        tuple(row[key] for key in outputs) for row in second.json()["replay_bars"]
    ]

    monkeypatch.setattr(local, "INPUTS", {"offsetMultiplier": 0.2})
    third = client.post("/api/bt/run", json={**request, "start_time": "10:02"})
    assert third.status_code == 200, third.text
    h3 = third.json()["run_context"]["history"]
    assert h3["calculation_revision"] != h2["calculation_revision"]
    assert h3["candles_reused"] and not h3["calculation_reused"]
    assert calls[-1] == "pinets_backtest.js" and calls.count("tv_backtest_bars.js") == 1


def test_ema7_rejects_incomplete_session_without_caching(console_app, monkeypatch):
    from datetime import timedelta
    from engine import local_pine_history as local

    first = datetime.fromisoformat(f"{DAY}T09:30:00").replace(tzinfo=local.ET)
    bars = [{"time": int((first + timedelta(minutes=5 * index)).timestamp()),
             "open": 100, "high": 101, "low": 99, "close": 100, "volume": 100}
            for index in range(-525, 77)]
    calls = []

    async def node(script, *args, **kwargs):
        calls.append(script)
        return {"metadata": {"timezone": "America/New_York", "session": "extended",
                             "minmove": 1, "pricescale": 100}, "bars": bars}

    monkeypatch.setattr(local, "_node", node)
    client = TestClient(console_app.app)
    request = {"symbol": SYMBOL, "day": DAY, "strategy": local.EMA7_MODEL}
    for _ in range(2):
        response = client.post("/api/bt/run", json=request)
        assert response.status_code == 400
        assert "缺少完整常规盘" in response.json()["detail"]
    assert calls == ["tv_backtest_bars.js", "tv_backtest_bars.js"]
