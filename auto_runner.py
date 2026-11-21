# -*- coding: utf-8 -*-
"""
策略实盘运行器 (auto_runner.py)
================================

脱离 Web 控制台的 headless 运行形态（与控制台 /api/auto 共用同一
StrategyEngine + LiveBroker）。开仓候选必须人工确认，后续管理自动执行。

用法：
    python3 auto_runner.py --strategy trend_pullback --symbols NASDAQ:NVDA,NASDAQ:QQQ
    EXECUTION_ENABLED=1 python3 auto_runner.py --strategy trend_pullback --symbols ...  # 实盘

确认：TTY 输入 approve <proposal_id> / reject <proposal_id> [原因]；
非 TTY 只提醒候选，不会自动批准。Ctrl+C 优雅退出，存量 lot 由 TWS 保护单托管。
"""

import argparse
import asyncio
import logging
import signal
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))

import config
from engine.auto_engine import LiveBroker, StrategyEngine
from engine.realtime_collector import RealtimeCollector
from engine.strategies import build_strategy
from execution.ibkr import IBKRGateway
from execution.order_lot import OrderManager
from execution.risk import RiskConfig, RiskGate
from execution.writer_lock import acquire_writer_lock

logger = logging.getLogger("auto_runner")
ET = ZoneInfo("America/New_York")


def _is_trading_hours() -> bool:
    now = datetime.now(ET)
    if now.weekday() >= 5:
        return False
    minutes = now.hour * 60 + now.minute
    return 9 * 60 + 30 <= minutes < 16 * 60


def record_auto_event(event_type: str, **data) -> None:
    import json as _json
    try:
        day = time.strftime("%Y%m%d")
        path = Path(__file__).resolve().parent / "logs" / f"auto-{day}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(_json.dumps({"ts": time.time(), "type": event_type,
                                 **data}, ensure_ascii=False) + "\n")
    except OSError as e:
        logger.warning("事件写入失败: %s", e)


def _stdin_loop(loop: asyncio.AbstractEventLoop, queue: asyncio.Queue,
                stop_event: asyncio.Event) -> None:
    while not stop_event.is_set():
        try:
            line = sys.stdin.readline()
        except (EOFError, KeyboardInterrupt):
            return
        if not line:
            return
        try:
            loop.call_soon_threadsafe(queue.put_nowait, line.strip())
        except RuntimeError:
            return


async def _handle_decision(command: str, engine: StrategyEngine,
                           market: dict) -> None:
    parts = command.strip().split()
    if not parts:
        return
    action = parts[0].lower()
    if action not in ("approve", "reject") or len(parts) < 2:
        logger.info("命令: approve <proposal_id> / reject <proposal_id> [原因]")
        return
    proposal_id = parts[1]
    if action == "approve":
        proposal = next((p for p in engine.proposals()
                         if p["proposal_id"] == proposal_id), None)
        bar_c = {}
        if proposal:
            bar_c = (market.get(proposal["symbol"], {})
                     .get("bar_c") or {})
        result, error = await engine.approve_entry(proposal_id, bar_c=bar_c)
    else:
        result, error = await engine.reject_entry(
            proposal_id, reason=" ".join(parts[2:]))
    if error:
        logger.warning("候选 %s 处理失败: %s", proposal_id, error)
    else:
        logger.info("候选 %s 处理完成: %s", proposal_id, result)


async def run(strategy_name: str, symbols: list, params: dict) -> None:
    config.validate_config()
    if not acquire_writer_lock("logs/autotrade_writer.lock"):
        raise RuntimeError("另一个 autotrade 执行写进程正在运行，禁止重复管理订单")
    gateway = IBKRGateway.from_config()
    risk_gate = RiskGate(RiskConfig(
        per_trade_pct=config.RISK_PER_TRADE_PCT,
        per_trade_usd=config.RISK_PER_TRADE_USD,
        max_order_qty=config.RISK_MAX_ORDER_QTY,
        max_open_positions=config.RISK_MAX_OPEN_POSITIONS,
        max_total_risk_usd=config.RISK_MAX_TOTAL_RISK_USD,
        max_direction_risk_usd=config.RISK_MAX_DIRECTION_RISK_USD,
        daily_loss_limit_usd=config.RISK_DAILY_LOSS_LIMIT_USD,
    ))
    order_mgr = OrderManager(gateway, risk_gate)

    def emit(event_type: str, **data) -> None:
        record_auto_event(event_type, **data)
        if event_type == "auto_entry_proposed":
            logger.warning(
                "开仓候选 %s %s %s线 @%.2f 损 %.2f；输入 approve %s 确认，"
                "或 reject %s 忽略",
                data["symbol"], data["side"], data["line"],
                data["limit_px"], data["stop_px"], data["proposal_id"],
                data["proposal_id"])

    engine = StrategyEngine(
        LiveBroker(order_mgr), risk_gate=risk_gate, gateway=gateway,
        emit=emit, max_instances=config.AUTO_MAX_INSTANCES,
        require_entry_confirmation=True)

    connected = await gateway.connect()
    if config.EXECUTION_ENABLED and not connected:
        logger.error("EXECUTION_ENABLED=1 但 IBKR 未连接，下单将失败")
    if connected:
        await order_mgr.reconcile()

    collector = RealtimeCollector(symbols)
    await collector.start()

    strategy = build_strategy(strategy_name, **params)
    started = engine.start(strategy, symbols)
    logger.info("策略 %s 启动: %s（dry_run=%s）", strategy_name, started,
                not config.EXECUTION_ENABLED)

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop_event.set)

    decision_queue = asyncio.Queue()
    if sys.stdin.isatty():
        threading.Thread(target=_stdin_loop,
                         args=(loop, decision_queue, stop_event),
                         name="auto-entry-confirm", daemon=True).start()
        logger.info("人工确认命令: approve <proposal_id> / reject <proposal_id> [原因]")
    else:
        logger.warning("stdin 非 TTY：开仓候选只提醒，不会自动批准")

    last_bar: dict = {}
    adopt_tick = 0
    try:
        while not stop_event.is_set():
            market = collector.latest()
            while True:
                try:
                    command = decision_queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                try:
                    await _handle_decision(command, engine, market)
                except Exception:
                    logger.exception("人工确认命令处理异常: %s", command)
            now_hm = datetime.now(ET).hour * 60 + datetime.now(ET).minute
            trading = _is_trading_hours()
            for sym, data in market.items():
                bar_c = data.get("bar_c") or {}
                ts_c = data.get("ts_c") or data.get("ts") or {}
                fvg_c = data.get("fvg_c") or data.get("fvg")
                bt = bar_c.get("time") or 0
                # bar 收盘推进 → 管理（PENDING/ACTIVE）
                if bt and last_bar.get(sym) != bt:
                    last_bar[sym] = bt
                    try:
                        await engine.on_bar(sym, bar_c, ts_c)
                    except Exception:
                        logger.exception("on_bar 异常 %s", sym)
                # tick：WATCHING 产生候选；ACTIVE 用当前 bar 极值达到 2R 立即保本
                if trading:
                    bar = data.get("bar") or {}
                    low, high = bar.get("low"), bar.get("high")
                    if low or high:
                        try:
                            await engine.on_price_extremes(
                                sym, low, high, ts_c, bar_c, now_hm,
                                fvg_c=fvg_c)
                        except Exception:
                            logger.exception("on_price 异常 %s", sym)
            # 收养人工仓（每 5s 扫一次）
            adopt_tick += 1
            if adopt_tick >= 5:
                adopt_tick = 0
                try:
                    adopted = await engine.adopt_manual_lots(
                        order_mgr.list_lots(active_only=True))
                    for sym in adopted:
                        data = market.get(sym) or {}
                        bar_c = data.get("bar_c") or {}
                        if bar_c:
                            ts_c = data.get("ts_c") or data.get("ts") or {}
                            await engine.on_bar(sym, bar_c, ts_c)
                except Exception:
                    logger.exception("收养扫描异常")
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=1.0)
            except asyncio.TimeoutError:
                pass
    finally:
        await collector.stop()
        await gateway.disconnect()
        logger.info("auto_runner 已退出（存量 lot 由 TWS 保护单托管）")


def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    logging.getLogger("ib_async").setLevel(logging.WARNING)
    ap = argparse.ArgumentParser(description="全自动策略运行器")
    ap.add_argument("--strategy", default="trend_pullback")
    ap.add_argument("--symbols", required=True, help="逗号分隔，如 NASDAQ:NVDA,NASDAQ:QQQ")
    ap.add_argument("--param", action="append", default=[],
                    help="覆盖策略参数，如 breakeven_r=1.5（可多次）")
    args = ap.parse_args()
    params = {}
    for kv in args.param:
        k, _, v = kv.partition("=")
        try:
            params[k] = float(v)
        except ValueError:
            params[k] = v
    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    asyncio.run(run(args.strategy, symbols, params))


if __name__ == "__main__":
    main()
