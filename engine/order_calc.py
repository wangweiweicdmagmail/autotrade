# -*- coding: utf-8 -*-
"""
下单参数计算 (Order Calculation)
=================================

根据 st_channel_www + EMA(7) 的指标值，计算下单参数：

- 方向：Up_Trend 有效=多头，Down_Trend 有效=空头。
- 止损（STOP 子单价）：多头=Up_Trend(ST)，空头=Down_Trend(ST)。
- EMA 限价单：EMA(7) 值。
- channel 边沿限价单：多头=Uptrend_Channel(ST+ATR)，空头=Downtrend_Channel(ST-ATR)。

两种限价单：① EMA(7) 限价单，② channel 边沿限价单；止损统一 ST 线。
限价单为父单，止损为子单（parentId 关联），平仓/撤父单时子单自动取消。
"""

import logging
from typing import Dict, Optional

logger = logging.getLogger(__name__)


def compute_order_params(extra: Dict[str, float],
                         symbol: str = "") -> Optional[dict]:
    """
    从 StockSignalData.extra 计算下单参数。

    :param extra: 含 st_up_trend/st_down_trend/st_up_channel/st_down_channel/ema7
    :return: {symbol, side, stop_px, ema_limit_px, channel_limit_px} 或 None（数据不全）
    """
    up = extra.get("st_up_trend")
    down = extra.get("st_down_trend")
    up_ch = extra.get("st_up_channel")
    down_ch = extra.get("st_down_channel")
    ema = extra.get("ema7")

    if up is not None and up > 0:
        side = "long"
        stop_px = up
        channel_px = up_ch
    elif down is not None and down > 0:
        side = "short"
        stop_px = down
        channel_px = down_ch
    else:
        return None

    if stop_px is None or ema is None or ema <= 0:
        return None

    # channel 边沿可能暂时无值（如趋势切换瞬间），允许为 None
    return {
        "symbol": symbol,
        "side": side,
        "stop_px": round(stop_px, 2),
        "ema_limit_px": round(ema, 2),
        "channel_limit_px": round(channel_px, 2) if channel_px else None,
    }


def calc_position(equity: float, pct: float,
                  limit_px: float, stop_px: float):
    """
    按止损风险定股数（用户分配的是"单笔风险占净值百分比"）。

    :param equity:   账户净值（美元）
    :param pct:      仓位风险百分比（如 0.01 = 1%）
    :param limit_px: 限价单价格
    :param stop_px:  STOP 止损价
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
    从实时快照的单标的数据计算完整下单参数（供控制台实时展示）。

    :param symbol_data: {bar:{...}, ema7:float, st:{up_trend,down_trend,
                        up_channel,down_channel,body_middle}}
    :param equity:      账户净值
    :param pct:         仓位风险百分比（如 0.01）
    :param limit_type:  'ema'（EMA 回踩）或 'channel'（ST 回踩，通道边沿）
    :return: 完整下单参数或 None（数据不全/方向未定）
    """
    st = symbol_data.get("st") or {}
    ema7 = symbol_data.get("ema7")
    bar = symbol_data.get("bar") or {}
    close = bar.get("close")

    up = st.get("up_trend")
    down = st.get("down_trend")
    up_ch = st.get("up_channel")
    down_ch = st.get("down_channel")

    if up is not None:
        side = "long"
        stop_px = up
        channel_px = up_ch
    elif down is not None:
        side = "short"
        stop_px = down
        channel_px = down_ch
    else:
        return None

    if limit_type == "channel":
        limit_px = channel_px
    else:
        limit_px = ema7

    if limit_px is None or limit_px <= 0 or stop_px is None or stop_px <= 0:
        return None

    qty, amount = calc_position(equity, pct, limit_px, stop_px)
    risk = round(abs(limit_px - stop_px) * qty, 2)

    return {
        "side": side,
        "limit_px": round(limit_px, 2),
        "stop_px": round(stop_px, 2),
        "ema7": round(ema7, 2) if ema7 else None,
        "channel_px": round(channel_px, 2) if channel_px else None,
        "close": round(close, 2) if close else None,
        "qty": qty,
        "amount": round(amount, 2),
        "risk": risk,
        "limit_type": limit_type,
    }
