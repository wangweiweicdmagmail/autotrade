"""automatic application functions and owned state. No console.app dependency."""

from dataclasses import replace
from console.services.auto import AutoServiceError
from fastapi import HTTPException
from typing import Optional
import asyncio
import config
import time
from console.shared import logger
from engine.live_audit import decision_market_frame
from console.schemas import AutoAdoptRequest, AutoRejectRequest, AutoStartRequest


class AutomaticService:
    def __init__(self, context):
        self.context = context

    def _emit_auto(self, event_type: str, **data) -> None:
        self.context.events.record_event(
            f"auto_{event_type}" if not event_type.startswith("auto_") else event_type,
            **data,
        )
        if event_type not in self.context.automatic._AUTO_NOTICE_EVENTS:
            return
        if event_type == "pyramid_flat" and (not data.get("had_fill")):
            return
        notice_id = f"{time.time_ns()}:{event_type}:{data.get('symbol', '')}"
        self.context.automatic._auto_notices[notice_id] = {
            "id": notice_id,
            "event": event_type,
            "ts": time.time(),
            **data,
        }
        while len(self.context.automatic._auto_notices) > 100:
            self.context.automatic._auto_notices.pop(
                next(iter(self.context.automatic._auto_notices))
            )

    async def _auto_loop(self) -> None:
        from datetime import datetime
        from zoneinfo import ZoneInfo

        while True:
            await asyncio.sleep(1.0)
            token = decision_market_frame.set(self.context.collector.market_frame_id)
            try:
                now = datetime.now(ZoneInfo("America/New_York"))
                await self.context.strategy_runtime.step(
                    self.context.collector.latest(),
                    now.hour * 60 + now.minute,
                    trading=self.context.system._is_trading_hours(),
                    stale=self.context.collector.data_health()["stale"],
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Pine 执行调度异常")
            finally:
                decision_market_frame.reset(token)

    async def api_auto_models(self) -> dict:
        return {"models": self.context.auto_service.registry.describe()}

    async def api_auto_start(self, req: AutoStartRequest, trader: str = None, *, preview=False) -> dict:
        self.context.auto_service.watchlist = config.WATCHLIST
        try:
            result = self.context.auto_service.start(
                req.strategy, req.symbols, {"initial_risk_usd": config.RISK_PER_TRADE_USD}, req.instance_id,
                preview=preview, confirmed_directions=req.confirmed_directions,
            )
        except AutoServiceError as error:
            raise HTTPException(error.status, error.detail)
        if preview:
            return result
        (
            self.context.automatic._auto_enabled,
            self.context.automatic._auto_strategy_name,
        ) = (self.context.auto_service.enabled, self.context.auto_service.strategy_name)
        return result

    async def api_golden_entry(self, req, trader: str) -> dict:
        from datetime import datetime
        from zoneinfo import ZoneInfo
        from domain.contracts import PriceObservation
        from engine.opening_range import opening_entry_block

        context = self.context
        service = context.auto_service
        symbol = service.symbol(req.symbol)
        if req.direction == -1 and not config.ALLOW_SHORT:
            raise HTTPException(409, "当前配置禁止做空")
        if symbol not in config.WATCHLIST:
            raise HTTPException(400, "标的不在股票池内")
        now = time.time()
        if not context.system._is_trading_hours() or not req.bar_time <= now < req.bar_time + 300:
            raise HTTPException(409, "黄金信号已过有效期或当前不在交易时段")
        if context.collector.data_health()["stale"]:
            raise HTTPException(409, "行情陈旧，无法开仓")
        data = (context.collector.latest() or {}).get(symbol) or {}
        model = service.registry.get("golden_strategy")
        try:
            observation = PriceObservation.from_market(symbol, data, now)
        except ValueError as error:
            raise HTTPException(409, str(error)) from error
        output = model.from_market(data)
        if (observation.time_basis != "collector_observed" or not 0 <= now - observation.observed_at <= 15
                or observation.bar_open_time != req.bar_time or output.time != req.bar_time
                or output.direction not in (1, -1) or output.direction != req.direction or output.errors
                or output.entry_price is None or output.stop_px is None or output.stop_px <= 0
                or (output.entry_price - output.stop_px) * output.direction <= 0
                or (observation.price - output.stop_px) * output.direction <= 0):
            raise HTTPException(409, "黄金信号已变化或保护价无效，请刷新后核对")
        if not req.manual_window and not output.entry_signal:
            raise HTTPException(409, "黄金信号未触线，请等待回撤")
        if context.strategy_runtime.require_opening_range:
            reason = opening_entry_block(data, output.direction, observation.price, as_of=now)
            if reason:
                raise HTTPException(409, reason)
        instance_id = "golden-click"
        engine = service.engine(model.strategy_id, instance_id)
        instance = engine.get_instance(symbol)
        if instance and instance.state != "DONE":
            raise HTTPException(409, "该标的已有一键开仓实例，请查看执行监控")
        try:
            result = service.start(model.strategy_id, [symbol], {}, instance_id,
                                   entry_scope=(req.bar_time, req.direction))
        except AutoServiceError as error:
            raise HTTPException(error.status, error.detail) from error
        if symbol not in result["started"]:
            raise HTTPException(409, "执行实例未启动，请检查实例上限")
        instance = engine.get_instance(symbol)
        et = datetime.fromtimestamp(now, ZoneInfo("America/New_York"))
        token = decision_market_frame.set(getattr(context.collector, "market_frame_id", None))
        try:
            context.events.record_event("golden_entry_click", symbol=symbol, trader=trader,
                                        direction=req.direction, bar_time=req.bar_time,
                                        manual_window=req.manual_window, retracement_triggered=output.entry_signal)
            if req.manual_window:
                # This request explicitly authorizes one entry in the validated bar.
                output = replace(output, entry_signal=True)
            await engine.on_observation(observation, output, et.hour * 60 + et.minute)
        finally:
            decision_market_frame.reset(token)
            if not instance.lot_ids:
                engine.stop_symbol(symbol)
        if not instance.lot_ids:
            raise HTTPException(409, instance.last_decision_reason or "开仓未确认，请查看执行监控")
        self._auto_enabled = service.enabled
        self._auto_strategy_name = service.strategy_name
        return {"ok": True, "lot_id": instance.lot_ids[-1]}

    async def api_auto_stop(
        self, close_positions: bool = False, trader: str = None
    ) -> dict:
        result = await self.context.auto_service.stop(close_positions)
        self.context.automatic._auto_enabled = False
        return result

    def _auto_instances(self) -> list:
        rows = self.context.auto_service.instances()
        equity = self.context.accounts._equity_cache.get("equity") or 0
        for row in rows:
            row["max_position_pct"] = (
                round(row["max_notional"] / equity * 100, 2) if equity > 0 else None
            )
        return rows

    async def api_auto_status(self) -> dict:
        """金字塔策略状态：首仓预算、建仓次数、平均成本、解锁与最大仓位。"""
        return {
            "enabled": self.context.automatic._auto_enabled,
            "strategy": self.context.automatic._auto_strategy_name,
            "models": self.context.auto_service.registry.describe(),
            "max_instances": config.AUTO_MAX_INSTANCES,
            "instances": self.context.automatic._auto_instances(),
            "proposals": [],
        }

    async def api_auto_adopt(self, req: AutoAdoptRequest, trader: str = None) -> dict:
        try:
            result = await self.context.auto_service.adopt(
                req.symbol,
                req.lot_id,
                config.RISK_PER_TRADE_USD,
                req.strategy,
                req.instance_id,
            )
        except AutoServiceError as error:
            raise HTTPException(error.status, error.detail)
        (
            self.context.automatic._auto_enabled,
            self.context.automatic._auto_strategy_name,
        ) = (self.context.auto_service.enabled, self.context.auto_service.strategy_name)
        return result

    async def api_auto_instance_stop(
        self,
        symbol: str,
        close_positions: bool = False,
        trader: str = None,
        strategy: str = "ema9_pyramid",
        instance_id: str = "default",
    ) -> dict:
        try:
            return await self.context.auto_service.stop_instance(
                symbol, close_positions, strategy, instance_id
            )
        except AutoServiceError as error:
            raise HTTPException(error.status, error.detail)

    async def api_auto_instance_breakeven(self, symbol, req, trader=None,
                                         strategy="ema9_pyramid", instance_id="default"):
        try:
            return await self.context.auto_service.breakeven_instance(
                symbol, req.group_id, req.qty, req.avg_cost, strategy, instance_id)
        except AutoServiceError as error:
            raise HTTPException(error.status, error.detail)

    async def api_auto_proposal_approve(
        self, proposal_id: str, trader: str = None
    ) -> dict:
        raise HTTPException(410, "EMA9 金字塔为全自动策略，无人工候选")

    async def api_auto_proposal_reject(
        self,
        proposal_id: str,
        req: Optional[AutoRejectRequest] = None,
        trader: str = None,
    ) -> dict:
        raise HTTPException(410, "EMA9 金字塔为全自动策略，无人工候选")
