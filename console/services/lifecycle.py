"""lifecycle application functions and owned state. No console.app dependency."""

import asyncio
import sys
from pathlib import Path

from fastapi import HTTPException
from execution.writer_lock import acquire_writer_lock
import config
import time
from console.shared import logger


class LifecycleService:
    def __init__(self, context):
        self.context = context
        self._cache_repair_running = False

    def _startup_status(self) -> dict:
        collector = self.context.collector
        snap = collector.latest() or {}
        session = collector.market_session_status()
        unavailable = session["unavailable"]
        confirmed_symbols = {
            symbol for symbol in config.WATCHLIST
            if (snap.get(symbol, {}).get("ts_c") or {}).get("trend_state") is not None
        }
        bars_ready = sum(bool((snap.get(symbol) or {}).get("bar")) for symbol in config.WATCHLIST)
        pending = [symbol for symbol in config.WATCHLIST
                   if symbol not in confirmed_symbols and symbol not in unavailable]
        confirmed = len(confirmed_symbols)
        expected = len(config.WATCHLIST)
        elapsed = time.time() - self.context.lifecycle._startup_state["started_at"]
        market_ready = collector.running and not pending and confirmed == expected
        allow_session_review = collector.running and session["requested"] == "24h" and (
            not pending or (collector.startup_subscribed >= expected and elapsed > 120)
        )
        ibkr_required = bool(self.context.gateway.enabled)
        ibkr_ready = bool(self.context.gateway._connected) or not ibkr_required
        core_ready = bool(self.context.lifecycle._startup_state["core_ready"])
        browser_ready = bool(self.context.lifecycle._startup_state["browser_ready"])
        stages = [
            {
                "key": "core",
                "label": "交易服务",
                "ready": core_ready,
                "detail": "风控、订单与后台任务已启动" if core_ready else "正在启动",
            },
            {
                "key": "ibkr",
                "label": "IBKR",
                "ready": ibkr_ready,
                "blocking": False,
                "detail": "TWS 已连接"
                if self.context.gateway._connected
                else "DRY-RUN 模式"
                if not ibkr_required
                else "TWS 未连接，后台重试中",
            },
            {
                "key": "market",
                "label": "行情与指标",
                "ready": market_ready,
                "blocking": market_ready or not allow_session_review,
                "detail": f"确认指标 {confirmed}/{expected}",
            },
            {
                "key": "browser",
                "label": "图表工作区",
                "ready": browser_ready,
                "detail": (
                    "左侧 autotrade · 右侧 TradingView 分屏已就绪"
                    if self.context.lifecycle._startup_state.get("split_ready")
                    else "浏览器已打开；自动分屏未完成："
                    + self.context.lifecycle._startup_state["split_error"]
                    if self.context.lifecycle._startup_state.get("split_error")
                    else "控制台与 TradingView 标签页已就绪"
                )
                if browser_ready
                else self.context.lifecycle._startup_state["browser_error"]
                or "正在准备浏览器标签页",
            },
        ]
        errors = []
        if self.context.lifecycle._startup_state["browser_error"] and (
            not browser_ready
        ):
            errors.append(self.context.lifecycle._startup_state["browser_error"])
        if self.context.collector.study_health().get("systemic") and (not market_ready):
            errors.append(
                "TradingView 指标通道异常，请运行 python3 service/tv_diag.py 检查"
            )
        if unavailable:
            errors.append("行情时段未确认：" + "、".join(
                f'{symbol}（{info.get("resolved") or "订阅错误"}）'
                for symbol, info in unavailable.items()
            ))
        if pending and elapsed > 240:
            errors.append("行情订阅未完成，等待：" + "、".join(pending))
        return {
            "market_pending": pending,
            "market_session": session,
            "ready": bool(self.context.lifecycle._startup_state["ready"]),
            "stages": stages,
            "error": "；".join(errors),
            "elapsed": round(elapsed),
            "all_ready": all(s["ready"] for s in stages),
            "workspace_ready": all(s["ready"] for s in stages if s.get("blocking", True)),
            "market_progress": {
                "indicators": self.context.collector.startup_indicators,
                "subscribed": self.context.collector.startup_subscribed,
                "bars_ready": bars_ready,
                "confirmed": confirmed,
                "total": expected,
            },
            "cache_issues": list(self.context.collector.cache_issues.values()),
            "cache_repair_running": self._cache_repair_running,
        }

    async def on_startup(self) -> None:
        self.context.system._system_stopping = False
        config.validate_config()
        if not acquire_writer_lock(
            str(self.context.paths.data_dir / "autotrade_writer.lock"),
            owner=self.context,
        ):
            raise RuntimeError("另一个 autotrade 执行写进程正在运行，禁止重复管理订单")
        self.context.execution_writer_acquired = True
        self.context.order_mgr._load(allow_recovery=True)  # reload after owning the writer lock, before broker access
        self.context.order_mgr._save()  # validate/migrate local ledger before any broker side effects
        self.context.market_audit.start()
        self.context.gateway.on_order_audit = self.context.events.record_event
        connected = await self.context.gateway.connect()
        if config.EXECUTION_ENABLED and (not connected):
            logger.error(
                "实盘 TWS 未连接，后台持续重连，控制台继续启动"
            )
            self.context.gateway._start_reconnect()
        elif not connected:
            logger.info("IBKR 未连接，运行于纯 dry-run（虚拟成交）")
        self.context.order_mgr.on_lot_closed = self.context.events._on_lot_closed
        self.context.order_mgr.on_lot_revived = self.context.events._on_lot_revived
        self.context.order_mgr.on_fill_event = lambda event: self.context.events.record_event("execution_fill", **event)
        def entry_stage(**event):
            self.context.events.record_event("execution_entry_stage", **event)

        self.context.order_mgr.on_entry_stage = entry_stage
        self.context.gateway.on_entry_stage = entry_stage
        self.context.order_mgr.price_provider = self.context.accounts._tv_last_price
        if connected:
            await self.context.order_mgr.reconcile()
        from datetime import datetime
        from zoneinfo import ZoneInfo

        await self.context.auto_service.restore(datetime.now(ZoneInfo("America/New_York")).date())
        self.context.automatic._auto_enabled = self.context.auto_service.enabled
        self.context.automatic._auto_strategy_name = self.context.auto_service.strategy_name
        await self.context.collector.start()
        await self.context.copy_supervisor.start(self.context.copy._load_copy_servers())
        self.context.snapshot._broadcast_task = self.context.tasks.start(
            "snapshot._broadcast", lambda: self.context.snapshot._broadcast_loop()
        )
        self.context.monitoring._eod_task = self.context.tasks.start(
            "monitoring._eod_force_close",
            lambda: self.context.monitoring._eod_force_close_loop(),
        )
        self.context.monitoring._scan_task = self.context.tasks.start(
            "monitoring._position_scan",
            lambda: self.context.monitoring._position_scan_loop(),
        )
        self.context.monitoring._stop_guard_task = self.context.tasks.start(
            "monitoring._stop_guard", lambda: self.context.monitoring._stop_guard_loop()
        )
        self.context.monitoring._slow_break_task = self.context.tasks.start(
            "monitoring._slow_break", lambda: self.context.monitoring._slow_break_loop()
        )
        self.context.monitoring._auto_breakeven_task = self.context.tasks.start(
            "monitoring._auto_breakeven",
            lambda: self.context.monitoring._auto_breakeven_loop(),
        )
        self.context.monitoring._reconcile_task = self.context.tasks.start(
            "monitoring._reconcile", lambda: self.context.monitoring._reconcile_loop()
        )
        self.context.signals._shadow_task = self.context.tasks.start(
            "signals._shadow", lambda: self.context.signals._shadow_loop()
        )
        self.context.automatic._auto_task = self.context.tasks.start(
            "automatic._auto", lambda: self.context.automatic._auto_loop()
        )
        self.context.trading._midpoint_reprice_task = self.context.tasks.start(
            "trading._midpoint_limit_reprice",
            lambda: self.context.trading._midpoint_limit_reprice_loop(),
        )
        self.context.trading._retest_reprice_task = self.context.tasks.start(
            "trading._retest_limit_reprice",
            lambda: self.context.trading._retest_limit_reprice_loop(),
        )
        self.context.monitoring._external_cleanup_task = self.context.tasks.start(
            "monitoring._external_cleanup",
            lambda: self.context.monitoring._external_cleanup_loop(),
        )
        self.context.signals._fvg_task = self.context.tasks.start(
            "signals._fvg_monitor", lambda: self.context.signals._fvg_monitor_loop()
        )
        self.context.monitoring._fvg_reverse_task = self.context.tasks.start(
            "monitoring._fvg_reverse_guard",
            lambda: self.context.monitoring._fvg_reverse_guard_loop(),
        )
        self.context.signals._breakout_task = self.context.tasks.start(
            "signals._breakout", lambda: self.context.signals._breakout_loop()
        )
        self.context.monitoring._naked_position_task = self.context.tasks.start(
            "monitoring._naked_position",
            lambda: self.context.monitoring._naked_position_loop(),
        )
        self.context.monitoring._health_watch_task = self.context.tasks.start(
            "monitoring._health_watch",
            lambda: self.context.monitoring._health_watch_loop(),
        )
        self.context.desktop._tv_check_task = self.context.tasks.start(
            "desktop._tv_startup_check",
            lambda: self.context.desktop._tv_startup_check(),
        )
        self.context.precheck._precheck_task = self.context.tasks.start(
            "precheck._precheck", lambda: self.context.precheck._precheck_loop()
        )
        self.context.lifecycle._startup_state["core_ready"] = True
        self.context.lifecycle._startup_task = self.context.tasks.start(
            "desktop._startup_prepare", lambda: self.context.desktop._startup_prepare()
        )

    async def on_shutdown(self) -> None:
        for t in (
            self.context.snapshot._broadcast_task,
            self.context.monitoring._eod_task,
            self.context.monitoring._scan_task,
            self.context.monitoring._stop_guard_task,
            self.context.monitoring._reconcile_task,
            self.context.signals._shadow_task,
            self.context.automatic._auto_task,
            self.context.trading._midpoint_reprice_task,
            self.context.trading._retest_reprice_task,
            self.context.monitoring._external_cleanup_task,
            self.context.monitoring._slow_break_task,
            self.context.monitoring._auto_breakeven_task,
            self.context.signals._fvg_task,
            self.context.monitoring._fvg_reverse_task,
            self.context.signals._breakout_task,
            self.context.monitoring._naked_position_task,
            self.context.monitoring._health_watch_task,
            self.context.reports._report_loop_task,
            self.context.precheck._precheck_task,
            self.context.desktop._tv_check_task,
            self.context.lifecycle._startup_task,
        ):
            if t:
                t.cancel()
        for stop in (
            self.context.night_test.disconnect,
            self.context.copy_supervisor.stop_all,
            self.context.collector.stop,
            self.context.gateway.disconnect,
            self.context.market_audit.stop,
        ):
            try:
                await stop()
            except Exception:
                logger.exception("退出清理失败: %s", stop.__name__)

    async def api_startup(self) -> dict:
        return self.context.lifecycle._startup_status()

    async def repair_pine_cache(self) -> dict:
        if self._cache_repair_running:
            raise HTTPException(409, "缓存修复正在进行")
        ids = list(self.context.collector.cache_issues)
        if not ids:
            raise HTTPException(409, "当前没有待修复的指标正文缓存")
        self._cache_repair_running = True
        script = Path(__file__).resolve().parents[2] / "service" / "refresh_pine_texts.py"
        args = [arg for indicator_id in ids for arg in ("--indicator", indicator_id)]
        proc = None
        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable, str(script), *args,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await proc.communicate()
            if proc.returncode:
                detail = stderr.decode(errors="replace").strip().splitlines()
                logger.warning("指标正文缓存修复失败: %s", detail[-1] if detail else proc.returncode)
                if detail and "官方帧正文不完整或布局版本落后" in detail[-1]:
                    raise HTTPException(502, "请在专用 TradingView 图表加载上方所列指标的最新版本，再点修复。")
                raise HTTPException(502, "缓存修复验证失败，请查看控制台日志后重试。")
            return {"ok": True}
        finally:
            if proc is not None and proc.returncode is None:
                proc.terminate()
                await proc.wait()
            self._cache_repair_running = False
