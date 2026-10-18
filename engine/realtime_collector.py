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
        -> self._snapshot: {symbol: {bar, bar_c, ts, ts_c}}

5M K线 + trend_state 单指标（ts=实时值，ts_c=上一根已收盘确认值）。
"""

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SERVICE_DIR = PROJECT_ROOT / "service"
TV_DAEMON_JS = SERVICE_DIR / "tv_cloud_daemon.js"


class RealtimeCollector:
    """常驻 TV 订阅采集器。"""

    def __init__(self, symbols: List[str]):
        self.symbols = [s.strip() for s in symbols if s.strip()]
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._reader_task: Optional[asyncio.Task] = None
        self._watchdog_task: Optional[asyncio.Task] = None
        self._snapshot: Dict[str, dict] = {}
        self._last_update: float = 0.0
        self.latest_bar_time: float = 0.0   # 最新 K 线时间戳（数据时钟）
        self.bar_update_delay: Optional[float] = None  # K线收盘→收到 的延时（秒）
        self._running = False

    async def start(self) -> None:
        """启动 daemon 子进程、读取循环与守护（自动重启）任务。"""
        if not TV_DAEMON_JS.exists():
            raise RuntimeError(f"daemon 不存在: {TV_DAEMON_JS}")
        await self._launch()
        self._running = True
        self._reader_task = asyncio.create_task(self._reader_loop())
        self._watchdog_task = asyncio.create_task(self._watchdog_loop())

    async def _launch(self) -> None:
        """启动 daemon 子进程。"""
        self._proc = await asyncio.create_subprocess_exec(
            "node", str(TV_DAEMON_JS), "--symbols", ",".join(self.symbols),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(SERVICE_DIR),
        )
        logger.info("TV 订阅采集器启动: %d 个标的", len(self.symbols))

    async def _reader_loop(self) -> None:
        """逐行读取 daemon 输出，更新快照；daemon 退出则本循环自然结束。"""
        assert self._proc and self._proc.stdout
        while True:
            try:
                line = await self._proc.stdout.readline()
            except (asyncio.CancelledError, Exception):
                break
            if not line:
                # daemon 退出（stdout EOF），交给 watchdog 重启
                break
            try:
                msg = json.loads(line.decode("utf-8"))
            except json.JSONDecodeError:
                continue
            if msg.get("type") == "tick":
                self._snapshot = msg.get("symbols", {})
                self._last_update = time.time()
                # 记录最新 K 线时间戳（数据时钟）：取所有标的最大的 bar.time
                max_t = 0.0
                for s in self._snapshot.values():
                    t = (s.get("bar") or {}).get("time") or 0
                    if t > max_t:
                        max_t = t
                if max_t > 0:
                    if self.latest_bar_time == 0:
                        # 首次初始化：只记基准，不计延时（避免假延时）
                        self.latest_bar_time = max_t
                    elif max_t > self.latest_bar_time:
                        # bar.time 跳变 = 旧 K 线收盘、新 K 线开始；
                        # 记录"K线收盘→收到"的延时（秒）
                        self.bar_update_delay = round(time.time() - max_t, 1)
                        self.latest_bar_time = max_t

    async def _watchdog_loop(self) -> None:
        """守护：daemon 退出后 5s 退避自动重启。"""
        while True:
            await asyncio.sleep(5.0)
            if self._proc is not None and self._proc.returncode is None:
                continue  # daemon 还活着
            if not self._running:
                continue  # 已停止
            self._running = False
            logger.warning("TV daemon 已退出，尝试重启 ...")
            try:
                await self._launch()
                self._running = True
                self._reader_task = asyncio.create_task(self._reader_loop())
                logger.info("TV daemon 重启成功")
            except Exception as e:
                logger.warning("daemon 重启失败（5s 后重试）: %s", e)

    def latest(self) -> Dict[str, dict]:
        """返回最新快照 {symbol: {bar, bar_c, ts, ts_c}}。"""
        return self._snapshot

    def age(self) -> float:
        """距最后一次 tick 的秒数（用于判断数据是否新鲜）。"""
        if self._last_update == 0:
            return float("inf")
        return time.time() - self._last_update

    @property
    def running(self) -> bool:
        return self._running

    async def stop(self) -> None:
        """停止采集器。"""
        self._running = False
        if self._watchdog_task:
            self._watchdog_task.cancel()
            self._watchdog_task = None
        if self._reader_task:
            self._reader_task.cancel()
            self._reader_task = None
        if self._proc and self._proc.returncode is None:
            self._proc.terminate()
            try:
                await asyncio.wait_for(self._proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                self._proc.kill()
        self._proc = None
