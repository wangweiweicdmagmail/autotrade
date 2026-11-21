"""Manually started Pine paper tests. No production broker, collector or ledger access."""

import asyncio
import logging
import math
import time
from collections import deque
from dataclasses import asdict, replace
from datetime import datetime
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

from engine.brokers.simulated import SimBroker
from engine.paper_feed import PinePaperFeed
from engine.pine_execution import PineExecutionEngine
from engine.pine_models import PineModelRegistry
from execution.state_store import atomic_write, encode

logger = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")


def minute(value):
    hour, minutes = map(int, value.split(":"))
    if not 0 <= hour <= 23 or not 0 <= minutes <= 59:
        raise ValueError("时间必须为 HH:MM 美东时间")
    return hour * 60 + minutes


def in_window(hm, start, end):
    return start == end or (
        start <= hm < end if start < end else hm >= start or hm < end
    )


class NightTestError(ValueError):
    pass


class NightTestService:
    def __init__(self, directory, tasks, *, registry=None, feed_factory=PinePaperFeed):
        self.directory = Path(directory) / "paper_tests"
        self.tasks = tasks
        self.registry = registry or PineModelRegistry.load()
        self.feed_factory = feed_factory
        self.lock = asyncio.Lock()
        self.feed = None
        self.model = None
        self.engine = None
        self.broker = None
        self.running = False
        self.run_id = None
        self.started_at = None
        self.stopped_at = None
        self.error = ""
        self.events = deque(maxlen=5000)
        self.samples = deque(maxlen=10000)
        self.dropped_samples = 0
        self.dropped_events = 0
        self.last_fingerprint = None
        self.last_bar = None
        self.last_price = None
        self.last_observed_at = None
        self.params = {}

    async def connect(self, symbol, strategy, session):
        async with self.lock:
            if self.run_id and self.stopped_at is None:
                raise NightTestError("请先停止并结算当前模拟，再切换数据")
            symbol = symbol.strip().upper()
            import re

            if not re.fullmatch(r"[A-Z0-9_]+:[A-Z0-9_.!\-]+", symbol):
                raise NightTestError("请填写带交易所的标的，例如 NASDAQ:NVDA")
            if session not in ("regular", "extended", "24h"):
                raise NightTestError("不支持的数据时段")
            model = self.registry.get(strategy)
            if model.local_only:
                raise NightTestError("该策略尚未接入夜盘验证")
            if self.feed:
                await self.feed.stop()
            self.engine = self.broker = None
            self.run_id = self.started_at = self.stopped_at = None
            self.params = {}
            self.events.clear()
            self.samples.clear()
            self.feed = self.feed_factory(symbol, strategy, session)
            self.model = model
            self.error = ""
            try:
                await self.feed.start()
            except Exception:
                logger.exception("夜盘只读数据订阅启动失败")
                await self.feed.stop()
                self.error = "数据服务启动失败，请核对 Node、网络与登录状态"
                raise NightTestError(self.error) from None
            self.tasks.start("night_test.paper_loop", self._loop)
            return self.status()

    def _feed_ready(self):
        if not self.feed or not self.model:
            return False, "请先连接测试数据"
        health = self.feed.health()
        if not health["fresh"]:
            return False, health["reason"]
        data = self.feed.data
        session = data.get("session") or {}
        if session.get("resolved") != self.feed.session:
            return False, "服务端尚未确认请求时段；不会回退到其他时段进行测试"
        from domain.contracts import PriceObservation

        try:
            PriceObservation.from_market(self.feed.symbol, data, time.time())
        except ValueError:
            return False, "行情价格或观察时间无效"
        return True, ""

    def _ready(self):
        ready, reason = self._feed_ready()
        if not ready:
            return ready, reason
        data = self.feed.data
        output = self.model.from_market(data).for_bar(
            (data.get("bar") or {}).get("time")
        )
        confirmed = self.model.from_market(data, confirmed=True)
        bar_c = data.get("bar_c") or {}
        if (
            output.errors
            or output.alignment != "aligned"
            or not output.entry_price
            or output.entry_price <= 0
            or confirmed.time != bar_c.get("time")
            or confirmed.direction not in (1, -1)
        ):
            return False, "等待时间对齐的实时及确认 Pine 输出、有效参考价和启动方向"
        return True, ""

    async def start_run(self, risk, window_start, window_end):
        async with self.lock:
            if self.run_id and self.stopped_at is None:
                raise NightTestError("当前模拟尚未结算，请先停止并结算")
            if not math.isfinite(risk) or risk <= 0 or risk > 100000:
                raise NightTestError("模拟风险预算无效")
            ready, reason = self._ready()
            if not ready:
                raise NightTestError(reason)
            begin, end = minute(window_start), minute(window_end)
            self.broker = (
                SimBroker()
            )  # always constructed here; cannot inject a live port
            profile = replace(self.model.profile, entry_start_hm=0, entry_end_hm=1440)
            self.run_id = uuid4().hex
            self.events.clear()
            self.samples.clear()
            self.dropped_samples = self.dropped_events = 0
            self.started_at = time.time()
            self.stopped_at = None
            self.error = ""
            self.last_fingerprint = None
            self.last_price = self.last_observed_at = None
            self.last_bar = (self.feed.data.get("bar_c") or {}).get("time")
            self.params = {
                "risk_budget": risk,
                "window_start": window_start,
                "window_end": window_end,
                "window_minutes": [begin, end],
                "timezone": "America/New_York",
                "session": self.feed.session,
                "symbol": self.feed.symbol,
                "strategy": self.model.strategy_id,
                "base_execution_profile": self.model.profile.name,
                "effective_profile": asdict(profile),
                "entry_window_override": True,
                "source": dict(self.feed.data.get("session") or {}),
                "startup_direction_source": "completed_bar",
                "sampling": "latest quote / 1 second",
                "settlement_reference": "last observed valid test quote",
            }
            self.engine = PineExecutionEngine(
                self.broker,
                model=self.model,
                instance_id=f"paper-{self.run_id}",
                execution_profile=profile,
                emit=self._event,
            )
            direction = self.model.from_market(self.feed.data, confirmed=True).direction
            self.params.update(
                auto_breakeven=self.engine.breakeven_enabled,
                breakeven_r=self.engine.breakeven_r,
                add_unlock_r=self.engine.add_unlock_r,
            )
            self.engine.start([self.feed.symbol], risk, {self.feed.symbol: direction})
            self._event(
                "paper_started", started_at=self.started_at, startup_direction=direction
            )
            # Persist the empty test and manual startup boundary before processing quotes.
            self._save_report()
            self.running = True
            return self.status()

    def _event(self, event, **values):
        if len(self.events) == self.events.maxlen:
            self.dropped_events += 1
        self.events.append({"event": event, "received_at": time.time(), **values})

    async def step(self, now=None):
        async with self.lock:
            if not self.running:
                return
            ready, _reason = self._feed_ready()
            if not ready:
                return  # no historic high/low replay, no orders from stale/unverified data
            data = self.feed.data
            observed_at = data.get("price_observed_at") or 0
            if observed_at < self.started_at:
                return  # data loaded for inspection is never a pre-start trading tick
            now = now or time.time()
            output = self.model.from_market(data).for_bar(
                data["bar"]["time"], received_at=now
            )
            fingerprint = (
                observed_at,
                replace(output, received_at=None),
                (data.get("bar_c") or {}).get("time"),
                self.model.from_market(data, confirmed=True),
            )
            if fingerprint == self.last_fingerprint:
                return
            price = data["bar"]["close"]
            if self.last_observed_at and observed_at < self.last_observed_at:
                return
            symbol = self.feed.symbol
            # Existing pending orders consume only newly observed quotes, never historical extremes.
            if self.last_observed_at is None or observed_at > self.last_observed_at:
                self.broker.on_price(symbol, price, ts=observed_at)
            closed = data.get("bar_c") or {}
            if (
                closed.get("time", 0) > (self.last_bar or 0)
                and self.model.from_market(data, confirmed=True).time == closed["time"]
                and closed["time"] < data["bar"]["time"]
            ):
                from domain.contracts import ClosedBar

                await self.engine.on_confirmed_bar(
                    ClosedBar.from_mapping(symbol, closed),
                    self.model.from_market(data, confirmed=True),
                )
                self.last_bar = closed["time"]
            dt = datetime.fromtimestamp(observed_at, ET)
            hm = dt.hour * 60 + dt.minute
            allowed = in_window(hm, *self.params["window_minutes"])
            effective = output if allowed else replace(output, entry_signal=False)
            await self.engine.on_output(
                symbol, price, effective, hm, observed_at=observed_at
            )
            self.last_price, self.last_observed_at = price, observed_at
            self.last_fingerprint = fingerprint
            if len(self.samples) == self.samples.maxlen:
                self.dropped_samples += 1
            self.samples.append(
                {
                    "received_at": now,
                    "observed_at": observed_at,
                    "bar": dict(data["bar"]),
                    "output": asdict(output),
                    "entry_window_open": allowed,
                }
            )
            self._save_report()

    async def stop_run(self):
        async with self.lock:
            if self.run_id and self.stopped_at is None:
                for instance in self.engine._instances.values():
                    self.engine.stop_symbol(instance.symbol)
                for lot in self.broker._lots.values():
                    if lot.state == "PENDING":
                        await self.broker.cancel(lot.lot_id)
                    elif lot.state == "ACTIVE" and self.last_price:
                        await self.broker.close(
                            lot.lot_id,
                            self.last_price,
                            "paper_manual_stop",
                            ts=self.last_observed_at,
                        )
                for instance in self.engine._instances.values():
                    await self.engine._refresh(instance)
                self.running = False
                self.stopped_at = time.time()
                self._event(
                    "paper_stopped",
                    quote_time=self.last_observed_at,
                    reference_price=self.last_price,
                )
                self._save_report()
            return self.status()

    async def disconnect(self):
        await self.stop_run()
        async with self.lock:
            if self.feed:
                await self.feed.stop()
            return self.status()

    async def _loop(self):
        while True:
            await asyncio.sleep(1)
            try:
                await self.step()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("夜盘模拟推进失败")
                # Preserve simulated positions for explicit stop/report; never resume automatically.
                self.running = False
                self.error = "模拟处理中断，已停止推进；请导出当前记录并重新连接测试"

    def status(self):
        ready, reason = self._ready()
        data = self.feed.data if self.feed else {}
        return {
            "connected": bool(self.feed and self.feed.health()["connected"]),
            "health": self.feed.health() if self.feed else {},
            "ready": ready,
            "reason": reason,
            "running": self.running,
            "error": self.error,
            "run_id": self.run_id,
            "started_at": self.started_at,
            "stopped_at": self.stopped_at,
            "params": self.params,
            "session": data.get("session") or {},
            "symbol": self.feed.symbol if self.feed else None,
            "strategy": self.model.strategy_id if self.model else None,
            "raw_realtime": data.get(self.model.realtime_key) if self.model else {},
            "raw_confirmed": data.get(self.model.confirmed_key) if self.model else {},
            "bar": data.get("bar"),
            "bar_c": data.get("bar_c"),
            "bars": (data.get("bars") or [])[-200:],
            "history_outputs": [
                asdict(self.model.decode(row))
                for row in data.get("history_outputs", [])
            ]
            if self.model
            else [],
            "realtime": asdict(self.model.from_market(data)) if self.model else None,
            "confirmed": asdict(self.model.from_market(data, confirmed=True))
            if self.model
            else None,
            "orders": [asdict(lot) for lot in self.broker._lots.values()]
            if self.broker
            else [],
            "instances": self.engine.instances() if self.engine else [],
            "events": list(self.events)[-100:],
            "dropped_samples": self.dropped_samples,
            "dropped_events": self.dropped_events,
            "limitations": "MKT 入场按触发时最新观察价模拟成交，保护止损仍按后续新鲜价格触发；不含盘口、滑点、手续费、实盘账户风控与成交保证。",
        }

    def report(self):
        result = self.status()
        result.update(
            samples=list(self.samples),
            events=list(self.events),
            fills=[asdict(event) for event in self.broker.fill_events]
            if self.broker
            else [],
            paper_only=True,
            model=self.model.describe() if self.model else None,
        )
        return result

    def _save_report(self):
        atomic_write(self.directory / f"{self.run_id}.json", encode(self.report()))
