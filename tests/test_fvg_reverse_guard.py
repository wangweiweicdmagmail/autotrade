# -*- coding: utf-8 -*-
"""持仓反向 FVG 守卫的决策函数回归（console.app._fvg_reverse_action）。

守的口径：只有「收盘确认的反向缺口」才触发动作——多仓遇空头 FVG(-1)、
空仓遇多头 FVG(+1)；浮亏 → close（市价平仓），浮盈/持平 → protect
（保本+提醒锁利）；同向缺口、价格/成本缺失一律 None。纯函数，零依赖。
"""
import pytest

from engine.execution_rules import fvg_reverse_action as _fvg_reverse_action


@pytest.mark.parametrize("side,event,avg_fill,price,expected", [
    # 多仓：空头 FVG(-1) 才算反向
    ("long", -1, 100.0, 99.0, "close"),      # 浮亏 → 市价平仓
    ("long", -1, 100.0, 101.0, "protect"),   # 浮盈 → 保本+提醒
    ("long", -1, 100.0, 100.0, "protect"),   # 持平按浮盈处理
    ("long", 1, 100.0, 99.0, None),          # 同向缺口不动作
    ("long", 0, 100.0, 99.0, None),          # 无缺口
    # 空仓：多头 FVG(+1) 才算反向（镜像）
    ("short", 1, 100.0, 101.0, "close"),
    ("short", 1, 100.0, 99.0, "protect"),
    ("short", -1, 100.0, 101.0, None),
    ("short", 0, 100.0, 101.0, None),
    # 数据缺失一律不动作
    ("long", -1, None, 99.0, None),
    ("long", -1, 100.0, None, None),
    ("long", -1, 100.0, 0, None),
    ("long", -1, 100.0, -1, None),
])
def test_fvg_reverse_action(side, event, avg_fill, price, expected):
    assert _fvg_reverse_action(side, event, avg_fill, price) == expected
