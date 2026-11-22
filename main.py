# -*- coding: utf-8 -*-
"""
主程序入口 (Orchestrator)
=========================

v1 全流程（人机结合，架构见 PROJECT_NOTES 1.1）：

    TV 云端采集 (cloud_collector)
      -> 信号跳变检测 + 风控定量 (proposal_manager / risk)
        -> 提案卡人工确认（TV 右侧停靠栏）
          -> 批准 -> RiskGate 硬性限额 -> IBKR 下单 (ibkr/position)
      -> 宫格 Top N 刷新 (tv_control)
      -> 追踪止损棘轮：SuperTrend 线值驱动（每根 bar 收盘）

调度：
- 信号轮对齐 5 分钟 bar 收盘（ALIGN_TO_BAR_CLOSE + BAR_SECONDS +
  BAR_BUFFER_SEC 缓冲）；信号只用已收盘 bar（快照最新一根为进行中 bar，
  指标值会抖动，跳变检测基于收盘快照序列，天然只在收盘后推进）。
- 人工决策消费为独立的 1s 轮询协程，不阻塞采集/调度。

运行：
    python main.py            # dry-run（默认，EXECUTION_ENABLED=0）
    EXECUTION_ENABLED=1 python main.py   # 实盘下单（风控参数先确认！）

停止：Ctrl+C（优雅退出，关闭 WS / CDP / IBKR 连接）。
"""

import asyncio
import logging
import signal
import time
from typing import Dict, List, Optional

import config
from engine.data_collector import build_collector
from engine.proposal_manager import Proposal, ProposalManager
from engine.strategy import (
    StockSignalData,
    StrategyConfig,
    select_top_signals,
    symbols_from_decision,
    to_decision_payload,
)
from execution.ibkr import IBKRGateway
from execution.position import PositionManager
from execution.risk import RiskConfig, RiskGate
from execution.writer_lock import acquire_writer_lock

logger = logging.getLogger(__name__)


def build_controller():
    """按 config.CONTROLLER_IMPL 选择控制器实现（接口一致，可互换）。"""
    impl = getattr(config, "CONTROLLER_IMPL", "node")
    if impl == "python":
        from controller.tv_cdp import TradingViewCDPController
        logger.info("控制器: tv_cdp.py (纯 Python)")
        return TradingViewCDPController()
    from controller.tv_control_client import NodeTVController
    logger.info("控制器: tv_control.js (基于开源 tradingview-mcp)")
    return NodeTVController()

# ---------------------------------------------------------------------------
# 调度模式
# ---------------------------------------------------------------------------
ALIGN_TO_BAR_CLOSE = True    # 信号只在 bar 收盘有意义
BAR_SECONDS = 300            # 5 分钟 K 线
BAR_BUFFER_SEC = 5           # 收盘后缓冲数秒，等 TV 云端定型
DECISION_POLL_SEC = 1.0      # 人工决策轮询间隔（点击 -> 系统的延迟上限）


# ---------------------------------------------------------------------------
# 决策模块 (Decision Agent) 对接点
# ---------------------------------------------------------------------------
# 【数据接口说明】
#   输入: to_decision_payload() 生成的标准 JSON（见 strategy.py 接口约定）
#   输出: {"symbols": ["NASDAQ:AAPL", ...]}  —— 宫格展示排序
#
#   接入方式（三选一）：
#     1. 进程内:  直接 import 决策模块，替换下面的 default_decision_hook；
#     2. 本地 IPC: 把 payload 写入管道/socket/文件，读取 Agent 返回；
#     3. HTTP/gRPC: payload POST 给独立 Agent 服务。
#   当前为默认直通实现：不改变排序，原样透传 Top N。
# ---------------------------------------------------------------------------


async def default_decision_hook(payload: dict) -> list:
    """默认决策钩子：直通策略结果。替换此函数即可接入 Decision Agent。"""
    return symbols_from_decision(payload)


# ---------------------------------------------------------------------------
# 信号 -> 提案
# ---------------------------------------------------------------------------


def build_proposal(symbol: str, side: str, data: StockSignalData,
                   risk_gate: RiskGate,
                   equity: Optional[float] = None) -> Optional[Proposal]:
    """
    把信号跳变翻译成完整量化提案：
    - 入场 = 最新收盘价（下单时转 marketable LIMIT ±0.5%）
    - 止损 = 1 分钟 SuperTrend(10,2) 线值（extra.st_1m_line）；
      线值缺失或在不利一侧时退化为 entry ∓ 2×ATR(5M)
    - 跟踪幅度 = 2×ATR（TRAIL 子单 auxPrice，止损止盈一体）
    - 仓位 = 净值 × 0.1% ÷ |entry - 止损|（固定风险 0.1%）
    """
    entry = data.close
    atr = data.atr
    if entry <= 0:
        logger.warning("%s 无有效收盘价，跳过提案", symbol)
        return None
    if atr <= 0:
        logger.warning("%s ATR 无效（K线不足？），跳过提案", symbol)
        return None
    sign = 1 if side == "long" else -1
    trail_amount = round(config.TRAIL_ATR_MULT * atr, 4)
    stop = None
    st_line = data.extra.get("st_1m_line")
    if st_line:
        candidate = round(float(st_line), 2)
        # 方向合法性：多仓线值必须低于入场价，反之亦然
        if (side == "long" and candidate < entry) or \
           (side == "short" and candidate > entry):
            stop = candidate
    if stop is None:
        stop = round(entry - sign * config.TRAIL_ATR_MULT * atr, 2)
        logger.warning("%s 1M ST 线值缺失/方向不合，止损退化为 entry∓2×ATR",
                       symbol)
    qty = risk_gate.size_position(entry, stop, equity)
    if qty <= 0:
        logger.warning("%s 以损定量结果为 0（止损距离过大？），跳过", symbol)
        return None
    risk = round(qty * abs(entry - stop), 2)
    return Proposal(
        symbol=symbol, side=side, entry=entry, stop=stop, qty=qty,
        risk=risk, trail_amount=trail_amount,
        reason=f"supertrend 翻{'多' if side == 'long' else '空'} (5M)"
               f" · TRAIL {config.TRAIL_ATR_MULT}×ATR={trail_amount}",
    )


def side_allowed(side: str) -> bool:
    """按 STRATEGY_REQUIRE_SUPERTREND 过滤提案方向（1=仅多/-1=仅空）。"""
    req = config.STRATEGY_REQUIRE_SUPERTREND
    if req == 1 and side != "long":
        return False
    if req == -1 and side != "short":
        return False
    return True


# ---------------------------------------------------------------------------
# 单轮流水线（每根 bar 收盘触发）
# ---------------------------------------------------------------------------


async def run_pipeline(collector, controller, proposal_mgr: ProposalManager,
                       position_mgr: PositionManager, risk_gate: RiskGate,
                       strategy_cfg: StrategyConfig, gateway) -> None:
    """一轮：采集 -> 信号跳变 -> 提案弹卡 -> 宫格刷新。
    跟踪止损由 broker 端 TRAIL 单自动执行，无需每轮改价。"""
    t0 = time.monotonic()

    # 1) 采集：TradingView 指标快照
    snapshot = await collector.collect()

    # 2) 账户净值（以损定量基准；dry-run/未连接时为 None，降级固定额）
    equity = await gateway.net_liquidation()

    # 3) 信号跳变检测（supertrend 方向 0→±1）-> 提案
    signals: Dict[str, int] = {s: d.supertrend for s, d in snapshot.items()}
    edges = proposal_mgr.detect_edges(signals)
    proposals: List[Proposal] = []
    for sym, side in edges:
        if not side_allowed(side):
            logger.info("%s %s 方向被策略配置过滤，跳过", sym, side)
            continue
        p = build_proposal(sym, side, snapshot[sym], risk_gate, equity)
        if p:
            proposals.append(p)
    fresh = await proposal_mgr.on_bar_close(proposals)

    # 4) 宫格：提案标的占前 N 格（与卡片顺序一致），其余格用 Top N 补齐
    ranked = select_top_signals(snapshot, cfg=strategy_cfg, top_n=config.TOP_N)
    payload = to_decision_payload(ranked, cfg=strategy_cfg)
    symbols = await default_decision_hook(payload)
    prop_syms = [p.symbol for p in fresh]
    ordered = prop_syms + [s for s in symbols if s not in prop_syms]
    if ordered:
        await controller.apply_grid(ordered)

    logger.info("本轮流水线耗时 %.2fs", time.monotonic() - t0)


# ---------------------------------------------------------------------------
# 人工决策消费（独立协程，1s 轮询）
# ---------------------------------------------------------------------------


async def decision_loop(proposal_mgr: ProposalManager,
                        position_mgr: PositionManager,
                        stop_event: asyncio.Event) -> None:
    """轮询人工决策；批准的提案异步进执行层（风控在 open_position 内）。"""
    while not stop_event.is_set():
        try:
            approved = await proposal_mgr.poll()
            for p in approved:
                logger.info("提案批准，进入执行流程: %s %s qty=%d stop=%.2f",
                            p.symbol, p.side, p.qty, p.stop)
                asyncio.create_task(position_mgr.open_position(p))
        except Exception:
            logger.exception("决策轮询失败，继续")
        try:
            await asyncio.wait_for(stop_event.wait(),
                                   timeout=DECISION_POLL_SEC)
        except asyncio.TimeoutError:
            pass


# ---------------------------------------------------------------------------
# 调度循环
# ---------------------------------------------------------------------------


def _seconds_to_next_bar(bar_seconds: int) -> float:
    """距下一根 K 线收盘的秒数（对齐整点周期）+ 定型缓冲。"""
    now = time.time()
    return bar_seconds - (now % bar_seconds) + BAR_BUFFER_SEC


async def scheduler(stop_event: asyncio.Event) -> None:
    """主调度协程：建立长连接，循环触发流水线，异常自动重连。"""
    strategy_cfg = StrategyConfig(
        require_supertrend=config.STRATEGY_REQUIRE_SUPERTREND,
        min_rejection_score=config.STRATEGY_MIN_REJECTION_SCORE,
        min_atr=config.STRATEGY_MIN_ATR,
        min_volume=config.STRATEGY_MIN_VOLUME,
        require_attack_zone=config.STRATEGY_REQUIRE_ATTACK_ZONE,
    )
    risk_gate = RiskGate(RiskConfig(
        per_trade_pct=config.RISK_PER_TRADE_PCT,
        per_trade_usd=config.RISK_PER_TRADE_USD,
        max_order_qty=config.RISK_MAX_ORDER_QTY,
        max_open_positions=config.RISK_MAX_OPEN_POSITIONS,
        daily_loss_limit_usd=config.RISK_DAILY_LOSS_LIMIT_USD,
    ))

    while not stop_event.is_set():
        gateway = IBKRGateway.from_config()
        try:
            # IBKR 连接：dry-run 下失败仅告警（不阻塞全流程演练）；
            # 实盘开关打开但连不上时记 ERROR——提案弹卡照常但下单会失败
            connected = await gateway.connect()
            if not connected and config.EXECUTION_ENABLED:
                logger.error("EXECUTION_ENABLED=1 但 IBKR 未连接，"
                             "批准的提案将无法下单！")

            async with build_collector(config.WATCHLIST) as collector, \
                       build_controller() as controller:
                position_mgr = PositionManager(gateway, risk_gate)
                proposal_mgr = ProposalManager(controller)
                poller = asyncio.create_task(
                    decision_loop(proposal_mgr, position_mgr, stop_event))
                logger.info("连接就绪，开始调度 (bar 对齐 %ds + %ds 缓冲, "
                            "执行=%s)",
                            BAR_SECONDS, BAR_BUFFER_SEC,
                            "实盘" if config.EXECUTION_ENABLED else "dry-run")
                try:
                    while not stop_event.is_set():
                        try:
                            await run_pipeline(collector, controller,
                                               proposal_mgr, position_mgr,
                                               risk_gate, strategy_cfg,
                                               gateway)
                        except Exception:
                            logger.exception("单轮流水线失败，进入下一轮")

                        if ALIGN_TO_BAR_CLOSE:
                            delay = _seconds_to_next_bar(BAR_SECONDS)
                        else:
                            delay = config.POLL_INTERVAL
                        try:
                            await asyncio.wait_for(stop_event.wait(),
                                                   timeout=delay)
                        except asyncio.TimeoutError:
                            pass  # 到点触发下一轮
                finally:
                    poller.cancel()
        except Exception:
            # 连接级失败（TV 未启动 / WS 掉线）：等待后整体重连
            logger.exception("连接失败，%ds 后重连", 10)
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=10)
            except asyncio.TimeoutError:
                pass
        finally:
            await gateway.disconnect()


def main() -> None:
    if config.DATA_SOURCE != "mock":
        raise RuntimeError("历史main.py入口已退役；请使用 run_console.sh 的本地PineTS链路")
    logging.basicConfig(
        level=getattr(logging, config.LOG_LEVEL, logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    # ib_async 会把每个持仓/成交回报都打进 INFO，噪声极大；IBKR 的
    # 错误/提示已由 IBKRGateway._on_error 接管，这里压到 WARNING
    logging.getLogger("ib_async").setLevel(logging.WARNING)
    stop_event = asyncio.Event()

    async def runner() -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop_event.set)
        if not acquire_writer_lock("logs/autotrade_writer.lock"):
            raise RuntimeError("另一个 autotrade 执行写进程正在运行，禁止重复管理订单")
        await scheduler(stop_event)

    try:
        asyncio.run(runner())
    except KeyboardInterrupt:
        pass
    logger.info("已退出")


if __name__ == "__main__":
    main()
