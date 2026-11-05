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
