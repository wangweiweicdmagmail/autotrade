# -*- coding: utf-8 -*-
"""
下单参数计算 (Order Calculation)
=================================

根据 trend_state 指标（5M）的输出值计算下单参数：

- 方向：direction = 1 多头 / -1 空头（Up_Trend / Down_Trend 谁有效），
  下单方向与 trend_state 同向（指标内部已保证一致，无反向订单）。
- 限价：两条回踩入场线 —— ema_fast（EMA20）或 channel_edge（ST±1×ATR）。
- 止损（STP 子单触发价）：限价 ∓ 1×ATR（atr 为指标 Atr_Value）。

限价单为父单，STP 止损为子单（parentId 关联），平仓/撤父单时子单自动取消。
"""

import logging
from typing import Dict, Optional

logger = logging.getLogger(__name__)


def compute_order_params(ts: Dict[str, float],
                         symbol: str = "") -> Optional[dict]:
    """
    从 trend_state 的一组值（ts 或 ts_c）计算下单参数。

    :param ts: 含 direction/ema_fast/channel_edge/atr/supertrend/trend_state
    :return: {symbol, side, stop_px, ema_limit_px, channel_limit_px, atr} 或 None
    """
    direction = ts.get("direction")
    ema = ts.get("ema_fast")
    channel = ts.get("channel_edge")
    atr = ts.get("atr")

    if direction == 1:
        side = "long"
    elif direction == -1:
        side = "short"
    else:
        return None
    if ema is None or ema <= 0 or atr is None or atr <= 0:
        return None

    sign = 1 if side == "long" else -1
    return {
        "symbol": symbol,
        "side": side,
        "ema_limit_px": round(ema, 2),
        "channel_limit_px": round(channel, 2) if channel and channel > 0 else None,
        "stop_px": round(ema - sign * atr, 2),          # EMA 限价的止损
        "channel_stop_px": (round(channel - sign * atr, 2)
                            if channel and channel > 0 else None),
        "atr": round(atr, 4),
    }


def calc_position(equity: float, pct: float,
                  limit_px: float, stop_px: float):
    """
    按止损风险定股数（用户分配的是"单笔风险占净值百分比"）。

    :param equity:   账户净值（美元）
    :param pct:      仓位风险百分比（如 0.01 = 1%）
    :param limit_px: 限价单价格
    :param stop_px:  STP 止损价
    :return: (qty, amount)  股数与占用金额
    """
    if not equity or equity <= 0 or limit_px <= 0 or stop_px <= 0:
        return 0, 0.0
    risk_usd = equity * pct
    dist = abs(limit_px - stop_px)
    if dist <= 0:
        return 0, 0.0
    qty = int(risk_usd / dist)
    # 封顶：占用金额不超过全部净值（不能超仓）
    max_qty = int(equity / limit_px)
    qty = min(qty, max_qty)
    amount = round(qty * limit_px, 2)
    return qty, amount


def compute_live_order(symbol_data: dict, equity: float, pct: float,
                       limit_type: str = "ema") -> Optional[dict]:
    """
    从实时快照的单标的数据计算完整下单参数（方向/限价/止损/股数）。
    策略用确认值 ts_c（上一根已收盘），不用盘中实时值。

    :param symbol_data: {bar:{...}, bar_c2:{...}, ts:{...}, ts_c:{...}}
    :param equity:      账户净值
    :param pct:         仓位风险百分比（如 0.01）
    :param limit_type:  'ema'（EMA 快线回踩）/ 'channel'（通道边沿回踩）/
                        'retest'（上上根 K 线极值回踩，止损取 bar_c2 另一端极值，
                        不依赖 ATR）
    :return: 完整下单参数或 None（数据不全/方向未定/震荡）
    """
    ts = symbol_data.get("ts_c") or symbol_data.get("ts") or {}
    bar = symbol_data.get("bar") or {}
    close = bar.get("close")

    p = compute_order_params(ts, "")
    if p is None:
        return None
    side = p["side"]
    sign = 1 if side == "long" else -1

    if limit_type == "retest":
        bar2 = symbol_data.get("bar_c2") or {}
        high2, low2 = bar2.get("high"), bar2.get("low")
        if high2 is None or low2 is None:
            return None
        limit_px = round(high2 if side == "long" else low2, 2)
        stop_px = round(low2 if side == "long" else high2, 2)
        if limit_px <= 0 or stop_px <= 0 or stop_px == limit_px:
            return None
    else:
        if limit_type == "channel":
            limit_px = ts.get("channel_edge")
        else:
            limit_px = ts.get("ema_fast")
        atr = ts.get("atr")
        if limit_px is None or limit_px <= 0 or atr is None or atr <= 0:
            return None
        stop_px = round(limit_px - sign * atr, 2)
    if stop_px <= 0:
        return None

    qty, amount = calc_position(equity, pct, limit_px, stop_px)
    risk = round(abs(limit_px - stop_px) * qty, 2)

    return {
        "side": side,
        "limit_px": round(limit_px, 2),
        "stop_px": stop_px,
        "ema_fast": p["ema_limit_px"],
        "channel_px": p["channel_limit_px"],
        "atr": p["atr"],
        "close": round(close, 2) if close else None,
        "qty": qty,
        "amount": round(amount, 2),
        "risk": risk,
        "limit_type": limit_type,
    }
