# -*- coding: utf-8 -*-
"""
trend_pullback：trend_state 回踩策略
=====================================

进场（tick 触发，指标用上一根已收盘 5M 的确认值 ts_c）：
- trend_state == 1（单边多头）：实时成交价触及 ema_fast / channel_edge /
  fvg_last_top（price <= 线值）→ 做多候选；多线同触取更低价（更优入场）
- trend_state == -1（单边空头）：price >= 线值（ema_fast / channel_edge /
  fvg_last_bottom）→ 做空候选；取更高价
- FVG 线 = 最近同向 FVG 的区沿（需求区上沿 / 供应区下沿），可用
  require_same_dir_fvg / min_fvg_cum 追加方向与累积计数过滤（缺失即拦截）
- 限价 = 线值；止损 = 限价 ∓ N×ATR（ts_c.atr，N = params.stop_atr_mult，
  默认取 config.STOP_ATR_MULT 全局默认）；方向 = ST 方向，无反向订单
- 实时候选经人工确认后下单；回测关闭确认以评估原始信号
- 时段闸：默认 09:45–15:00 ET 之外不开仓

管理（引擎统一执行）：限价单 / STP 止损 / 3 根 bar 过期撤单 /
2R 移动保本 / 保本后 supertrend 线单向跟踪 / 趋势反向平仓。
"""

import logging
from typing import Optional

import config
from engine.auto_engine import AutoStrategy, EntryPlan

logger = logging.getLogger(__name__)


class TrendPullbackStrategy(AutoStrategy):
    name = "trend_pullback"

    def entry_trigger(self, ctx: dict, symbol: str,
                      price: float) -> Optional[EntryPlan]:
        ts_c = ctx.get("ts_c") or {}
        direction = ts_c.get("direction")     # SuperTrend 方向（1 多 / -1 空）
        if direction not in (1, -1):
            return None
        atr = ts_c.get("atr")
        if not atr or atr <= 0:
            return None
        fvg_c = ctx.get("fvg_c") or {}
        fvg_dir = fvg_c.get("fvg_last_dir")   # 最近 FVG 方向（1 需求 / -1 供应）
        # FVG 过滤：缺失值视为不满足，直接拦截（不静默放行）
        if self.params.get("require_same_dir_fvg") and fvg_dir != direction:
            return None
        min_cum = self.params.get("min_fvg_cum", 0)
        if min_cum > 0:
            fvg_cum = fvg_c.get("fvg_cum_count")
            if fvg_cum is None or fvg_cum < min_cum:
                return None
        side = "long" if direction == 1 else "short"
        # tick 价触及线值（多: price <= 线 / 空: price >= 线），多线同触取更优价
        touched = []
        for key, name in (("ema_fast", "ema"), ("channel_edge", "channel")):
            line = ts_c.get(key)
            if not line or line <= 0:
                continue
            if (side == "long" and price <= line) or \
               (side == "short" and price >= line):
                touched.append((name, line))
        # FVG 区沿线：多 = 需求区上沿，空 = 供应区下沿
        if self.params.get("use_fvg_line"):
            if side == "long" and fvg_dir == 1:
                fvg_line = fvg_c.get("fvg_last_top")
                if fvg_line is not None and price <= fvg_line:
                    touched.append(("fvg", fvg_line))
            elif side == "short" and fvg_dir == -1:
                fvg_line = fvg_c.get("fvg_last_bottom")
                if fvg_line is not None and price >= fvg_line:
                    touched.append(("fvg", fvg_line))
        if not touched:
            return None
        line_name, line_px = (min(touched, key=lambda x: x[1]) if side == "long"
                              else max(touched, key=lambda x: x[1]))
        sign = 1 if side == "long" else -1
        # 止损：优先读 smart_stop 指标的止损距离（所见即所得，3 根极值 + ATR 钳制）；
        # 缺失回退 限价 ∓ N×ATR（N = params stop_atr_mult，缺省取 config 默认）
        bar_close = (ctx.get("bar_c") or {}).get("close")
        ss = ts_c.get("smart_stop_long") if side == "long" \
            else ts_c.get("smart_stop_short")
        dist = None
        if ss is not None and bar_close is not None:
            d = (bar_close - ss) if side == "long" else (ss - bar_close)
            if d > 0:
                dist = d
        if dist is None:
            mult = float(self.params.get("stop_atr_mult", config.STOP_ATR_MULT))
            dist = atr * mult
        stop = round(line_px - sign * dist, 2)
        if stop <= 0:
            return None
        return EntryPlan(
            symbol=symbol, side=side, limit_px=round(line_px, 2),
            stop_px=stop, line=line_name,
            reason=f"ST方向={direction} 回踩{line_name}线 {line_px:.2f}")
