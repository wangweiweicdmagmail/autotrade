"""accounts application functions and owned state. No console.app dependency."""

from typing import Optional
import time


class AccountsService:
    def __init__(self, context):
        self.context = context

    async def get_status_cached(self) -> dict:
        """净值/日盈亏缓存（每 10s 刷新一次）。"""
        if time.time() - self.context.accounts._equity_cache["ts"] > 10.0:
            self.context.accounts._equity_cache[
                "equity"
            ] = await self.context.gateway.net_liquidation()
            self.context.accounts._equity_cache[
                "daily_pnl"
            ] = await self.context.gateway.daily_pnl()
            self.context.accounts._equity_cache["ts"] = time.time()
        return self.context.accounts._equity_cache

    def _total_position_pct(self, equity) -> Optional[float]:
        """全部开放 lot 市值合计 ÷ 净值 ×100；净值为空返回 None。

        市值口径：PENDING 用 qty×entry_px（计划值）；ACTIVE/CLOSING 用
        filled_qty×last_px（last_px≤0 时回退 avg_fill/entry_px）。
        """
        if not equity or equity <= 0:
            return None
        amount = 0.0
        for lot in self.context.order_mgr.list_lots(active_only=True):
            if lot["state"] == "PENDING":
                amount += lot["qty"] * lot["entry_px"]
            else:
                px = lot["last_px"] or lot["avg_fill"] or lot["entry_px"]
                amount += lot["filled_qty"] * px
        return round(amount / equity * 100, 1)

    def _tv_last_price(self, symbol: str) -> Optional[float]:
        """TV 现价（唯一价格源）：collector 实时 bar close；不可用返回 None。
        全系统统一用 TV，不依赖 TWS 行情权限（10089 延时通道）。"""
        data = (self.context.collector.latest() or {}).get(symbol) or {}
        close = (data.get("bar") or {}).get("close")
        return close if close and close > 0 else None

    def _refresh_marks_from_tv(self) -> None:
        """用 TV 实时价刷新 ACTIVE lot 的 mark 价/PnL/MAE/MFE（每秒调用，不落盘）。
        替代昂贵的 IBKR last_price 轮询——collector 里本来就有全标的实时收盘价。"""
        prices: dict = {}
        for sym, data in self.context.collector.latest().items():
            bar = data.get("bar") or {}
            close = bar.get("close")
            if close and close > 0:
                prices[sym] = close
        if prices:
            self.context.order_mgr.apply_marks(prices, save=False)

    async def api_refresh(self) -> dict:
        self.context.accounts._refresh_marks_from_tv()
        return await self.context.snapshot.build_snapshot()
