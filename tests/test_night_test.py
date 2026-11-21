"""Night paper tests use only synthetic Pine snapshots and temporary report files."""

import copy
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from console.services.night_test import NightTestError, NightTestService, in_window
from engine.paper_feed import PinePaperFeed
from tests.conftest import run
from tests.test_pine_execution import fixture_output, registry

SYM = "NASDAQ:NVDA"
ET = ZoneInfo("America/New_York")


class Tasks:
    def start(self, name, factory):
        pass  # tests advance snapshots explicitly; no real background tasks


class Feed:
    def __init__(self, symbol, model, session):
        self.symbol, self.model, self.session = symbol, model, session
        self.data = {}
        self.fresh = True
        self.connected = False

    async def start(self):
        self.connected = True

    async def stop(self):
        self.connected = False

    def health(self):
        return {
            "connected": self.connected,
            "fresh": self.connected and self.fresh,
            "reason": "waiting",
            "observed_at": self.data.get("price_observed_at"),
        }


def make_paper_fixture(tmp_path, monkeypatch, hour=21):
    from console.services import night_test

    clock = [datetime(2026, 11, 2, hour, 10, tzinfo=ET).timestamp()]
    monkeypatch.setattr(night_test.time, "time", lambda: clock[0])
    service = NightTestService(
        tmp_path, Tasks(), registry=registry(), feed_factory=Feed
    )
    run(service.connect(SYM, "fixture_pullback", "24h"))
    stamp = clock[0] // 300 * 300
    service.feed.data = {
        "bar": {"time": stamp, "open": 100, "high": 105, "low": 90, "close": 100},
        "bar_c": {"time": stamp - 300, "close": 100},
        "price_observed_at": clock[0] - 1,
        "fixture": fixture_output(time=stamp),
        "fixture_c": fixture_output(time=stamp - 300),
        "session": {"requested": "24h", "resolved": "24h", "symbol": "BATS:NVDA"},
    }
    return service, clock


def tick(service, clock, price=100):
    clock[0] += 1
    service.feed.data["price_observed_at"] = clock[0]
    service.feed.data["bar"]["close"] = price
    run(service.step())


def test_manual_start_has_no_warmup_orders_and_never_replays_old_extremes(
    tmp_path, monkeypatch
):
    service, clock = make_paper_fixture(tmp_path, monkeypatch)
    run(service.start_run(100, "20:00", "04:00"))
    run(service.step())
    assert service.broker._lots == {}
    tick(service, clock)
    lot = next(iter(service.broker._lots.values()))
    assert lot.state == "ACTIVE"  # MKT fills now; the old low=90 cannot stop it.
    assert lot.entry_order_type == "MKT"
    assert lot.avg_fill == service.feed.data["bar"]["close"]
    tick(service, clock)
    assert lot.state == "ACTIVE"
    assert lot.fill_time >= service.started_at
    tick(service, clock, 98)
    assert lot.state == "DONE" and lot.exit_reason == "stop"
    assert len(service.broker.fill_events) == 2
    assert service.status()["session"]["symbol"] == "BATS:NVDA"
    assert not (tmp_path / "lots_state.json").exists()
    assert len(list((tmp_path / "paper_tests").glob("*.json"))) == 1


@pytest.mark.parametrize(
    "hm,allowed",
    [(21 * 60, True), (3 * 60, True), (4 * 60, False), (19 * 60, False), (0, True)],
)
def test_cross_midnight_entry_window(hm, allowed):
    assert in_window(hm, 20 * 60, 4 * 60) is allowed


@pytest.mark.parametrize(
    "problem",
    ["stale", "wrong_session", "unknown_session", "unaligned", "missing_confirmed"],
)
def test_start_requires_fresh_verified_and_aligned_pine_data(
    tmp_path, monkeypatch, problem
):
    service, _clock = make_paper_fixture(tmp_path, monkeypatch)
    if problem == "stale":
        service.feed.fresh = False
    elif problem == "wrong_session":
        service.feed.data["session"]["resolved"] = "extended"
    elif problem == "unknown_session":
        service.feed.data["session"]["resolved"] = None
    elif problem == "unaligned":
        service.feed.data["fixture"]["time"] -= 300
    else:
        service.feed.data["fixture_c"] = {}
    with pytest.raises(NightTestError):
        run(service.start_run(100, "20:00", "04:00"))
    assert service.engine is None
    assert not (tmp_path / "paper_tests").exists()


def test_start_direction_uses_completed_bar_and_profile_override_is_local(
    tmp_path, monkeypatch
):
    service, _clock = make_paper_fixture(tmp_path, monkeypatch)
    before = copy.deepcopy(service.model.profile)
    service.feed.data["fixture"]["bias"] = -1
    run(service.start_run(100, "20:00", "04:00"))
    assert service.engine.get_instance(SYM).start_direction == 1
    assert service.model.profile == before
    assert service.params["entry_window_override"]
    assert service.params["base_execution_profile"] == before.name


def test_heartbeat_does_not_duplicate_market_fill_or_sample(
    tmp_path, monkeypatch
):
    service, clock = make_paper_fixture(tmp_path, monkeypatch)
    run(service.start_run(100, "20:00", "04:00"))
    tick(service, clock)
    sample_count = len(service.samples)
    clock[0] += 1
    run(service.step())  # same observed quote, different runtime receipt clock
    assert len(service.samples) == sample_count
    assert next(iter(service.broker._lots.values())).state == "ACTIVE"
    assert len(service.broker.fill_events) == 1


def test_stop_protection_still_runs_when_pine_output_is_missing(tmp_path, monkeypatch):
    service, clock = make_paper_fixture(tmp_path, monkeypatch)
    run(service.start_run(100, "20:00", "04:00"))
    tick(service, clock)
    tick(service, clock)
    service.feed.data["fixture"] = {}
    service.feed.data["fixture_c"] = {}
    tick(service, clock, 98)
    assert next(iter(service.broker._lots.values())).state == "DONE"


def test_outside_window_allows_protection_but_no_new_entries(tmp_path, monkeypatch):
    service, clock = make_paper_fixture(tmp_path, monkeypatch, hour=12)
    run(service.start_run(100, "20:00", "04:00"))
    tick(service, clock)
    assert service.broker._lots == {}
    assert service.samples[-1]["entry_window_open"] is False


def test_stale_and_wrong_session_ticks_cannot_drive_orders(tmp_path, monkeypatch):
    service, clock = make_paper_fixture(tmp_path, monkeypatch)
    run(service.start_run(100, "20:00", "04:00"))
    service.feed.fresh = False
    tick(service, clock)
    assert not service.broker._lots
    service.feed.fresh = True
    service.feed.data["session"]["resolved"] = "regular"
    tick(service, clock)
    assert not service.broker._lots


def test_stop_settles_only_test_orders_and_prevents_unsettled_switch(
    tmp_path, monkeypatch
):
    service, clock = make_paper_fixture(tmp_path, monkeypatch)
    run(service.start_run(100, "20:00", "04:00"))
    tick(service, clock)
    tick(service, clock)
    with pytest.raises(NightTestError):
        run(service.connect("NASDAQ:MSFT", "fixture_pullback", "extended"))
    run(service.stop_run())
    lot = next(iter(service.broker._lots.values()))
    assert lot.state == "DONE" and lot.exit_reason == "paper_manual_stop"
    assert not service.running
    report = service.report()
    assert report["paper_only"] and report["params"]["session"] == "24h"
    assert report["started_at"] and report["stopped_at"]
    run(service.disconnect())
    assert not service.feed.connected
    run(service.connect("NASDAQ:MSFT", "fixture_pullback", "extended"))
    assert service.run_id is None and service.broker is None


def test_new_confirmed_bar_is_not_consumed_on_mismatched_pine_timestamp(
    tmp_path, monkeypatch
):
    service, clock = make_paper_fixture(tmp_path, monkeypatch)
    run(service.start_run(100, "20:00", "04:00"))
    previous = service.last_bar
    service.feed.data["bar"]["time"] += 300
    service.feed.data["bar_c"]["time"] += 300
    service.feed.data["fixture"]["time"] += 300
    tick(service, clock)
    assert service.last_bar == previous
    service.feed.data["fixture_c"]["time"] += 300
    tick(service, clock)
    assert service.last_bar == previous + 300


def test_feed_freshness_checks_observation_and_bar_not_just_heartbeat():
    feed = PinePaperFeed(SYM, "ema9_pyramid", "24h")
    feed.process = SimpleNamespace(returncode=None)
    feed.received_at = 1000
    feed.data = {"bar": {"time": 900, "close": 100}, "price_observed_at": 999}
    assert feed.health(1000)["fresh"]
    feed.data["price_observed_at"] = 950
    assert not feed.health(1000)["fresh"]
    feed.data["price_observed_at"] = 999
    feed.data["bar"]["time"] = 300
    assert not feed.health(1000)["fresh"]
    feed.data["bar"]["time"] = 900
    feed.fault = True
    assert not feed.health(1000)["fresh"]


def test_night_endpoints_validate_and_are_separate_from_production(console_app):
    client = TestClient(console_app.app)
    assert client.get("/api/night-test/status").json()["running"] is False
    response = client.post("/api/night-test/start", json={"risk_budget": 100})
    assert response.status_code == 403
    from console.dependencies import require_trader

    console_app.app.dependency_overrides[require_trader] = lambda: "master"
    assert (
        client.post("/api/night-test/start", json={"risk_budget": 100}).status_code
        == 409
    )
    response = client.post(
        "/api/night-test/connect", json={"symbol": SYM, "session": "24x7"}
    )
    assert response.status_code == 422
    assert client.get("/api/night-test/report").json()["paper_only"]
    assert console_app.order_mgr.list_lots() == []
    assert console_app.auto_service.instances() == []


def test_dedicated_feed_closes_its_subprocess_and_io_tasks(monkeypatch):
    import asyncio

    from engine import paper_feed

    async def scenario():
        commands = []

        class Process:
            returncode = None
            stdout = asyncio.StreamReader()
            stderr = asyncio.StreamReader()
            terminated = False

            def terminate(self):
                self.terminated = True

            async def wait(self):
                self.returncode = 0
                return 0

        process = Process()

        async def spawn(*args, **kwargs):
            commands.append(args)
            return process

        monkeypatch.setattr(paper_feed.asyncio, "create_subprocess_exec", spawn)
        feed = PinePaperFeed(SYM, "ema9_pyramid", "24h")
        await feed.start()
        assert "pine_paper_daemon.js" in commands[0][1]
        assert commands[0][-2:] == ("--session", "24h")
        tasks = list(feed.tasks)
        await feed.stop()
        assert process.terminated and process.returncode == 0
        assert feed.tasks == [] and all(task.done() for task in tasks)

    run(scenario())
