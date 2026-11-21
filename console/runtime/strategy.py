"""统一行情分发：按模型与执行实例隔离 bar 游标，不推导 Pine 信号。"""

import logging
import time

from domain.contracts import ClosedBar, PriceObservation, finite_number
from engine.opening_range import opening_entry_block

logger = logging.getLogger(__name__)


class StrategyRuntime:
    def __init__(self, service, *, require_opening_range=False):
        self.service = service
        self.require_opening_range = require_opening_range
        self.last_bars = {}
        self.adopt_tick = 0
        self.rejected_signals = {}
        self.unverified_confirmed = set()

    async def _confirmed(self, engine, symbol, data, *, force=False):
        model = engine.model
        prices = model.market_bars(data)
        raw_bar = prices.get("bar_c") or {}
        raw_output = data.get(model.confirmed_key) or {}
        if not raw_bar or not raw_output:
            return
        key = (model.strategy_id, engine.instance_id, symbol)
        try:
            bar = ClosedBar.from_mapping(symbol, raw_bar, model.interval_seconds)
        except ValueError:
            return
        current_open = finite_number((prices.get("bar") or {}).get("time"))
        if current_open and bar.open_time >= current_open:
            return
        stamp = raw_output.get("time")
        if stamp is not None and finite_number(stamp) != bar.open_time:
            return
        if not force and bar.open_time <= self.last_bars.get(key, 0):
            return
        output = model.decode(raw_output).for_bar(bar.open_time, confirmed=True, received_at=time.time())
        if output.alignment == "unknown" and key not in self.unverified_confirmed:
            self.unverified_confirmed.add(key)
            self.service.record_event(
                "pine_confirmation_unverified", strategy_id=model.strategy_id,
                strategy_instance_id=engine.instance_id, symbol=symbol,
                reason="旧确认快照缺少指标时间，按兼容模式处理，未宣称时间已核验",
            )
        await engine.on_confirmed_bar(bar, output)
        # A failed dispatch must remain retryable on the next snapshot.
        self.last_bars[key] = bar.open_time

    async def step(self, market, now_hm, *, trading, stale=False):
        if not market or (trading and stale):
            return
        engines = self.service.engines()
        for engine in engines:
            model = engine.model
            for symbol, data in market.items():
                key = (model.strategy_id, engine.instance_id, symbol)
                try:
                    await self._confirmed(engine, symbol, data)
                except Exception:
                    logger.exception("Pine 收盘管理异常 %s %s", model.strategy_id, symbol)
                prices = model.market_bars(data)
                price = (prices.get("bar") or {}).get("close")
                if trading and price:
                    try:
                        observation = PriceObservation.from_market(symbol, prices, time.time())
                        output = model.from_market(data).for_bar(
                            observation.bar_open_time, received_at=observation.received_at,
                        )
                        if output.entry_signal and output.errors:
                            fingerprint = (output.time, output.errors)
                            if self.rejected_signals.get(key) != fingerprint:
                                self.rejected_signals[key] = fingerprint
                                self.service.record_event(
                                    "pine_output_rejected",
                                    strategy_id=model.strategy_id,
                                    strategy_instance_id=engine.instance_id,
                                    symbol=symbol,
                                    errors=list(output.errors),
                                )
                        block = ""
                        if self.require_opening_range and output.entry_signal:
                            block = opening_entry_block(
                                data, output.direction, observation.price,
                                as_of=observation.received_at,
                            )
                        if block:
                            await engine.on_observation(
                                observation, output, now_hm, entry_block_reason=block,
                            )
                        else:
                            await engine.on_observation(observation, output, now_hm)
                    except Exception:
                        logger.exception(
                            "Pine 实时执行异常 %s %s", model.strategy_id, symbol
                        )
        self.adopt_tick += 1
        if self.adopt_tick >= 5:
            self.adopt_tick = 0
            lots = self.service.order_manager.list_lots(active_only=True)
            # 先根据持久化归属发现执行实例，避免重启后只恢复默认策略。
            for row in lots:
                meta = row.get("signal_snapshot") or {}
                strategy_id = meta.get("strategy_id")
                if strategy_id:
                    try:
                        self.service.engine(
                            strategy_id, meta.get("strategy_instance_id") or "default"
                        )
                    except ValueError:
                        logger.error("无法恢复未注册 Pine 模型的订单 %s", row["lot_id"])
            for engine in self.service.engines():
                try:
                    adopted = await engine.adopt_manual_lots(lots, signals=market)
                    for symbol in adopted:
                        data = market.get(symbol) or {}
                        await self._confirmed(engine, symbol, data, force=True)
                except Exception:
                    logger.exception("Pine 持仓恢复异常 %s", engine.model.strategy_id)
