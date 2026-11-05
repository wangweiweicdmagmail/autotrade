"""UI metadata preserves execution state and never invents broker confirmation."""

from types import SimpleNamespace

from engine.pine_execution import PineExecutionEngine
from execution.order_lot import OrderLot

from .test_pyramid_safety import StateBroker


def test_startup_metadata_is_persisted_without_changing_rules(monkeypatch):
    monkeypatch.setattr("engine.pine_execution.time.time", lambda: 1000)
    broker = StateBroker()
    engine = PineExecutionEngine(broker, breakeven_r=4, add_unlock_r=2.5)
    engine.start(["NASDAQ:NVDA"], 300, {"NASDAQ:NVDA": -1})
    row = engine.instances()[0]
    saved = broker.states[row["group_id"]]
    assert saved["started_at"] == 1000
    assert saved["startup_params"]["start_direction"] == -1
    assert saved["startup_params"]["initial_risk_usd"] == 300
    assert saved["startup_params"]["breakeven_r"] == 4
    engine.breakeven_r = 5
    assert engine.instances()[0]["breakeven_r"] == 5
    assert engine.instances()[0]["startup_params"]["breakeven_r"] == 4


def test_protection_output_roundtrip_requires_new_broker_evidence():
    lot = OrderLot("fixture", "NASDAQ:NVDA", "long", 10, 100, 99, 0)
    assert lot.to_dict()["protection_order_status"] is None
    lot.trail_trade = SimpleNamespace(orderStatus=SimpleNamespace(status="Submitted"),
                                     order=SimpleNamespace(totalQuantity=10))
    snapshot = lot.to_dict()
    assert snapshot["protection_order_status"] == "Submitted"
    assert snapshot["protection_order_qty"] == 10
    restored = OrderLot.from_dict(snapshot)
    assert restored.stop_px == 99
    assert restored.to_dict()["protection_order_status"] is None
