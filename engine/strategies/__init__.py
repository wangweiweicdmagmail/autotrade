# -*- coding: utf-8 -*-
"""全自动策略注册表。新增策略在此登记即可被引擎/回测/控制台使用。"""

from engine.strategies.trend_pullback import TrendPullbackStrategy

STRATEGIES = {
    TrendPullbackStrategy.name: TrendPullbackStrategy,
}


def build_strategy(name: str, **params):
    """按名构建策略实例，params 覆盖默认参数。"""
    cls = STRATEGIES.get(name)
    if cls is None:
        raise ValueError(f"未知策略: {name}（可选: {list(STRATEGIES)}）")
    st = cls()
    if params:
        st.params = {**st.params, **params}
    return st
