# -*- coding: utf-8 -*-
"""
TradingView 实时订阅采集器 (RealtimeCollector)
================================================

通过 subprocess 常驻运行 service/tv_cloud_daemon.js（订阅模式），
逐行读取其 stdout 的实时 tick JSON，维护最新数据快照，供上层
（控制台 WebSocket）实时推送与下单计算。

数据流：
    node tv_cloud_daemon.js --symbols ...（常驻，每 1s 输出 tick）
      -> Python asyncio subprocess stdout 逐行解析
        -> self._snapshot: {symbol: {bar, bar_c, bar_c2, ts, ts_c, fvg, fvg_c, e9, e9_c}}

5M K线 + trend_state / FVG / ema9_autotrade（e9=实时触线值，e9_c=确认管理值；
bar_c=上一根已收盘，bar_c2=上上根已收盘，回踩单定价基准）。
"""

import asyncio
import datetime as dt
import json
import logging
import time
from collections import deque
from pathlib import Path
from typing import Dict, List, Optional
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SERVICE_DIR = PROJECT_ROOT / "service"
TV_DAEMON_JS = SERVICE_DIR / "tv_cloud_daemon.js"


class RealtimeCollector:
    """常驻 TV 订阅采集器。"""

    CONTENT_STALE_SECONDS = 45.0
    TRANSPORT_STALE_SECONDS = 15.0
    BAR_STALE_SECONDS = 600.0
    STARTUP_GRACE_SECONDS = 90.0
    RESTART_COOLDOWN_SECONDS = 60.0

    def __init__(self, symbols: List[str]):
        self.symbols = [s.strip() for s in symbols if s.strip()]
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._reader_task: Optional[asyncio.Task] = None
        self._drain_task: Optional[asyncio.Task] = None
        self._watchdog_task: Optional[asyncio.Task] = None
        self._snapshot: Dict[str, dict] = {}
        self._content_key = ""
        self._last_update = 0.0          # 最后一次收到有效 tick（传输心跳）
        self._last_content_update = 0.0  # 最后一次 symbols 实际变化
        self._last_launch = 0.0
        self._last_restart = 0.0
        self.latest_bar_time: float = 0.0   # 最新 K 线时间戳（数据时钟）
        self.bar_update_delay: Optional[float] = None  # K线收盘→收到 的延时（秒）
        self._running = False
        # study 健康监测（2026-10-27 协议故障教训：K线正常但指标全灭时
        # data_stale 检测不到）：stderr 里的 study_error 滚动计数 +
        # 快照 ts_c 就绪率持续跟踪
        self._study_errors: deque = deque()      # 最近 60s study_error 时间戳
        self._ts_dead_since: Optional[float] = None  # ts_c 就绪率跌破一半的起始时刻

    async def start(self) -> None:
        """启动 daemon 子进程、读取循环与守护（自动重启）任务。"""
        if not TV_DAEMON_JS.exists():
            raise RuntimeError(f"daemon 不存在: {TV_DAEMON_JS}")
        self._running = True
        try:
            await self._launch()
        except Exception:
            self._running = False
            raise
        self._watchdog_task = asyncio.create_task(self._watchdog_loop())

    async def _launch(self) -> None:
        """启动 daemon 及其专属 stdout/stderr 消费任务。"""
        self._last_launch = time.time()
        self._proc = await asyncio.create_subprocess_exec(
            "node", str(TV_DAEMON_JS), "--symbols", ",".join(self.symbols),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(SERVICE_DIR),
            # tick JSON 单行 = 全部标的快照，54 标的实测 71KB > asyncio 默认
            # 64KB 流上限（readline 抛 ValueError 静默断流）；放宽到 4MB
            limit=4 * 1024 * 1024,
        )
        logger.info("TV 订阅采集器启动: %d 个标的", len(self.symbols))
        self._drain_task = asyncio.create_task(self._stderr_drain(self._proc))
        self._reader_task = asyncio.create_task(self._reader_loop(self._proc))

    async def _stderr_drain(self, proc: asyncio.subprocess.Process) -> None:
        """逐行排空 daemon stderr 写日志（[tv_cloud] 前缀，warning 级别）。
        stderr 接了 PIPE 却从不读取，daemon 的 console.error 写满 64KB
        管道缓冲后会永久阻塞（实测死锁）；进程退出 EOF 后本任务自然结束。"""
        assert proc.stderr
        while True:
            try:
                line = await proc.stderr.readline()
            except (asyncio.CancelledError, Exception):
                break
            if not line:
                break
            msg = line.decode("utf-8", "replace").rstrip()
            if msg:
                if "study_error" in msg or "Can't parse pine" in msg:
                    self._study_errors.append(time.time())
                logger.warning("[tv_cloud] %s", msg)

    def _accept_tick(self, msg: dict, now: Optional[float] = None) -> None:
        """更新传输/内容时钟；重复旧快照只证明传输存活，不证明数据新鲜。"""
        symbols = msg.get("symbols")
        if not isinstance(symbols, dict):
            return
        now = time.time() if now is None else now
        self._last_update = now
        content_key = json.dumps(symbols, ensure_ascii=False, sort_keys=True,
                                 separators=(",", ":"))
        if content_key != self._content_key:
            symbols = {symbol: dict(data) for symbol, data in symbols.items()}
            for symbol, data in symbols.items():
                previous = self._snapshot.get(symbol) or {}
                data["price_observed_at"] = (
                    now if data.get("bar") != previous.get("bar")
                    else previous.get("price_observed_at", now))
            self._snapshot = symbols
            self._content_key = content_key
            self._last_content_update = now

        max_t = 0.0
        for s in self._snapshot.values():
            t = (s.get("bar") or {}).get("time") or 0
            if t > max_t:
                max_t = t
        if max_t > 0:
            if self.latest_bar_time == 0:
                self.latest_bar_time = max_t
            elif max_t > self.latest_bar_time:
                self.bar_update_delay = round(now - max_t, 1)
                self.latest_bar_time = max_t

    async def _reader_loop(self, proc: asyncio.subprocess.Process) -> None:
        """只读取指定 daemon；重启后旧 reader 不会误读新进程。"""
        assert proc.stdout
        while True:
            try:
                line = await proc.stdout.readline()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.exception("daemon stdout 读取异常，采集循环退出: %s", e)
                break
            if not line:
                break
            try:
                msg = json.loads(line.decode("utf-8"))
            except json.JSONDecodeError:
                continue
            if msg.get("type") == "tick":
                self._accept_tick(msg)

    async def _cancel_io(self) -> None:
        tasks = [t for t in (self._reader_task, self._drain_task) if t is not None]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._reader_task = None
        self._drain_task = None

    async def _restart(self, reason: str) -> None:
        self._last_restart = time.time()
        await self._cancel_io()
        proc = self._proc
        self._proc = None
        if proc is not None and proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
        await self._launch()

    async def _watchdog_loop(self) -> None:
        """进程退出、传输中断或内容停滞时重建 daemon。"""
        while True:
            await asyncio.sleep(5.0)
            if not self._running:
                continue
            if self._proc is None or self._proc.returncode is not None:
                try:
                    await self._restart("daemon 已退出")
                    logger.warning("TV daemon 已退出，重启成功")
                except Exception as e:
                    logger.warning("daemon 重启失败（5s 后重试）: %s", e)
                continue

            now = time.time()
            if now - self._last_launch < self.STARTUP_GRACE_SECONDS:
                continue
            if now - self._last_restart < self.RESTART_COOLDOWN_SECONDS:
                continue
            health = self.data_health(now)
            if not health["stale"]:
                continue
            reason = health["reason"]
            try:
                await self._restart(reason)
                logger.warning("TV 数据异常（%s），daemon 重启成功", reason)
            except Exception as e:
                logger.warning("TV 数据异常（%s），daemon 重启失败: %s", reason, e)

    def latest(self) -> Dict[str, dict]:
        """返回最新快照 {symbol: {bar, bar_c, bar_c2, ts, ts_c, fvg, fvg_c}}。"""
        return self._snapshot

    def study_health(self) -> dict:
        """study（指标）通道健康度。

        systemic=True 的两种触发（2026-10-27 故障模式）：
        - 错误风暴：60s 内 study_error > 20 条（协议级故障是全体刷屏，
          单标的自愈重建每分钟最多几条，阈值拉开一个数量级）；
        - 持续死亡：快照已建立但 ts_c 就绪率 < 50% 且持续超过 120s
          （覆盖 daemon 启动慢：54 标的加载约 60-90s，宽限后再判死）。"""
        now = time.time()
        while self._study_errors and now - self._study_errors[0] > 60:
            self._study_errors.popleft()
        errs = len(self._study_errors)
        total = len(self._snapshot)
        ready = sum(1 for v in self._snapshot.values()
                    if isinstance(v.get("ts_c"), dict)
                    and v["ts_c"].get("trend_state") is not None)
        if total > 0 and ready < total * 0.5:
            if self._ts_dead_since is None:
                self._ts_dead_since = now
        else:
            self._ts_dead_since = None
        dead_for = (round(now - self._ts_dead_since, 1)
                    if self._ts_dead_since is not None else 0.0)
        systemic = errs > 20 or dead_for > 120
        return {"study_errors_60s": errs, "ts_ready": ready, "ts_total": total,
                "ts_dead_for": dead_for, "systemic": systemic}

    @staticmethod
    def _is_trading_hours() -> bool:
        now = dt.datetime.now(ZoneInfo("America/New_York"))
        minutes = now.hour * 60 + now.minute
        return now.weekday() < 5 and 9 * 60 + 30 <= minutes < 16 * 60

    @staticmethod
    def _elapsed(since: float, now: float) -> float:
        return float("inf") if since <= 0 else max(0.0, now - since)

    @staticmethod
    def _display_age(age: float) -> Optional[float]:
        return None if age == float("inf") else round(age, 1)

    def data_health(self, now: Optional[float] = None,
                    trading: Optional[bool] = None) -> dict:
        """行情健康度；交易时段外的自然静默不触发重启或开仓拦截。"""
        now = time.time() if now is None else now
        trading = self._is_trading_hours() if trading is None else trading
        content_age = self._elapsed(self._last_content_update, now)
        transport_age = self._elapsed(self._last_update, now)
        bar_age = self._elapsed(self.latest_bar_time, now)
        reasons = []
        if trading:
            if not self._running:
                reasons.append("daemon_stopped")
            if transport_age > self.TRANSPORT_STALE_SECONDS:
                reasons.append("transport_stale")
            if content_age > self.CONTENT_STALE_SECONDS:
                reasons.append("content_stale")
            if bar_age > self.BAR_STALE_SECONDS:
                reasons.append("bar_stale")
        reason = ",".join(reasons)
        return {
            "stale": bool(reasons),
            "reason": reason,
            "trade_allowed": not reasons,
            "trading_hours": trading,
            "content_age": self._display_age(content_age),
            "transport_age": self._display_age(transport_age),
            "bar_age": self._display_age(bar_age),
        }

    def age(self) -> float:
        """距最后一次内容实际变化的秒数。"""
        return self._elapsed(self._last_content_update, time.time())

    def transport_age(self) -> float:
        """距最后一次有效 tick 的秒数。"""
        return self._elapsed(self._last_update, time.time())

    @property
    def running(self) -> bool:
        return bool(self._running and self._proc is not None
                    and self._proc.returncode is None)

    async def stop(self) -> None:
        """停止采集器。"""
        self._running = False
        if self._watchdog_task:
            self._watchdog_task.cancel()
            await asyncio.gather(self._watchdog_task, return_exceptions=True)
            self._watchdog_task = None
        await self._cancel_io()
        if self._proc and self._proc.returncode is None:
            self._proc.terminate()
            try:
                await asyncio.wait_for(self._proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                self._proc.kill()
                await self._proc.wait()
        self._proc = None
