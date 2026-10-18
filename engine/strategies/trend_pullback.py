# -*- coding: utf-8 -*-
"""
trend_pullback：trend_state 回踩策略
=====================================

进场（tick 触发，指标用上一根已收盘 5M 的确认值 ts_c）：
- trend_state == 1（单边多头）：实时成交价触及 ema_fast 或 channel_edge
  （price <= 线值）→ 做多候选；两线同触取更低价（更优入场）
- trend_state == -1（单边空头）：price >= 线值 → 做空候选；取更高价
- 限价 = 线值；止损 = 限价 ∓ 1×ATR（ts_c.atr）；方向 = ST 方向，无反向订单
- 实时候选经人工确认后下单；回测关闭确认以评估原始信号
- 时段闸：默认 09:45–15:00 ET 之外不开仓

管理（引擎统一执行）：限价单 / STP 止损 / 3 根 bar 过期撤单 /
2R 移动保本 / 保本后 supertrend 线单向跟踪 / 趋势反向平仓。
"""

import logging
from typing import Optional

from engine.auto_engine import AutoStrategy, EntryPlan

logger = logging.getLogger(__name__)


class TrendPullbackStrategy(AutoStrategy):
    name = "trend_pullback"

    def entry_trigger(self, ctx: dict, symbol: str,
                      price: float) -> Optional[EntryPlan]:
        ts_c = ctx.get("ts_c") or {}
        trend = ts_c.get("trend_state")
        if trend not in (1, -1):
            return None
        atr = ts_c.get("atr")
        if not atr or atr <= 0:
            return None
        side = "long" if trend == 1 else "short"
        # tick 价触及线值（多: price <= 线 / 空: price >= 线），两线同触取更优价
        touched = []
        for key, name in (("ema_fast", "ema"), ("channel_edge", "channel")):
            line = ts_c.get(key)
            if not line or line <= 0:
                continue
            if (side == "long" and price <= line) or \
               (side == "short" and price >= line):
                touched.append((name, line))
        if not touched:
            return None
        line_name, line_px = (min(touched, key=lambda x: x[1]) if side == "long"
                              else max(touched, key=lambda x: x[1]))
        sign = 1 if side == "long" else -1
        stop = round(line_px - sign * atr, 2)
        if stop <= 0:
            return None
        return EntryPlan(
            symbol=symbol, side=side, limit_px=round(line_px, 2),
            stop_px=stop, line=line_name,
            reason=f"trend_state={trend} 回踩{line_name}线 {line_px:.2f}")
