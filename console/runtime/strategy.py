"""统一行情分发：按模型与执行实例隔离 bar 游标，不推导 Pine 信号。"""

import logging
import time
from dataclasses import replace

logger = logging.getLogger(__name__)


class StrategyRuntime:
    def __init__(self, service):
        self.service = service
        self.last_bars = {}
        self.adopt_tick = 0
        self.rejected_signals = {}

    async def step(self, market, now_hm, *, trading, stale=False):
        if not market or (trading and stale):
            return
        engines = self.service.engines()
        for engine in engines:
            model = engine.model
            for symbol, data in market.items():
                bar_c = data.get("bar_c") or {}
                bt = bar_c.get("time") or 0
                key = (model.strategy_id, engine.instance_id, symbol)
                confirmed = data.get(model.confirmed_key) or {}
                confirmed_time = confirmed.get("time")
                aligned = confirmed_time is None or confirmed_time == bt
                if bt and aligned and self.last_bars.get(key) != bt:
                    self.last_bars[key] = bt
                    try:
                        await engine.on_bar(symbol, bar_c, confirmed)
                    except Exception:
                        logger.exception(
                            "Pine 收盘管理异常 %s %s", model.strategy_id, symbol
                        )
                price = (data.get("bar") or {}).get("close")
                if trading and price:
                    try:
                        output = model.from_market(data)
                        bar_time = (data.get("bar") or {}).get("time")
                        if (
                            output.entry_signal
                            and output.time
                            and bar_time
                            and output.time != bar_time
                        ):
                            output = replace(
                                output,
                                errors=output.errors + ("信号与当前 K 线时间不一致",),
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
                        await engine.on_output(
                            symbol,
                            price,
                            output,
                            now_hm,
                            observed_at=data.get("price_observed_at") or time.time(),
                        )
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
                        confirmed = data.get(engine.model.confirmed_key) or {}
                        bar_c = data.get("bar_c") or {}
                        if bar_c and confirmed.get("time") in (None, bar_c.get("time")):
                            await engine.on_bar(
                                symbol,
                                bar_c,
                                confirmed,
                            )
                except Exception:
                    logger.exception("Pine 持仓恢复异常 %s", engine.model.strategy_id)
