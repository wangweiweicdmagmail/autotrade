"""Legacy PoC mock source only. Cloud/WS indicator registration is retired."""
import logging
import random
import time
from typing import Dict, List
import config
from engine.strategy import StockSignalData
logger=logging.getLogger(__name__)

class TradingViewDataCollector:
    def __init__(self,*args,**kwargs):
        raise RuntimeError("旧TV私有指标通道已退役；请使用控制台的PineTS链路")

class MockDataCollector:
    """
    模拟采集器：随机游走生成伪指标数据。

    与真实采集器保持同一接口（async with + collect()），
    使 main.py 无需关心数据源差异（计算模块与控制模块解耦）。
    """

    def __init__(self, watchlist: List[str], timeframe: str = "5"):
        del timeframe
        self._state: Dict[str, Dict[str, float]] = {}
        for symbol in watchlist:
            self._state[symbol] = {
                "close": random.uniform(50, 500),
                "supertrend": random.choice([-1, 1]),
                "rejection_score": random.uniform(0, 100),
                "atr": random.uniform(0.5, 8.0),
                "volume": random.uniform(1e5, 5e6),
            }

    async def __aenter__(self) -> "MockDataCollector":
        return self

    async def __aexit__(self, *exc) -> None:
        return None

    async def collect(self) -> Dict[str, StockSignalData]:
        now = time.time()
        snapshot: Dict[str, StockSignalData] = {}
        for symbol, s in self._state.items():
            # 随机游走：模拟指标随行情演化
            s["close"] *= 1 + random.uniform(-0.005, 0.005)
            s["rejection_score"] = min(
                100.0, max(0.0,
                           s["rejection_score"] + random.uniform(-8, 8)))
            s["atr"] = max(0.1, s["atr"] + random.uniform(-0.3, 0.3))
            if random.random() < 0.1:  # 10% 概率 SuperTrend 翻转
                s["supertrend"] *= -1
            snapshot[symbol] = StockSignalData(
                symbol=symbol,
                supertrend=int(s["supertrend"]),
                rejection_score=round(s["rejection_score"], 2),
                atr=round(s["atr"], 3),
                close=round(s["close"], 2),
                volume=s["volume"],
                timestamp=now,
            )
        return snapshot


def build_collector(watchlist: List[str]):
    if getattr(config,"DATA_SOURCE","retired") != "mock":
        raise RuntimeError("历史采集入口已退役；请使用 run_console.sh，不再注册TV指标")
    return MockDataCollector(watchlist)
