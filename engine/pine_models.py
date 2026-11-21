"""Pine 是策略模型：这里只映射输出，不计算任何行情入场条件。"""

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

# Compatibility names remain available at the original import path.
from domain.contracts import StrategyOutput as PineOutput
from domain.contracts import finite_number as number

MODEL_PATH = Path(__file__).resolve().parent.parent / "service" / "pine_strategies.json"


@dataclass(frozen=True)
class ExecutionProfile:
    """执行配置与 Pine 信号分开；历史 EMA9 行为显式选择，非通用默认。"""

    name: str = "single_entry"
    risk_ratios: tuple = (1.0,)
    entry_start_hm: int = 0
    entry_end_hm: int = 24 * 60
    auto_breakeven: bool = False
    add_unlock_r: float = 2.0
    breakeven_r: float = 3.0
    follow_pine_stop: bool = True
    breakeven_on_entry_bar_close: bool = False
    lock_start_direction: bool = True
    finish_after_trade: bool = False


PROFILES = {
    "single_entry": ExecutionProfile(),
    "entry_bar_breakeven": ExecutionProfile(
        name="entry_bar_breakeven",
        follow_pine_stop=False,
        breakeven_on_entry_bar_close=True,
        lock_start_direction=False,
        finish_after_trade=True,
    ),
    "ema9_legacy_pyramid": ExecutionProfile(
        name="ema9_legacy_pyramid",
        risk_ratios=(1.0, 2 / 3, 1 / 3),
        entry_start_hm=9 * 60 + 45,
        entry_end_hm=15 * 60,
        auto_breakeven=True,
        follow_pine_stop=False,
    ),
}


@dataclass(frozen=True)
class PineModel:
    strategy_id: str
    name: str
    indicator_id: str
    realtime_key: str
    confirmed_key: str
    fields: Mapping[str, str]
    execution_profile: str = "single_entry"
    local_only: bool = False
    interval_seconds: int = 300
    startup_candidate_field: str = ""

    @property
    def profile(self):
        return PROFILES[self.execution_profile]

    def decode(self, values: dict) -> PineOutput:
        values = values or {}
        direction = number(values.get(self.fields["direction"]))
        direction = int(direction) if direction in (1, -1) else 0
        entry = number(values.get(self.fields["entry_price"]))
        long_stop = number(values.get(self.fields["long_stop"]))
        short_stop = number(values.get(self.fields["short_stop"]))
        signal = number(values.get(self.fields["entry_signal"])) == 1
        errors = []
        if signal:
            if not (number(values.get("time")) or 0) > 0:
                errors.append("信号时间缺失或无效")
            if not direction:
                errors.append("方向缺失或无效")
            stop = long_stop if direction == 1 else short_stop
            if entry is None or entry <= 0 or stop is None or stop <= 0:
                errors.append("入场价或止损缺失/无效")
            elif direction and (entry - stop) * direction <= 0:
                errors.append("止损方向无效")
        return PineOutput(
            self.strategy_id,
            number(values.get("time")) or 0,
            signal,
            direction,
            entry,
            long_stop,
            short_stop,
            tuple(errors),
        )

    def from_market(self, data: dict, *, confirmed=False) -> PineOutput:
        return self.decode(
            (data or {}).get(self.confirmed_key if confirmed else self.realtime_key)
            or {}
        )

    def market_bars(self, data: dict) -> dict:
        return (data.get("market_1m") or {}) if self.interval_seconds == 60 else data

    def describe(self):
        return {
            "id": self.strategy_id,
            "name": self.name,
            "indicator_id": self.indicator_id,
            "realtime_key": self.realtime_key,
            "confirmed_key": self.confirmed_key,
            "execution_profile": self.execution_profile,
            "fields": dict(self.fields),
            **({"local_only": True} if self.local_only else {}),
            **({"interval_seconds": self.interval_seconds} if self.interval_seconds != 300 else {}),
        }


class PineModelRegistry:
    def __init__(self, models=()):
        self._models = {}
        for model in models:
            self.register(model)

    def register(self, model: PineModel):
        if not re.fullmatch(r"[a-z][a-z0-9_]*", model.strategy_id):
            raise ValueError("策略标识必须为小写字母、数字和下划线")
        if model.strategy_id in self._models:
            raise ValueError(f"重复策略标识: {model.strategy_id}")
        if model.execution_profile not in PROFILES:
            raise ValueError(f"未知执行配置: {model.execution_profile}")
        if model.interval_seconds not in (60, 300):
            raise ValueError(f"不支持的策略 K 线周期: {model.interval_seconds}")
        required = {
            "entry_signal",
            "direction",
            "entry_price",
            "long_stop",
            "short_stop",
        }
        if not required <= model.fields.keys() or not all(
            model.fields[k] for k in required
        ):
            raise ValueError("Pine 输出字段映射不完整")
        reserved = {
            "bar",
            "bar_c",
            "bar_c2",
            "ts",
            "ts_c",
            "fvg",
            "fvg_c",
            "attack",
            "attack_c",
            "ss",
            "ss_c",
            "ema_9",
            "ema_9_c",
            "market_1m",
            "price_observed_at",
        }
        if model.realtime_key == model.confirmed_key or any(
            not re.fullmatch(r"[a-z][a-z0-9_]*", key) or key in reserved
            for key in (model.realtime_key, model.confirmed_key)
        ):
            raise ValueError("Pine 快照键无效或覆盖核心行情字段")
        for existing in self._models.values():
            if {model.realtime_key, model.confirmed_key} & {
                existing.realtime_key,
                existing.confirmed_key,
            }:
                raise ValueError("策略快照键冲突")
        self._models[model.strategy_id] = model

    def get(self, strategy_id):
        try:
            return self._models[strategy_id]
        except KeyError:
            raise ValueError(f"未注册 Pine 策略: {strategy_id}") from None

    def describe(self):
        return [model.describe() for model in self._models.values()]

    @classmethod
    def load(cls, path=MODEL_PATH):
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        if payload.get("version") != 1:
            raise ValueError("不支持的 Pine 策略配置版本")
        return cls(
            PineModel(
                row["id"],
                row["name"],
                row["indicator_id"],
                row["realtime_key"],
                row["confirmed_key"],
                row["fields"],
                row.get("execution_profile", "single_entry"),
                row.get("local_only", False),
                row.get("interval_seconds", 300),
                row.get("startup_candidate_field", ""),
            )
            for row in payload["strategies"]
        )
