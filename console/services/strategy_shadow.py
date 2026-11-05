"""Regular-session strategy observation using the simulated broker only."""

import json
from dataclasses import asdict
from datetime import datetime
from types import SimpleNamespace
from uuid import uuid4
from zoneinfo import ZoneInfo

import config
from console.runtime.strategy import StrategyRuntime
from domain.contracts import finite_number
from engine.brokers.simulated import SimBroker
from engine.pine_execution import PineExecutionEngine
from engine.pine_models import PineModelRegistry

ET = ZoneInfo("America/New_York")
STRATEGY = "ema9_pyramid"


class StrategyShadow:
    def __init__(self, directory, watchlist, execution_settings):
        self.directory = directory
        self.watchlist = set(watchlist)
        self.execution_settings = execution_settings
        self.day = ""
        self.run_id = uuid4().hex
        self.engine = None
        self.broker = None
        self._saved = None

    async def step(self, market, now, *, stale):
        minute = now.hour * 60 + now.minute
        if stale or now.weekday() >= 5 or not 570 <= minute < 960:
            return
        stamp = now.timestamp()
        fresh = {}
        for symbol in self.watchlist | {"NASDAQ:QQQ"}:
            data = market.get(symbol) or {}
            bar = data.get("bar") or {}
            observed = finite_number(data.get("price_observed_at"))
            opened = finite_number(bar.get("time"))
            price = finite_number(bar.get("close"))
            if (observed and opened and price and price > 0
                    and 0 <= stamp - observed <= 45 and 0 <= stamp - opened < 300):
                fresh[symbol] = data
        if not fresh:
            return
        day = now.date().isoformat()
        if day != self.day:
            if minute >= 945:
                return
            self.day = day
            self._saved = None
            self.broker = SimBroker()
            model = PineModelRegistry.load().get(STRATEGY)
            self.engine = PineExecutionEngine(
                self.broker, model=model, max_instances=len(self.watchlist),
                instance_id="shadow",
                breakeven_enabled=self.execution_settings._pyramid_auto_breakeven(),
                breakeven_r=self.execution_settings._pyramid_breakeven_r(),
                add_unlock_r=self.execution_settings._pyramid_add_unlock_r(),
            )
            self.runtime = StrategyRuntime(SimpleNamespace(
                engines=lambda: [self.engine],
                order_manager=SimpleNamespace(list_lots=lambda **kwargs: []),
                record_event=lambda *args, **kwargs: None,
            ), require_opening_range=True)
        model = self.engine.model
        if minute < 945:
            direction = model.from_market(fresh.get("NASDAQ:QQQ", {})).direction
            if direction in (-1, 1):
                directions = {
                    symbol: direction for symbol, data in fresh.items()
                    if symbol in self.watchlist and self.engine.get_instance(symbol) is None
                    and model.from_market(data).direction == direction
                    and model.from_market(data).entry_price
                }
                if directions:
                    self.engine.start(list(directions), config.RISK_PER_TRADE_USD, directions)
        active_data = {s: data for s, data in fresh.items() if self.engine.get_instance(s)}
        for symbol, data in active_data.items():
            price = data["bar"]["close"]
            self.broker.on_price(symbol, price, data["price_observed_at"])
            if minute >= 945 and any(
                lot.symbol == symbol and lot.state in ("PENDING", "ACTIVE")
                for lot in self.broker._lots.values()
            ):
                await self.engine.close_symbol(symbol, price, reason="shadow_eod")
        if minute < 945:
            await self.runtime.step(active_data, minute, trading=True)
        self._save()

    def _save(self):
        orders = [asdict(lot) for lot in self.broker._lots.values()]
        symbols = sorted(row["symbol"] for row in self.engine.instances())
        data = {"symbols": symbols, "orders": orders}
        if data == self._saved:
            return
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.directory / f"{self.day}-{self.run_id}.json"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)
        self._saved = data

    def report(self, day=""):
        if day:
            day = datetime.strptime(day, "%Y-%m-%d").date().isoformat()
        else:
            day = datetime.now(ET).date().isoformat()
        symbols, orders = set(), []
        for path in sorted(self.directory.glob(f"{day}-*.json")):
            run = json.loads(path.read_text(encoding="utf-8"))
            symbols.update(run["symbols"])
            orders.extend({**row, "lot_id": f"{path.stem[11:19]}:{row['lot_id']}"} for row in run["orders"])
        return {
            "day": day, "model": "EMA9-ST-ATR", "symbols": sorted(symbols), "orders": orders,
            "realized_pnl": round(sum(row["realized_pnl"] for row in orders), 2),
        }
