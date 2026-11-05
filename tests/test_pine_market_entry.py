"""Pine-triggered MKT entries: in-memory gateway, temporary ledger, no TV/TWS."""

import pytest

import config
from engine.brokers import LiveBroker, SimBroker
from engine.pine_execution import PineExecutionEngine
from execution.order_lot import OrderLot, OrderManager
from execution.risk import RiskConfig, RiskGate
from .conftest import run
from .test_pine_execution import fixture_model, fixture_output


class Gateway:
    enabled = False
    _connected = False
    supports_order_refs = True

    def __init__(self):
        self.calls = []

    async def realized_pnl(self):
        return 0

    async def place_market_with_stop(self, symbol, side, qty, stop_px, **refs):
        self.calls.append(("MKT", symbol, side, qty, stop_px, refs))
        return {"ok": True, "dry_run": True, "entry_order_id": 11, "stop_order_id": 12}

    async def place_limit_with_stop(self, symbol, side, qty, limit_px, stop_px, **refs):
        self.calls.append(("LMT", symbol, side, qty, stop_px, refs))
        return {"ok": True, "dry_run": True, "entry_order_id": 21, "stop_order_id": 22}


def manager_at(tmp_path):
    gateway = Gateway()
    manager = OrderManager(gateway, RiskGate(RiskConfig(max_order_qty=1000, max_open_positions=0),
                                            halt_dir=tmp_path), tmp_path / "orders.json")
    return manager, gateway


@pytest.mark.parametrize("direction,price,stop", [(1, 101, 99), (-1, 99, 101)])
def test_pine_market_entry_when_price_has_moved_away(tmp_path, monkeypatch, direction, price, stop):
    # Explicit automatic MKT must not depend on the manual-order global switch.
    monkeypatch.setattr(config, "MARKET_ORDER_STRICT", False)
    manager, gateway = manager_at(tmp_path)
    model = fixture_model()
    engine = PineExecutionEngine(LiveBroker(manager), model=model)
    symbol = "NASDAQ:GOOGL"
    engine.start([symbol], 100)
    run(engine.on_price(symbol, price, fixture_output(direction=direction), 600))
    assert len(gateway.calls) == 1 and gateway.calls[0][0] == "MKT"
    lot = manager.list_lots()[0]
    assert lot["limit_type"] == "auto_fixture_pullback"
    assert lot["entry_order_type"] == "MKT"
    assert lot["qty"] == 50
    assert lot["stop_px"] == stop
    assert lot["avg_fill"] == price  # dry-run evidence; real execution uses broker fills
    assert lot["signal_snapshot"]["order_plan"]["entry_price"] == 100  # Pine reference retained
    assert lot["signal_snapshot"]["order_plan"]["entry_order_type"] == "MKT"
    assert lot["signal_snapshot"]["order_plan"]["risk_reference_price"] == price
    assert lot["init_risk_usd"] == 100
    assert gateway.calls[0][-1]["order_ref"] == "autotrade:" + lot["lot_id"]
    loaded = OrderManager(gateway, manager._risk, tmp_path / "orders.json")
    assert loaded.get(lot["lot_id"]).entry_order_type == "MKT"


def test_manual_limit_remains_limit(tmp_path):
    manager, gateway = manager_at(tmp_path)
    lot, error = run(manager.place_limit_stop_lot("NASDAQ:QCOM", "long", 10, 100, 99,
                                                  limit_type="ema"))
    assert not error and lot.entry_order_type == "LMT"
    assert gateway.calls[0][0] == "LMT"


@pytest.mark.parametrize("direction,price", [(1, 98), (-1, 102)])
def test_market_price_on_wrong_side_of_stop_rejects_before_submission(tmp_path, direction, price):
    manager, gateway = manager_at(tmp_path)
    engine = PineExecutionEngine(LiveBroker(manager), model=fixture_model())
    engine.start(["NASDAQ:GOOGL"], 100)
    run(engine.on_price("NASDAQ:GOOGL", price, fixture_output(direction=direction), 600))
    assert gateway.calls == [] and manager.list_lots() == []


def test_simulated_market_does_not_wait_for_pine_reference():
    broker = SimBroker()
    broker.on_price("NASDAQ:GOOGL", 101, ts=2000)
    market = run(broker.place_limit_stop("NASDAQ:GOOGL", "long", 50, 100, 99,
                 meta={"entry_order_type": "MKT", "risk_reference_price": 101}))
    assert market.state == "ACTIVE" and market.avg_fill == 101
    assert market.fill_time == 2000
    limit = run(broker.place_limit_stop("NASDAQ:GOOGL", "long", 10, 100, 99))
    assert limit.state == "PENDING"
    broker.on_price("NASDAQ:GOOGL", 102, ts=2001)
    assert market.state == "ACTIVE" and limit.state == "PENDING"
    assert len(broker.fill_events) == 1


def test_legacy_order_type_stays_unknown():
    row = dict(lot_id="legacy", symbol="NASDAQ:QCOM", side="long", qty=10,
               entry_px=100, stop_px=99, trail_amount=0)
    assert OrderLot.from_dict(row).entry_order_type == ""


def test_shadow_store_follows_isolated_runtime_paths(console_app):
    # A console test must not initialize/read the production research database.
    assert console_app.shadow_tracker._db_path == console_app.paths.data_dir / "research.db"
