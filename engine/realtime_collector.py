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

    # 53 symbols with 161 historical rows and Pine breakout context exceed 4MB.
    STREAM_LIMIT_BYTES = 16 * 1024 * 1024
    CONTENT_STALE_SECONDS = 45.0
    TRANSPORT_STALE_SECONDS = 15.0
    BAR_STALE_SECONDS = 600.0
    STARTUP_GRACE_SECONDS = 90.0
    STARTUP_MAX_SECONDS = 180.0
    RESTART_COOLDOWN_SECONDS = 60.0

    def __init__(self, symbols: List[str], *, audit=None, market_session="extended", default_session_symbols=(), ipo_watches=None, on_ipo_trade=None):
        self.audit = audit
        self.market_session = market_session
        self.default_session_symbols = frozenset(default_session_symbols)
        self.market_sessions: dict[str, dict] = {}
        self.vwap_distribution: dict = {"frame": None, "progress": None}
        self.vwap_opening: dict = {}
        self.ipo_watches = dict(ipo_watches or {})
        self.ipo_trades: dict = {}
        self.ipo_status: dict = {}
        self.on_ipo_trade = on_ipo_trade
        self.market_frame_id = None
        self.symbols = [s.strip() for s in symbols if s.strip()]
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._reader_task: Optional[asyncio.Task] = None
        self._drain_task: Optional[asyncio.Task] = None
        self._watchdog_task: Optional[asyncio.Task] = None
        self._snapshot: Dict[str, dict] = {}
        self._content_key = ""
        self._last_update = 0.0          # 最后一次收到有效 tick（传输心跳）
        self._last_content_update = 0.0  # 最后一次 K 线实际变化
        self._last_launch = 0.0
        self._last_startup_progress = 0.0
        self.startup_indicators: dict = {}
        self.startup_subscribed = 0
        self.cache_issues: dict[str, dict] = {}
        self._started_at = None
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
        self._started_at = time.monotonic()
        try:
            await self._launch()
        except Exception:
            self._running = False
            raise
        self._watchdog_task = asyncio.create_task(self._watchdog_loop())

    @property
    def starting_up(self) -> bool:
        return (
            self._running
            and self._started_at is not None
            and time.monotonic() - self._started_at < self.STARTUP_GRACE_SECONDS
        )

    async def _launch(self) -> None:
        """启动 daemon 及其专属 stdout/stderr 消费任务。"""
        self._last_launch = time.time()
        self._last_startup_progress = 0.0
        self.startup_indicators = {}
        self.startup_subscribed = 0
        self.market_sessions = {}
        self.vwap_opening = {}
        self.ipo_status = {}
        if self.market_session == "24h":
            self._snapshot = {}
            self._content_key = ""
            self.latest_bar_time = self._last_content_update = self._last_update = 0.0
            self.bar_update_delay = None
        self._proc = await asyncio.create_subprocess_exec(
            "node", str(TV_DAEMON_JS), "--symbols", ",".join(self.symbols),
            "--market-session", self.market_session,
            "--default-session-symbols", ",".join(sorted(self.default_session_symbols)),
            *(["--ipo-watches", json.dumps(self.ipo_watches)] if self.ipo_watches else []),
            *(["--rhythm-cache", str(self.audit.path.parent / "market_rhythm.json")]
              if self.audit is not None else []),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(SERVICE_DIR),
            # Whole-watchlist ticks now include historical Pine context (>4MB).
            # Keep a bounded buffer with headroom for the complete snapshot.
            limit=self.STREAM_LIMIT_BYTES,
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
                if any(marker in msg for marker in ("local_indicator_error", "study_error", "Can't parse pine")):
                    self._study_errors.append(time.time())
                logger.warning("[tv_cloud] %s", msg)

    def _accept_tick(self, msg: dict, now: Optional[float] = None) -> None:
        """更新传输/内容时钟；重复旧快照只证明传输存活，不证明数据新鲜。"""
        symbols = msg.get("symbols")
        if not isinstance(symbols, dict):
            return
        sessions = msg.get("sessions")
        if isinstance(sessions, dict):
            self.market_sessions = sessions
        vwap_opening = msg.get("vwap_opening")
        if isinstance(vwap_opening, dict):
            self.vwap_opening = vwap_opening
        vwap = msg.get("vwap_distribution")
        if isinstance(vwap, dict):
            frame = vwap.get("frame")
            previous = self.vwap_distribution["frame"]
            revision = frame.get("revision", 0) if isinstance(frame, dict) else None
            rows = frame.get("rows") if isinstance(frame, dict) else None
            row_symbols = {
                row["symbol"] for row in rows
                if isinstance(row, dict) and isinstance(row.get("symbol"), str)
                and row["symbol"] and row.get("time") == frame.get("time")
            } if isinstance(rows, list) else set()
            progress = vwap.get("progress")
            total = progress.get("total") if isinstance(progress, dict) else None
            if (not isinstance(frame, dict) or type(frame.get("time")) is not int
                    or frame["time"] <= 0
                    or type(revision) is not int or not 0 <= revision <= 9_007_199_254_740_991
                    or not isinstance(rows, list) or not rows or len(row_symbols) != len(rows)
                    or (isinstance(total, int) and total > 0 and len(rows) != total)
                    or (previous and (frame["time"], revision) <=
                        (previous["time"], previous.get("revision", 0)))):
                frame = previous
            self.vwap_distribution = {"frame": frame, "progress": vwap.get("progress")}
        if self.market_session == "24h" or self.default_session_symbols:
            symbols = {
                symbol: row for symbol, row in symbols.items()
                if isinstance(row, dict) and row.get("bar")
                and (
                    (self.market_session == "extended" and symbol not in self.default_session_symbols)
                    or self._confirmed_session(symbol, row.get("session") or {})
                )
            }
        now = time.time() if now is None else now
        self._last_update = now
        content_key = json.dumps(symbols, ensure_ascii=False, sort_keys=True,
                                 separators=(",", ":"))
        if content_key != self._content_key:
            symbols = {symbol: dict(data) for symbol, data in symbols.items()}
            for symbol, data in symbols.items():
                previous = self._snapshot.get(symbol) or {}
                bar = data.get("bar") or {}
                old_bar = previous.get("bar") or {}
                # Snapshot bars also carry Pine context. Its arrival must not
                # make a stale market price appear newly observed.
                changed = bool(bar) and any(
                    bar.get(field) != old_bar.get(field)
                    for field in ("time", "open", "high", "low", "close", "volume")
                )
                observation = data.get("price_observed_at")
                if type(observation) not in (int, float) or not 0 < observation <= now:
                    observation = None
                if changed:
                    self._last_content_update = max(self._last_content_update, observation or now)
                data["price_observed_at"] = (
                    observation if observation is not None
                    else now if changed else previous.get("price_observed_at")
                )
            self._snapshot = symbols
            self._content_key = content_key

        if self.audit is not None:
            source_at = msg.get("ts")
            self.market_frame_id = self.audit.capture(
                self._snapshot, now, source_at if isinstance(source_at, (int, float)) else None,
            )

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
            elif msg.get("type") == "startup_progress":
                self._last_startup_progress = time.time()
                self.startup_subscribed = msg["subscribed"]
            elif msg.get("type") in ("ipo_trade", "ipo_status"):
                self._accept_ipo(msg)
            elif msg.get("type") == "indicator_startup":
                self.startup_indicators = {
                    key: msg[key] for key in ("loaded", "total", "name", "source", "tv_study_subscriptions")
                    if key in msg
                }
                if msg.get("source") == "pinets" and msg.get("loaded") == msg.get("total"):
                    logger.info("PineTS 本地指标就绪 %s/%s；TV 指标订阅数 %s",
                                msg["loaded"], msg["total"], msg.get("tv_study_subscriptions"))
                if issue := msg.get("cache_issue"):
                    self.cache_issues[issue["id"]] = issue
                if loaded_id := msg.get("loaded_id"):
                    self.cache_issues.pop(loaded_id, None)

    def _accept_ipo(self, msg):
        symbol = msg.get("symbol")
        if not isinstance(symbol,str) or self.ipo_watches.get(symbol) != msg.get("listing_date") or symbol not in self.ipo_watches:
            return
        if msg.get("type") == "ipo_trade":
            self.ipo_trades[symbol] = dict(msg)
            if self.on_ipo_trade:
                try:
                    self.on_ipo_trade(self.ipo_trades,time.time())
                except Exception:
                    logger.exception("IPO提醒处理失败，保留报价供后续重试")
        elif msg.get("type") == "ipo_status":
            self.ipo_status[symbol] = dict(msg)
        # IPO messages never advance normal market freshness or readiness clocks.

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

            # A dead reader cannot recover by waiting for market hours or ticks.
            # Restart only this collector's process, including during premarket.
            if self._reader_task is not None and self._reader_task.done():
                try:
                    await self._restart("行情读取任务已停止")
                    logger.warning("行情读取任务已停止，daemon 重启成功")
                except Exception as e:
                    logger.warning("行情读取任务已停止，daemon 重启失败: %s", e)
                continue

            now = time.time()
            launch_age = now - self._last_launch
            if launch_age < self.STARTUP_GRACE_SECONDS:
                continue
            # Allow an advancing subscription batch to finish. Progress never
            # counts as a fresh tick and cannot extend startup indefinitely.
            if (launch_age < self.STARTUP_MAX_SECONDS
                    and now - self._last_startup_progress < self.TRANSPORT_STALE_SECONDS):
                continue
            if now - self._last_restart < self.RESTART_COOLDOWN_SECONDS:
                continue
            health = self.data_health(now)
            if not health["stale"] or health["reason"] == "session_unavailable":
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

    def _confirmed_session(self, symbol: str, session: dict) -> bool:
        if symbol in self.default_session_symbols:
            return (session.get("requested") == "default" and session.get("accepted") is True
                    and not session.get("error"))
        return session.get("resolved") == self.market_session and not session.get("error")

    def _session_unavailable(self, symbol: str) -> bool:
        session = self.market_sessions.get(symbol, {})
        if symbol in self.default_session_symbols:
            return bool(session.get("error"))
        return self.market_session == "24h" and bool(
            session.get("error") or session.get("resolved") not in (None, "24h")
        )

    def market_session_status(self) -> dict:
        confirmed = sum(
            bool(row.get("bar") and self._confirmed_session(symbol, row.get("session") or {}))
            for symbol, row in self._snapshot.items()
        )
        return {
            "requested": self.market_session,
            "confirmed": confirmed,
            "total": len(self.symbols),
            "unavailable": {
                symbol: session for symbol, session in self.market_sessions.items()
                if self._session_unavailable(symbol)
            },
            "symbols": self.market_sessions,
        }

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
            unavailable = self.market_session == "24h" and self.symbols and all(
                self._session_unavailable(symbol) for symbol in self.symbols
            )
            if unavailable:
                reasons.append("session_unavailable")
            else:
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
            **({"confirmed_24h_symbols": tuple(
                symbol for symbol in self._snapshot if symbol not in self.default_session_symbols
            )}
               if self.market_session == "24h" else {}),
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
