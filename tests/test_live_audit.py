import asyncio
import sqlite3
from types import SimpleNamespace

from engine.live_audit import LiveMarketAudit, decision_market_frame
from engine.realtime_collector import RealtimeCollector
from console.services.events import EventsService


def test_audit_keeps_exact_market_frames_and_decision_link(tmp_path):
    async def exercise():
        journal = LiveMarketAudit(tmp_path / "live_audit.sqlite3")
        collector = RealtimeCollector(["NASDAQ:TEST"], audit=journal)
        context = SimpleNamespace(paths=SimpleNamespace(data_dir=tmp_path), collector=collector)
        events = EventsService(context)
        symbol = "NASDAQ:TEST"
        row = {
            "session": {"requested":"regular","resolved":"regular","accepted":True},
            "bar": {"time": 1800, "close": 101},
            "bar_c": {"time": 1500, "close": 100},
            "e9": {"time": 1800, "touch": 1},
            "e9_c": {"time": 1500, "touch": 0},
            "opening_breakout_history": [[1500, 100]],
        }
        journal.start()
        collector._accept_tick({"ts": 1803, "symbols": {symbol: row}}, now=1804)
        decision_frame = collector.market_frame_id
        token = decision_market_frame.set(decision_frame)
        try:
            collector._accept_tick({"ts": 1804, "symbols": {symbol: row}}, now=1805)
            repeated_frame = collector.market_frame_id
            changed = {**row, "bar": {"time": 1800, "close": 102}, "e9": {}}
            collector._accept_tick({"ts": 1805, "symbols": {symbol: changed}}, now=1806)
            events.record_event("broker_order", lot_id="lot-test", order_id=123)
        finally:
            decision_market_frame.reset(token)
        latest_frame = collector.market_frame_id
        collector._accept_tick({"ts": 1806, "symbols": {}}, now=1807)
        absent_frame = collector.market_frame_id
        await journal.stop()

        rows = journal.read(symbol)
        assert [item["frame_id"] for item in rows] == [latest_frame, repeated_frame, decision_frame]
        assert rows[0]["data"] == {**changed, "price_observed_at": 1806}
        assert rows[1]["data"] == rows[2]["data"] == {**row, "price_observed_at": 1804}
        assert rows[2]["received_at"] == 1804 and rows[2]["source_at"] == 1803
        assert journal.read(symbol, frame_id=int(absent_frame)) == []
        assert journal.read(symbol, frame_id=int(decision_frame)) == rows[-1:]
        assert journal.status()["dropped"] == 0
        assert journal.status()["last_committed_frame"] == absent_frame
        with sqlite3.connect(journal.path) as db:
            assert db.execute("SELECT count(*) FROM market_versions").fetchone()[0] == 2
            assert db.execute("SELECT count(*) FROM market_history").fetchone()[0] == 1

        matches = await events.api_events(lot_id="lot-test", order_id=123)
        assert matches[0]["decision_market_frame"] == decision_frame
        assert matches[0]["market_frame_at_event"] == latest_frame
        assert not await events.api_events(order_id=456)
        path = next(tmp_path.glob("events-*.jsonl"))
        line = path.read_text()
        path.write_text("broken line\n" + line)
        assert len(await events.api_events(lot_id="lot-test")) == 1

    asyncio.run(exercise())


def test_audit_write_failure_reports_gap_and_recovers(tmp_path, monkeypatch):
    async def exercise():
        journal = LiveMarketAudit(tmp_path / "live_audit.sqlite3")
        write = journal._write
        monkeypatch.setattr(journal, "_write", lambda frames: (_ for _ in ()).throw(OSError("disk failure")))
        journal.start()
        bad_frame = journal.capture({"X": {"bar": {"time": 10}}}, 11, 10)
        await journal.stop()
        assert journal.status()["dropped"] == 1
        assert journal.status()["error"] == "disk failure"

        monkeypatch.setattr(journal, "_write", write)
        journal.start()
        journal.capture({"X": {"bar": {"time": 10}}}, 12, 11)
        await journal.stop()
        assert journal.read("X", frame_id=int(bad_frame)) == []
        assert journal.status()["dropped"] == 1
        assert journal.status()["error"] == ""

    asyncio.run(exercise())

def test_agent_read_endpoints(console_app):
    import httpx

    async def exercise():
        audit = console_app.market_audit
        audit.start()
        frame = audit.capture({"NASDAQ:TEST": {"bar": {"time": 1500}}}, 1501, 1500)
        await audit.stop()
        transport = httpx.ASGITransport(app=console_app.app, client=("127.0.0.1", 8001))
        async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as client:
            result = await client.get("/api/market/audit", params={"symbol": "NASDAQ:TEST", "frame_id": frame})
            assert result.status_code == 200
            assert result.json()["rows"][0]["frame_id"] == frame
            assert (await client.get("/api/lots?active_only=true")).json() == []
            assert (await client.get("/api/positions")).json() == []
            assert (await client.get("/api/trades/history")).json() == []
        transport = httpx.ASGITransport(app=console_app.app, client=("192.0.2.1", 8001))
        async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as client:
            assert (await client.get("/api/market/audit", params={"symbol": "NASDAQ:TEST"})).status_code == 403

    asyncio.run(exercise())


def test_broker_fill_audit_has_order_and_lot_ids():
    from datetime import datetime, timezone
    from ib_async import MarketOrder, OrderStatus, Stock, Trade
    from execution.ibkr import IBKRGateway

    gateway = IBKRGateway("127.0.0.1", 1, 0, "", "")
    captured = []
    gateway.on_order_audit = lambda event, **data: captured.append((event, data))
    trade = Trade(
        Stock("TEST", "SMART", "USD"),
        MarketOrder("BUY", 5, orderId=123, orderRef="autotrade:lot-1:entry"),
        OrderStatus(status="Filled", filled=5, avgFillPrice=101),
    )
    fill = SimpleNamespace(execution=SimpleNamespace(
        execId="exec-123", time=datetime(2026, 11, 5, 15, tzinfo=timezone.utc),
        shares=5, price=101,
    ))
    gateway._audit_trade("submitted", trade)
    gateway._audit_trade("fill", trade, fill)
    assert [data["action"] for _, data in captured] == ["submitted", "fill"]
    event, data = captured[-1]
    assert event == "broker_order"
    assert data["lot_id"] == "lot-1"
    assert data["order_id"] == 123
    assert data["exec_id"] == "exec-123"
    assert data["fill_qty"] == 5
