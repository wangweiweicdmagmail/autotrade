"""automatic application functions and owned state. No console.app dependency."""

from console.services.auto import AutoServiceError
from fastapi import HTTPException
from typing import Optional
import asyncio
import config
import time
from console.shared import logger
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

    async def api_auto_models(self) -> dict:
        return {"models": self.context.auto_service.registry.describe()}

    async def api_auto_start(self, req: AutoStartRequest, trader: str = None) -> dict:
        self.context.auto_service.watchlist = config.WATCHLIST
        try:
            result = self.context.auto_service.start(
                req.strategy, req.symbols, req.params, req.instance_id
            )
        except AutoServiceError as error:
            raise HTTPException(error.status, error.detail)
        (
            self.context.automatic._auto_enabled,
            self.context.automatic._auto_strategy_name,
        ) = (self.context.auto_service.enabled, self.context.auto_service.strategy_name)
        return result

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
                req.initial_risk_usd,
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
