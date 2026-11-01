"""lifecycle application functions and owned state. No console.app dependency."""

from execution.writer_lock import acquire_writer_lock
import config
import time
from console.shared import logger


class LifecycleService:
    def __init__(self, context):
        self.context = context

    def _startup_status(self) -> dict:
        snap = self.context.collector.latest() or {}
        total = len(snap)
        confirmed = sum(
            (
                1
                for v in snap.values()
                if isinstance(v.get("ts_c"), dict)
                and v["ts_c"].get("trend_state") is not None
            )
        )
        expected = len(config.WATCHLIST)
        market_ready = (
            self.context.collector.running
            and total >= expected
            and (confirmed >= total)
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
                "detail": "TWS 已连接"
                if self.context.gateway._connected
                else "DRY-RUN 模式"
                if not ibkr_required
                else "等待 TWS 连接（7496）",
            },
            {
                "key": "market",
                "label": "行情与指标",
                "ready": market_ready,
                "detail": f"确认指标 {confirmed}/{expected}",
            },
            {
                "key": "browser",
                "label": "图表工作区",
                "ready": browser_ready,
                "detail": "控制台与 TradingView 双标签页已就绪"
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
        return {
            "ready": bool(self.context.lifecycle._startup_state["ready"]),
            "stages": stages,
            "error": "；".join(errors),
            "elapsed": round(
                time.time() - self.context.lifecycle._startup_state["started_at"]
            ),
            "all_ready": all((s["ready"] for s in stages)),
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
        connected = await self.context.gateway.connect()
        if config.EXECUTION_ENABLED and (not connected):
            logger.error(
                "EXECUTION_ENABLED=1 但 IBKR 未连接，将持续重连，就绪后进入主页面"
            )
            self.context.gateway._start_reconnect()
        elif not connected:
            logger.info("IBKR 未连接，运行于纯 dry-run（虚拟成交）")
        self.context.order_mgr.on_lot_closed = self.context.events._on_lot_closed
        self.context.order_mgr.on_lot_revived = self.context.events._on_lot_revived
        self.context.order_mgr.price_provider = self.context.accounts._tv_last_price
        if connected:
            await self.context.order_mgr.reconcile()
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
        self.context.reports._report_loop_task = self.context.tasks.start(
            "reports._report", lambda: self.context.reports._report_loop()
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
            self.context.copy_supervisor.stop_all,
            self.context.collector.stop,
            self.context.gateway.disconnect,
        ):
            try:
                await stop()
            except Exception:
                logger.exception("退出清理失败: %s", stop.__name__)

    async def api_startup(self) -> dict:
        return self.context.lifecycle._startup_status()
