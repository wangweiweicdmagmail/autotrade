"""历史持仓叠加规则的纯函数；不产生 Pine 入场信号。"""

from typing import Optional


def fvg_reverse_action(
    side: str, fvg_event: int, avg_fill: Optional[float], price: Optional[float]
) -> Optional[str]:
    """反向 FVG 收盘确认后的建议分类（纯函数，不授权任何订单操作）：
    同向缺口/数据缺失 → None；浮亏 → "close"；浮盈（含持平）→ "protect"。"""
    if side == "long" and fvg_event != -1:
        return None
    if side == "short" and fvg_event != 1:
        return None
    if not avg_fill or not price or price <= 0:
        return None
    floating = price - avg_fill if side == "long" else avg_fill - price
    return "close" if floating < 0 else "protect"
