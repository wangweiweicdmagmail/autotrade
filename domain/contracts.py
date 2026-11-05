"""Immutable domain contracts. No application, broker, storage or config imports."""

import math
from dataclasses import dataclass, replace
from datetime import datetime
from enum import Enum


def finite_number(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) and abs(value) < 1e99 else None


def positive(value, label: str) -> float:
    number = finite_number(value)
    if number is None or number <= 0:
        raise ValueError(f"{label} 必须为有限正数")
    return number


class OrderState(str, Enum):
    PENDING = "PENDING"
    ACTIVE = "ACTIVE"
    CLOSING = "CLOSING"
    CLOSED = "CLOSED"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"

    @classmethod
    def from_legacy(cls, value):
        return cls("CLOSED" if value == "DONE" else value)


OPEN_ORDER_VALUES = frozenset(("PENDING", "ACTIVE", "CLOSING"))
TERMINAL_ORDER_VALUES = frozenset(("CLOSED", "CANCELLED", "FAILED", "DONE"))


@dataclass(frozen=True)
class TradingGroupKey:
    strategy_id: str
    instance_id: str
    symbol: str
    group_id: str

    def metadata(self) -> dict:
        return {
            "strategy_id": self.strategy_id,
            "strategy_instance_id": self.instance_id,
            "symbol": self.symbol,
            "group_id": self.group_id,
        }


@dataclass(frozen=True)
class PriceObservation:
    symbol: str
    price: float
    observed_at: float  # Collector receipt/content-observation clock, NOT bar open.
    received_at: float  # Runtime receipt clock.
    bar_open_time: float | None = None
    market_time: float | None = None  # Unknown unless explicitly supplied by feed.
    time_basis: str = "collector_observed"

    def __post_init__(self):
        positive(self.price, "行情价格")
        positive(self.observed_at, "价格观察时间")
        positive(self.received_at, "接收时间")

    @classmethod
    def from_market(cls, symbol: str, data: dict, received_at: float):
        bar = data.get("bar") or {}
        observed = finite_number(data.get("price_observed_at"))
        return cls(
            symbol,
            positive(bar.get("close"), "行情价格"),
            observed if observed is not None else received_at,
            received_at,
            finite_number(bar.get("time")),
            finite_number(data.get("price_market_time")),
            "collector_observed" if observed is not None else "runtime_received",
        )


@dataclass(frozen=True)
class ClosedBar:
    symbol: str
    open_time: float
    close: float
    interval_seconds: int = 300
    open: float | None = None
    high: float | None = None
    low: float | None = None
    volume: float | None = None

    def __post_init__(self):
        positive(self.open_time, "K 线起始时间")
        positive(self.close, "K 线收盘价")
        if self.interval_seconds <= 0:
            raise ValueError("K 线周期必须大于 0")

    @property
    def close_time(self):
        return self.open_time + self.interval_seconds

    @classmethod
    def from_mapping(cls, symbol: str, bar: dict, interval_seconds=300):
        return cls(
            symbol,
            positive(bar.get("time"), "K 线起始时间"),
            positive(bar.get("close"), "K 线收盘价"),
            interval_seconds,
            finite_number(bar.get("open")),
            finite_number(bar.get("high", bar.get("max"))),
            finite_number(bar.get("low", bar.get("min"))),
            finite_number(bar.get("volume")),
        )

    def values(self):
        return {
            "time": self.open_time,
            "close": self.close,
            **{
                name: getattr(self, name)
                for name in ("open", "high", "low", "volume")
                if getattr(self, name) is not None
            },
        }


@dataclass(frozen=True)
class StrategyOutput:
    strategy_id: str
    time: float  # Pine's bar-open timestamp; never an exchange tick timestamp.
    entry_signal: bool
    direction: int
    entry_price: float | None
    long_stop: float | None
    short_stop: float | None
    errors: tuple = ()
    confirmed: bool = False
    received_at: float | None = None
    associated_bar_time: float | None = None

    @property
    def stop_px(self):
        return self.long_stop if self.direction == 1 else self.short_stop

    @property
    def alignment(self):
        if not self.time or not self.associated_bar_time:
            return "unknown"
        return "aligned" if self.time == self.associated_bar_time else "mismatch"

    def for_bar(self, bar_time, *, confirmed=False, received_at=None):
        errors = self.errors
        if self.time and bar_time and self.time != bar_time:
            errors += ("信号与当前 K 线时间不一致",)
        return replace(
            self,
            confirmed=confirmed,
            received_at=received_at,
            associated_bar_time=bar_time,
            errors=tuple(dict.fromkeys(errors)),
        )

    def execution_values(self) -> dict:
        return {
            "time": self.time,
            "entry_signal": int(self.entry_signal and not self.errors),
            "direction": self.direction,
            "entry_price": self.entry_price,
            "long_stop": self.long_stop,
            "short_stop": self.short_stop,
        }


@dataclass(frozen=True)
class OrderPlan:
    symbol: str
    side: str
    qty: int
    entry_price: float
    stop_price: float
    risk_budget: float = 0.0
    risk_reference_price: float = 0.0
    group: TradingGroupKey | None = None
    signal_bar_time: float | None = None
    created_at: float | None = None
    entry_order_type: str = "LMT"

    def __post_init__(self):
        if self.entry_order_type not in ("MKT", "LMT"):
            raise ValueError("入场订单类型无效")
        if not self.symbol or self.side not in ("long", "short"):
            raise ValueError("订单标的或方向无效")
        if isinstance(self.qty, bool) or not isinstance(self.qty, int) or self.qty <= 0:
            raise ValueError("qty 必须为正整数")
        positive(self.entry_price, "入场参考价")
        positive(self.stop_price, "止损价")
        budget = finite_number(self.risk_budget)
        reference = finite_number(self.risk_reference_price)
        if budget is None or budget < 0 or reference is None or reference < 0:
            raise ValueError("风险预算或参考价无效")
        if (self.reference_price - self.stop_price) * (
            1 if self.side == "long" else -1
        ) <= 0:
            raise ValueError("实际止损方向或距离无效")
        if self.group is not None and self.group.symbol != self.symbol:
            raise ValueError("订单计划与交易组标的不一致")

    @property
    def reference_price(self):
        return self.risk_reference_price or self.entry_price

    @property
    def risk_distance(self):
        return round(abs(self.reference_price - self.stop_price), 8)

    @property
    def planned_risk(self):
        return round(self.qty * self.risk_distance, 2)

    @classmethod
    def sized(
        cls,
        symbol,
        side,
        entry_price,
        stop_price,
        risk_budget,
        *,
        group=None,
        signal_bar_time=None,
        created_at=None,
        risk_reference_price=0.0,
        entry_order_type="LMT",
    ):
        budget = positive(risk_budget, "风险预算")
        entry = round(positive(entry_price, "入场价"), 2)
        stop = round(positive(stop_price, "止损价"), 2)
        reference = round(positive(risk_reference_price, "风险参考价"), 2) if risk_reference_price else entry
        distance = round((reference - stop) * (1 if side == "long" else -1), 8)
        if distance <= 0 or stop <= 0:
            raise ValueError("实际止损方向或距离无效")
        qty = math.floor(budget / distance)
        if qty <= 0:
            raise ValueError("风险预算不足一股")
        return cls(
            symbol,
            side,
            qty,
            entry,
            stop,
            budget,
            risk_reference_price=reference,
            group=group,
            signal_bar_time=signal_bar_time,
            created_at=created_at,
            entry_order_type=entry_order_type,
        )

    def values(self):
        return {
            "symbol": self.symbol,
            "side": self.side,
            "qty": self.qty,
            "entry_price": self.entry_price,
            "entry_order_type": self.entry_order_type,
            "stop_price": self.stop_price,
            "risk_reference_price": self.reference_price,
            "risk_budget": self.risk_budget,
            "planned_risk_usd": self.planned_risk,
            "signal_bar_time": self.signal_bar_time,
            "created_at": self.created_at,
            "ownership": self.group.metadata() if self.group else None,
        }


@dataclass(frozen=True)
class FillDelta:
    quantity: int
    amount: float


@dataclass(frozen=True)
class StopAmendment:
    side: str
    current_price: float
    requested_price: float

    def tightened_price(self):
        current = positive(self.current_price, "原止损")
        requested = positive(self.requested_price, "新止损")
        if self.side not in ("long", "short"):
            raise ValueError("止损方向无效")
        return round(
            max(current, requested) if self.side == "long" else min(current, requested),
            2,
        )


@dataclass(frozen=True)
class FillEvent:
    """A cumulative broker snapshot, not a new fill on every watcher poll."""

    lot_id: str
    source: str
    cumulative_qty: int
    average_price: float  # zero means not known yet; must not invent a fill price.
    received_at: float
    order_id: int | None = None
    broker_time: float | None = None

    def __post_init__(self):
        quantity = finite_number(self.cumulative_qty)
        price = finite_number(self.average_price)
        received = finite_number(self.received_at)
        if quantity is None or quantity < 0 or not quantity.is_integer():
            raise ValueError("券商累计成交数量无效")
        if price is None or price < 0:
            raise ValueError("券商累计成交均价无效")
        # Simulation may have no clock (zero); do not manufacture wall time.
        if received is None or received < 0:
            raise ValueError("成交接收时间无效")
        if self.broker_time is not None:
            positive(self.broker_time, "券商成交时间")

    @classmethod
    def from_trade(cls, lot_id, source, trade, received_at):
        status = trade.orderStatus
        quantity = finite_number(status.filled or 0)
        average = finite_number(status.avgFillPrice or 0)
        if (
            quantity is None
            or quantity < 0
            or not quantity.is_integer()
            or average is None
            or average < 0
        ):
            raise ValueError("券商累计成交数量或均价无效")
        order_id = getattr(getattr(trade, "order", None), "orderId", None)
        broker_times = []
        for fill in getattr(trade, "fills", ()) or ():
            stamp = getattr(fill, "time", None)
            if isinstance(stamp, datetime) and stamp.tzinfo is not None:
                broker_times.append(stamp.timestamp())
        return cls(
            lot_id,
            "protective" if source == "trail" else source,
            int(quantity),
            average,
            received_at,
            int(order_id) if order_id else None,
            max(broker_times) if broker_times else None,
        )

    def delta_from(self, quantity: int, average: float) -> FillDelta:
        if self.cumulative_qty < quantity:
            raise ValueError("券商累计成交数量回退")
        return FillDelta(
            self.cumulative_qty - quantity,
            self.average_price * self.cumulative_qty - average * quantity,
        )


@dataclass(frozen=True)
class ExecutionGroupState:
    key: TradingGroupKey
    state: str
    entries: int
    quantity: int
    average_cost: float
    stop_price: float
    enabled: bool

    def __post_init__(self):
        if self.state not in (
            "WATCHING",
            "AWAITING_CONFIRM",
            "PENDING",
            "ACTIVE",
            "CLOSING",
            "DONE",
        ):
            raise ValueError(f"未知执行组状态: {self.state}")
        if self.entries < 0 or self.quantity < 0:
            raise ValueError("执行组次数与持仓不得为负")
