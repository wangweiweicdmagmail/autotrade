# -*- coding: utf-8 -*-
"""
策略计算模块 (Strategy Engine)
==============================

职责：
1. 定义标准数据结构 ``StockSignalData``（计算模块与控制模块/决策模块的通用语言）。
2. 对 TradingView 传回的指标做二次条件过滤、加权打分、降序排序。
3. 输出 Top N 股票列表，并序列化为标准 JSON（决策模块 Decision Agent 的数据接口）。

本模块不依赖任何网络/IO，纯函数实现，便于单元测试与独立复用。
"""

import logging
import time
from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 标准数据结构
# ---------------------------------------------------------------------------


@dataclass
class StockSignalData:
    """
    单只股票的指标信号快照。

    这是全系统的标准数据载体：
    - data_collector.py 负责从 TradingView 填充它；
    - strategy.py 负责过滤与打分；
    - 序列化后即为决策模块 (Decision Agent) 的输入 JSON。
    """

    symbol: str                     # 完整代码，如 "NASDAQ:AAPL"
    supertrend: int = 0             # SuperTrend 方向：1 多头 / -1 空头 / 0 未知
    rejection_score: float = 0.0    # 突破拒绝得分 0~100
    atr: float = 0.0                # ATR 波动率（绝对值）
    close: float = 0.0              # 最新收盘价（TV 报价会话回传）
    volume: float = 0.0             # 最新成交量
    timestamp: float = 0.0          # 数据时间戳（epoch 秒）
    extra: Dict[str, float] = field(default_factory=dict)  # 其他自定义指标扩展位

    def is_valid(self) -> bool:
        """数据有效性检查：时间戳存在且收盘价非零。"""
        return self.timestamp > 0 and self.close >= 0


@dataclass
class StrategyConfig:
    """策略过滤与打分参数（运行时可热替换）。"""

    # --- 硬性过滤条件 ---
    require_supertrend: Optional[int] = 1   # SuperTrend 方向要求（1 多/-1 空/None 不过滤）
    min_rejection_score: float = 60.0   # 拒绝得分下限
    min_atr: float = 0.0                # ATR 下限（过滤死水股）
    min_volume: float = 0.0             # 成交量下限
    require_attack_zone: Optional[int] = None  # 5M进攻区域方向值要求（None 不过滤）

    # --- 加权打分权重（用于同分股票间的排序） ---
    weight_rejection: float = 0.7       # 拒绝得分权重
    weight_atr_pct: float = 0.3         # ATR 百分比权重


# ---------------------------------------------------------------------------
# 打分与过滤
# ---------------------------------------------------------------------------


def compute_weighted_score(data: StockSignalData, cfg: StrategyConfig) -> float:
    """
    对已通过硬过滤的股票计算综合加权分。

    打分公式（可按策略需要自定义）：
        score = rejection_score * w1 + atr_percent * 100 * w2
    其中 atr_percent = atr / close，将波动率归一化为百分比，
    避免高价股天然占优。
    """
    atr_pct = (data.atr / data.close) if data.close > 0 else 0.0
    score = (
        data.rejection_score * cfg.weight_rejection
        + atr_pct * 100.0 * cfg.weight_atr_pct
    )
    return score


def passes_filter(data: StockSignalData, cfg: StrategyConfig) -> bool:
    """
    硬性条件过滤。

    当前规则（示例）：
        supertrend == 1  且  rejection_score > 60  且  atr/volume 达标
    自定义策略时仅需修改本函数或 StrategyConfig。
    """
    if not data.is_valid():
        return False
    if (cfg.require_supertrend is not None
            and data.supertrend != cfg.require_supertrend):
        return False
    if data.rejection_score < cfg.min_rejection_score:
        return False
    if data.atr < cfg.min_atr:
        return False
    if data.volume < cfg.min_volume:
        return False
    if (cfg.require_attack_zone is not None
            and data.extra.get("attack_zone_flag") != cfg.require_attack_zone):
        return False
    return True


def select_top_signals(
    signals: Dict[str, StockSignalData],
    cfg: Optional[StrategyConfig] = None,
    top_n: int = 9,
) -> List[StockSignalData]:
    """
    主策略入口：过滤 -> 打分 -> 降序 -> 取前 N。

    :param signals: symbol -> StockSignalData 的快照字典
    :param cfg:     策略配置，None 使用默认值
    :param top_n:   输出数量（八宫格为 8）
    :return:        按综合分降序排列的 StockSignalData 列表（长度 <= top_n）
    """
    cfg = cfg or StrategyConfig()

    candidates: List[StockSignalData] = []
    for symbol, data in signals.items():
        if passes_filter(data, cfg):
            candidates.append(data)
        else:
            logger.debug(
                "过滤掉 %s (supertrend=%s, rejection=%.1f, atr=%.3f)",
                symbol, data.supertrend, data.rejection_score, data.atr,
            )

    ranked = sorted(
        candidates,
        key=lambda d: compute_weighted_score(d, cfg),
        reverse=True,
    )
    top = ranked[:top_n]
    logger.info(
        "策略结果: 股票池=%d, 通过过滤=%d, 输出Top%d=%s",
        len(signals), len(candidates), len(top),
        [d.symbol for d in top],
    )
    return top


# ---------------------------------------------------------------------------
# 决策模块 (Decision Agent) 标准 JSON 接口
# ---------------------------------------------------------------------------
# 【接口约定】
#   to_decision_payload() 的输出即系统与 Decision Agent 之间的契约：
#     {
#       "version": 1,
#       "generated_at": <epoch 秒>,
#       "top_n": 9,
#       "signals": [ {StockSignalData 全字段 + "weighted_score"}, ... ]
#     }
#   Decision Agent 消费该 JSON，返回 {"symbols": [...]} 即可替换默认直通逻辑。
#   只要保持该 schema 稳定，决策模块可以用任何语言/进程独立实现。
# ---------------------------------------------------------------------------

SCHEMA_VERSION = 1


def to_decision_payload(
    ranked: List[StockSignalData],
    cfg: Optional[StrategyConfig] = None,
) -> dict:
    """把排序结果序列化为标准 JSON payload（决策模块输入）。"""
    cfg = cfg or StrategyConfig()
    return {
        "version": SCHEMA_VERSION,
        "generated_at": time.time(),
        "top_n": len(ranked),
        "signals": [
            {
                **asdict(d),
                "weighted_score": compute_weighted_score(d, cfg),
            }
            for d in ranked
        ],
    }


def symbols_from_decision(payload: dict) -> List[str]:
    """
    从决策模块（或默认直通逻辑）的返回中提取最终要灌入八宫格的 symbol 列表。

    默认直通：直接取 payload 中 signals 的 symbol 顺序。
    接入 Decision Agent 后，此函数解析 Agent 的返回 {"symbols": [...]}。
    """
    return [s["symbol"] for s in payload.get("signals", [])]
