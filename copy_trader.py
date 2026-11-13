#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
跟单引擎 (copy_trader.py)
=========================

follower 是 master 控制台（默认 127.0.0.1:8000）的「纯执行影子」：
消费 master `/ws/copy` 每秒快照，把 master 的 lot 生命周期镜像到本服务器的
IBKR 账户。每个 IBKR 服务器一个独立进程：

    python3 copy_trader.py --name A            # 读 logs/copy_servers.json 中 name==A
    python3 copy_trader.py --name A --dry-run  # 强制 dry-run（虚拟成交）

定稿语义（不可更改）：

- master lot PENDING        → 无动作（master 没成交 = follower 永远无仓）
- master lot 首次转 ACTIVE（filled_qty>0）
                            → follower 市价入场（marketable 限价 = master
                              avg_fill ×(1±0.5%，多+/空−)）+ STP 子单同发
                              （触发价 = master lot 当前 stop_px），
                              绝不允许无止损裸仓；入场后先对齐一次 master
                              最新 stop_px（防秒级窗口）
- master lot stop_px 变动   → follower 同步改 STP 到同一价格（快照轮询发现）
- master lot CLOSED（任何原因）→ follower 市价平仓（无限重试，每 30s）
- master lot CANCELLED（未成交）→ 无动作
- master 部分成交后补仓      → 不追加（首次成交即按足额入场）
- 回撤熔断：净值相对启动（或每日首个）基准回撤 > max_drawdown_pct →
  halted=true，停止新开仓镜像（已有仓位继续改止损/平仓同步），
  不自动解除（cmd 文件 {"unhalt": true} 手动解除）

状态文件（全部落 logs/，供 master 页面读取）：
- logs/copy-{name}.json          映射与游标（原子写）
- logs/copy-{name}-status.json   状态（每 2s 原子写）
- logs/copy-{name}-cmd.json      人工指令（master 页面写入，follower 每 2s 读，
                                 消费后删除）：{"retry":[...],"dismiss":[...],
                                 "unhalt":true}
- logs/copy-{name}-events.jsonl  事件日志
"""

import argparse
import asyncio
import hashlib
import json
import logging
import math
import os
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Dict, List, Optional
from zoneinfo import ZoneInfo

import config
from execution.copy_channel import copy_channel_token, copy_channel_url
from execution.ibkr import IBKRGateway
from execution.order_lot import OrderManager
from execution.risk import RiskConfig, RiskGate
from execution.writer_lock import acquire_writer_lock, release_writer_lock

logger = logging.getLogger("copy_trader")
ET = ZoneInfo("America/New_York")

PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_SERVERS_PATH = PROJECT_ROOT / "logs" / "copy_servers.json"

# master lot 状态（execution/order_lot.py 的状态机）
M_PENDING = "PENDING"
M_ACTIVE = "ACTIVE"
M_CLOSING = "CLOSING"
M_CANCELLED = "CANCELLED"
M_CLOSED = "CLOSED"

# follower 映射状态
F_ACTIVE = "ACTIVE"      # 已入场，STP 保护中
F_CLOSING = "CLOSING"    # 平仓单已提交/重试中
F_CLOSED = "CLOSED"      # 已平仓（终态）

MAX_TRIES = 3                       # entry/modify 最大尝试次数
RETRY_BACKOFF = (2.0, 4.0, 8.0)     # entry/modify 指数退避（秒）
CLOSE_RETRY_SECONDS = 30.0          # close 无限重试间隔（风控动作）
CONFIG_RELOAD_SECONDS = 5.0         # 服务器配置热更新周期
STATUS_SECONDS = 2.0                # 状态文件写入周期
CMD_POLL_SECONDS = 2.0              # 人工指令读取周期
EQUITY_CACHE_SECONDS = 10.0         # 净值查询缓存（避免高频打 IBKR）
WS_BACKOFF_MAX = 30.0               # master WS 断线重连退避上限


def _atomic_write_json(path: Path, payload: dict) -> None:
    """原子写 JSON（tmp + os.replace）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


class CopyFollower:
    """单个 IBKR 服务器的跟单 follower（一个进程一个实例）。"""

    def __init__(self, name: str, server_cfg: dict, dry_run: bool = False,
                 logs_dir: Optional[Path] = None,
                 servers_path: Optional[Path] = None,
                 gateway=None, order_manager=None) -> None:
        self.name = name
        self.cfg = dict(server_cfg)
        self.logs_dir = Path(logs_dir) if logs_dir else PROJECT_ROOT / "logs"
        # servers_path=None → 不做配置热更新（测试/离线场景）
        self.servers_path = Path(servers_path) if servers_path else None
        self.map_path = self.logs_dir / f"copy-{name}.json"
        self.status_path = self.logs_dir / f"copy-{name}-status.json"
        self.cmd_path = self.logs_dir / f"copy-{name}-cmd.json"
        self.events_path = self.logs_dir / f"copy-{name}-events.jsonl"

        if gateway is not None:
            self.gateway = gateway
        else:
            self.gateway = IBKRGateway(
                host=self.cfg.get("host", "127.0.0.1"),
                port=int(self.cfg.get("port", 7497)),
                client_id=int(self.cfg.get("client_id", 13)),
                account_id=self.cfg.get("account_id", ""),
                fa_group=self.cfg.get("fa_group", ""),
                enabled=bool(self.cfg.get("enabled")) and not dry_run,
            )
        self.order_manager = order_manager
        self._managed = gateway is None or order_manager is not None
        self._copy_lock = asyncio.Lock()
        self._last_snapshot = 0.0
        self._next_reconcile = 0.0
        self._recovery_block = ""
        self._entry_blocks = {}
        self._managed_ready = False
        self._simulation = bool(dry_run)
        self.gateway.disconnect_callback = self._on_ibkr_disconnect
        if isinstance(self.gateway, IBKRGateway):
            self.gateway.on_order_audit = self._event

        # master lot_id -> {"symbol","side","qty","stop_px","state",
        #                   "follower_order_ids":[],"entry_order_id","stop_order_id"}
        self.map: Dict[str, dict] = {}
        self.skip: set = set()                 # 已见但不跟的 master lot
        self._seen: Dict[str, str] = {}        # master lot_id -> 最近状态
        self._targets: Dict[str, int] = {}
        self._master_lots: Dict[str, dict] = {}  # master lot_id -> 最近快照行
        self._master_equity: Optional[float] = None
        self._first_snapshot = True

        # modify_stop 用的轻量 unit（需 symbol/stop_px/trail_trade 属性）
        self._units: Dict[str, SimpleNamespace] = {}
        # 重试队列
        self._entry_retry: Dict[str, dict] = {}   # lot_id -> {attempts,next_ts}
        self._stop_sync: Dict[str, dict] = {}     # lot_id -> {target,attempts,next_ts,gave_up}
        self._close_retry: Dict[str, float] = {}  # lot_id -> next_ts
        self.failures: List[dict] = []

        self.halted = False
        self.drawdown_pct = 0.0
        self.baseline_equity = 0.0
        self.baseline_date = ""
        self.equity: Optional[float] = None
        self._equity_ts = 0.0

        self.master_ws = False
        self.last_sync_ts = 0.0
        self.last_error = ""
        self.synced = 0
        self._exit = False
        self._map_dirty = False

    # ------------------------------------------------------------------
    # 落盘：映射 / 状态 / 事件
    # ------------------------------------------------------------------

    def _load_map(self) -> None:
        try:
            payload = json.loads(self.map_path.read_text(encoding="utf-8"))
            m = payload.get("map")
            if not isinstance(m, dict):
                raise ValueError("跟单映射格式无效")
            if self._managed and payload.get("binding") not in (None, self._binding()):
                raise ValueError("跟单账户或主端配置已变更")
            risk = payload.get("risk") or {}
            self.halted = bool(risk.get("halted"))
            self.baseline_equity = float(risk.get("baseline_equity", 0))
            self.baseline_date = str(risk.get("baseline_date", ""))
            self.skip = set(payload.get("skip", []))
            blocks = payload.get("entry_blocks", {})
            if not isinstance(blocks, dict) or any(not isinstance(value, str) or not (value == "rejected" or value.startswith("unknown:"))
                                                   for value in blocks.values()):
                raise ValueError("跟单入场状态格式无效")
            self._entry_blocks = blocks
            if isinstance(m, dict):
                self.map = m
                logger.info("映射已加载: %d 条 (%s)", len(self.map), self.map_path)
        except FileNotFoundError:
            if self._managed and self.order_manager and self.order_manager.list_lots():
                self._recovery_block = "跟单控制记录缺失，账本归属待核对"
        except (OSError, ValueError, TypeError, AttributeError) as e:
            self._recovery_block = "跟单映射损坏，需人工对账"
            logger.error("映射文件损坏 (%s): %s", self.map_path, e)

    def _save_map(self) -> None:
        if self._recovery_block:
            return
        _atomic_write_json(self.map_path, {
            "map": self.map, "skip": sorted(self.skip), "entry_blocks": self._entry_blocks,
            "binding": self._binding(),
            "risk": {"halted": self.halted, "baseline_equity": self.baseline_equity,
                     "baseline_date": self.baseline_date},
        })
        self._map_dirty = False

    def _binding(self):
        return {key: self.cfg.get(key, default) for key, default in (
            ("host", "127.0.0.1"), ("port", 7497), ("client_id", 13),
            ("account_id", ""), ("fa_group", ""), ("master_ws", "ws://127.0.0.1:8000/ws"),
        )}

    def _event(self, event_type: str, **data) -> None:
        try:
            self.events_path.parent.mkdir(parents=True, exist_ok=True)
            rec = {"ts": time.time(), "type": event_type,
                   "name": self.name, **data}
            with open(self.events_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except OSError as e:
            logger.warning("事件写入失败: %s", e)

    def _record_failure(self, master_lot_id: str, action: str, error: str,
                        retries: int = 0) -> None:
        for f in self.failures:
            if (f["master_lot_id"] == master_lot_id and f["action"] == action
                    and not f.get("dismissed")):
                f.update({"ts": time.time(), "error": error, "retries": retries})
                return
        self.failures.append({"ts": time.time(), "master_lot_id": master_lot_id,
                              "action": action, "error": error,
                              "retries": retries, "dismissed": False})

    def _clear_failures(self, master_lot_id: str,
                        action: Optional[str] = None) -> None:
        self.failures = [f for f in self.failures
                         if not (f["master_lot_id"] == master_lot_id
                                 and (action is None or f["action"] == action))]

    def _active_failure(self, master_lot_id: str, action: str) -> bool:
        return any(f["master_lot_id"] == master_lot_id and f["action"] == action
                   and not f.get("dismissed") for f in self.failures)

    # ------------------------------------------------------------------
    # 配置热更新（master 侧拥有 copy_servers.json，follower 只读）
    # ------------------------------------------------------------------

    def reload_config(self) -> None:
        if self.servers_path is None:
            return
        try:
            payload = json.loads(self.servers_path.read_text(encoding="utf-8"))
            servers = payload.get("servers") or []
            entry = next((s for s in servers if s.get("name") == self.name), None)
            if entry is None:
                logger.warning("配置中找不到 name==%s，保持现有参数", self.name)
                return
        except FileNotFoundError:
            return
        except (OSError, ValueError, TypeError) as e:
            logger.warning("配置重读失败，保持现有参数: %s", e)
            return
        if not entry.get("enabled", False):
            logger.warning("配置 enabled=false，进程退出（监管方不应再拉起）")
            self._exit = True
            return
        # 热更新运行参数（连接参数 host/port/client_id 变更需重启进程）
        for key in ("size_mode", "risk_usd", "size_multiplier",
                    "max_drawdown_pct", "direction"):
            if key in entry:
                self.cfg[key] = entry[key]

    # ------------------------------------------------------------------
    # 股数 / 定价
    # ------------------------------------------------------------------

    def _compute_qty(self, lot: dict):
        """按 size_mode 计算 follower 股数；返回 (qty, error)。"""
        mode = self.cfg.get("size_mode", "risk")
        mult = float(self.cfg.get("size_multiplier", 1.0))
        avg_fill = float(lot.get("avg_fill") or 0)
        stop_px = float(lot.get("stop_px") or 0)
        master_qty = int(lot.get("filled_qty") or 0) if self._managed else int(lot.get("qty") or 0)
        if mode == "risk":
            if avg_fill <= 0 or stop_px <= 0:
                return 0, "master avg_fill/stop_px 缺失，无法以损定量"
            if self._managed and (avg_fill - stop_px) * (1 if lot.get("side") == "long" else -1) <= 0:
                return 0, "保护止损已到成本或越过成本，不能据此定量"
            dist = max(0.01, abs(avg_fill - stop_px))
            qty = math.floor(float(self.cfg.get("risk_usd", 0)) * mult / dist)
        elif mode == "multiplier":
            qty = math.floor(master_qty * mult)
        elif mode == "balance":
            f_eq, m_eq = self.equity, self._master_equity
            if not f_eq or f_eq <= 0 or not m_eq or m_eq <= 0:
                return 0, "净值未知，无法按 balance 计算股数"
            qty = math.floor(master_qty * (f_eq / m_eq) * mult)
        else:
            return 0, f"未知 size_mode: {mode}"
        if qty < 1:
            return 0, "股数不足 1"
        return qty, ""

    def _entry_limit(self, side: str, avg_fill: float) -> float:
        """marketable 限价 = master avg_fill ×(1±0.5%，多+/空−)，
        与系统市价单惯例一致（不用裸 MARKET，延迟行情会被拒 error 10349）。"""
        premium = config.ENTRY_LIMIT_PREMIUM_PCT / 100.0
        sign = 1 if side == "long" else -1
        return round(avg_fill * (1 + premium * sign), 2)

    def _direction_ok(self, side: str) -> bool:
        direction = self.cfg.get("direction", "both")
        if direction == "long_only" and side == "short":
            return False
        if direction == "short_only" and side == "long":
            return False
        return True

    # ------------------------------------------------------------------
    # 启动 / 恢复
    # ------------------------------------------------------------------

    async def startup(self) -> None:
        """加载映射、连接券商、恢复订单句柄、建立净值基准、券商持仓只读核对。"""
        self._load_map()
        connected = await self.gateway.connect()
        if not connected:
            logger.warning("IBKR 未连接（gateway 自带重连机制会继续尝试）")
        if self._managed:
            await self._managed_reconcile()
            await self._check_drawdown()
            self._save_map()
            return
        # 恢复 STP 句柄 + 轻量 unit
        trades = None
        if getattr(self.gateway, "_connected", False):
            try:
                trades = await self.gateway.trade_map()
            except Exception as e:
                logger.warning("订单快照恢复失败: %s", e)
        for mid, rec in self.map.items():
            unit = SimpleNamespace(symbol=rec["symbol"],
                                   stop_px=rec["stop_px"], trail_trade=None,
                                   trail_modify_blocked_until=0.0)
            if rec["state"] == F_ACTIVE and trades:
                t = trades.get(rec.get("stop_order_id"))
                if t is not None:
                    unit.trail_trade = t
            if rec["state"] == F_ACTIVE and unit.trail_trade is None \
                    and getattr(self.gateway, "enabled", False):
                logger.warning("%s STP 句柄未恢复（orderId=%s），改止损将失败并"
                               "进入重试/失败队列", mid, rec.get("stop_order_id"))
            self._units[mid] = unit
        # 券商侧持仓只读核对（异常只打日志，不自动处理）
        if getattr(self.gateway, "_connected", False):
            try:
                positions = await self.gateway.positions()
                if positions is not None:
                    expected: Dict[str, float] = {}
                    for rec in self.map.values():
                        if rec["state"] != F_ACTIVE:
                            continue
                        ticker = rec["symbol"].split(":")[-1]
                        sign = 1 if rec["side"] == "long" else -1
                        expected[ticker] = expected.get(ticker, 0.0) \
                            + sign * rec["qty"]
                    for ticker in sorted(set(expected) | set(positions)):
                        exp, act = expected.get(ticker, 0.0), positions.get(ticker, 0.0)
                        if abs(exp - act) >= 1:
                            logger.warning("券商持仓核对异常 %s: 映射期望 %.0f 股，"
                                           "券商实际 %.0f 股（不自动处理，请人工核查）",
                                           ticker, exp, act)
                            self._event("copy_position_mismatch", ticker=ticker,
                                        expected=exp, actual=act)
            except Exception as e:
                logger.warning("券商持仓核对失败（只读，不影响启动）: %s", e)
        # 净值基准
        await self._refresh_equity(force=True)
        if self.equity and self.equity > 0:
            self.baseline_equity = self.equity
            self.baseline_date = time.strftime("%Y-%m-%d")
        self._event("copy_startup",
                    dry_run=not getattr(self.gateway, "enabled", False),
                    mapped=len(self.map),
                    copy_existing=bool(self.cfg.get("copy_existing_on_start")),
                    baseline_equity=self.baseline_equity)
        logger.info("copy_trader %s 启动完成（执行=%s，映射 %d 条，基准净值 %.2f）",
                    self.name,
                    "实盘" if getattr(self.gateway, "enabled", False) else "dry-run",
                    len(self.map), self.baseline_equity)

    def _on_ibkr_disconnect(self) -> None:
        self._managed_ready = False
        self._next_reconcile = 0.0
        self.last_error = "IBKR 断线（gateway 自动重连中）"
        logger.warning("IBKR 断线回调")

    # ------------------------------------------------------------------
    # master 快照处理（核心状态机）
    # ------------------------------------------------------------------

    async def process_snapshot(self, snap: dict) -> None:
        """处理一帧 master /ws 快照。"""
        if not isinstance(snap, dict) or snap.get("type") not in (None, "snapshot"):
            return
        if self._managed:
            async with self._copy_lock:
                await self._process_managed_snapshot(snap)
            return
        lots = snap.get("lots") or []
        status = snap.get("status") or {}
        if status.get("equity"):
            self._master_equity = status["equity"]
        self.last_sync_ts = time.time()
        first = self._first_snapshot
        self._first_snapshot = False

        for lot in lots:
            mid = lot.get("lot_id")
            if not mid:
                continue
            state = lot.get("state")
            self._master_lots[mid] = lot
            self._seen[mid] = state
            rec = self.map.get(mid)

            if rec and rec["state"] in (F_ACTIVE, F_CLOSING):
                if state == M_CLOSED:
                    # master 已平仓（任何原因）→ follower 市价平仓
                    await self._request_close(mid, lot)
                    continue
                if rec["state"] == F_ACTIVE:
                    mstop = float(lot.get("stop_px") or 0)
                    if mstop > 0 and abs(mstop - rec["stop_px"]) > 1e-9:
                        self._schedule_stop_sync(mid, mstop)
                continue

            if mid in self.skip:
                continue

            if state == M_ACTIVE and not lot.get("external_managed") and int(lot.get("filled_qty") or 0) > 0:
                # 首帧快照：已 ACTIVE 的历史仓按配置决定跟不跟
                if first and not self.cfg.get("copy_existing_on_start"):
                    self.skip.add(mid)
                    logger.info("已见但不跟（启动时已有 ACTIVE 仓）: %s %s",
                                mid, lot.get("symbol"))
                    continue
                if self._active_failure(mid, "entry"):
                    continue   # 入场已永久失败，等人工 retry/dismiss
                if mid in self._entry_retry:
                    continue   # 退避重试中
                if self.halted:
                    logger.warning("熔断中，跳过新开仓镜像: %s %s",
                                   mid, lot.get("symbol"))
                    self._event("copy_entry_skipped", master_lot_id=mid,
                                symbol=lot.get("symbol"), reason="halted")
                    continue
                if not self._direction_ok(lot.get("side")):
                    self.skip.add(mid)
                    logger.info("方向过滤跳过: %s %s %s (direction=%s)",
                                mid, lot.get("symbol"), lot.get("side"),
                                self.cfg.get("direction"))
                    self._event("copy_entry_skipped", master_lot_id=mid,
                                symbol=lot.get("symbol"),
                                side=lot.get("side"), reason="direction")
                    continue
                await self._request_entry(lot)

        await self._process_retries()
        if self._map_dirty:
            self._save_map()

    # ------------------------------------------------------------------
    # 入场 / 改止损 / 平仓
    # ------------------------------------------------------------------

    async def _managed_reconcile(self) -> None:
        if self._recovery_block or not self.order_manager:
            return
        if any(row.get("state") != F_CLOSED for row in self.map.values()):
            self._recovery_block = "旧跟单映射有未核验订单，完成券商对账和迁移前暂停跟单"
            return
        if not self.gateway.enabled:
            self._managed_ready = self._simulation
            return
        if not self.gateway._connected:
            self._managed_ready = False
            return
        now = time.time()
        if now < self._next_reconcile:
            return
        self._next_reconcile = now + 15
        self._managed_ready = False
        await self.order_manager.reconcile()
        recovery = self.order_manager.recovery_status()
        self._managed_ready = (
            recovery["phase"] == "checked" and not recovery["position_differences"]
        )

    def _managed_groups(self):
        groups = {}
        for row in self.order_manager.list_lots():
            mid = (row.get("signal_snapshot") or {}).get("copy_master_lot_id")
            if mid:
                groups.setdefault(mid, []).append(row)
        return groups

    async def _process_managed_snapshot(self, snap):
        status = snap.get("status") or {}
        stamp = snap.get("ts")
        if (not isinstance(stamp, (int, float)) or not math.isfinite(stamp)
                or not -5 <= time.time() - stamp <= 15 or stamp <= self._last_snapshot):
            return
        if self.gateway.enabled and status.get("dry_run") is not False:
            return
        self._last_snapshot = stamp
        self.last_sync_ts = time.time()
        self._master_equity = status.get("equity")
        if self._recovery_block or not self._managed_ready:
            return
        if self.gateway.enabled and not self.gateway._connected:
            return
        if self._map_dirty:
            self._save_map()
        groups = self._managed_groups()
        for lot in snap.get("lots") or []:
            if time.time() - stamp > 15:
                break
            mid = lot.get("lot_id")
            if not isinstance(mid, str) or not mid:
                continue
            state = lot.get("state")
            self._seen[mid] = state
            rows = groups.get(mid, [])
            block = self._entry_blocks.get(mid, "")
            if block.startswith("unknown:"):
                attempt = block.partition(":")[2]
                match = [row for row in rows if (row.get("signal_snapshot") or {}).get("copy_attempt_id") == attempt]
                if len(match) == 1 and not match[0].get("entry_submission_pending"):
                    state_after_reconcile = match[0]["state"]
                    if state_after_reconcile in ("FAILED", "CANCELLED"):
                        self._entry_blocks[mid] = "rejected"
                        self._record_failure(mid, "entry", match[0].get("note") or "券商确认订单未成交")
                        self._map_dirty = True
                    elif state_after_reconcile == "ACTIVE":
                        self._entry_blocks.pop(mid)
                        self._clear_failures(mid, "entry")
                        self._map_dirty = True
            if not rows and (lot.get("external_managed") or mid in self.map or mid in self.skip):
                continue
            if not rows and self._first_snapshot and not self.cfg.get("copy_existing_on_start"):
                if state in (M_ACTIVE, M_CLOSING):
                    self.skip.add(mid)
                    self._map_dirty = True
                continue
            if state not in (M_ACTIVE, M_PENDING, M_CLOSING, M_CLOSED, M_CANCELLED):
                continue
            await self._sync_managed_lot(mid, lot, rows)
        self._first_snapshot = False
        if self._map_dirty:
            self._save_map()

    async def _sync_managed_lot(self, mid, master, rows):
        state = master["state"]
        if state == M_PENDING and not rows:
            return
        if not rows and state != M_ACTIVE:
            return
        actual = max(0, int(master.get("filled_qty") or 0))
        live = [row for row in rows if row["state"] in ("ACTIVE", "CLOSING", "PENDING")]
        if any(row.get("entry_submission_pending") or row.get("partial_exit")
               or row["state"] == "CLOSING" for row in live):
            return
        # CLOSED retains its historical filled quantity in the master snapshot.
        closing = state in (M_CLOSING, M_CLOSED, M_CANCELLED)
        if rows:
            ratio = rows[0]["signal_snapshot"]["copy_size_ratio"]
            target = math.floor(actual * ratio + 1e-9)
        else:
            if not actual or self.halted or not self._direction_ok(master.get("side")):
                return
            target, error = self._compute_qty(master)
            if error:
                self._record_failure(mid, "entry", error)
                return
            ratio = target / actual
        if state in (M_CLOSED, M_CANCELLED):
            target = 0
        self._targets[mid] = target
        held = sum(row["filled_qty"] for row in live)
        pending = [row for row in live if row["state"] == "PENDING"]
        if pending:
            planned = held + sum(max(0, row["qty"] - row["filled_qty"]) for row in pending)
            if closing or planned > target:
                for row in pending:
                    await self.order_manager.cancel_lot(row["lot_id"])
            return
        price = float(master.get("exit_px") or master.get("last_px") or master.get("avg_fill") or 0)
        if held > target:
            row = next((row for row in reversed(live) if row["filled_qty"] > 0), None)
            if row is None:
                return
            qty = min(held - target, row["filled_qty"])
            if qty == row["filled_qty"]:
                ok, error = await self.order_manager.close_lot(row["lot_id"], price, "copy_exit")
            else:
                ok, error = await self.order_manager.close_half(
                    row["lot_id"], price, "copy_reduce", quantity=qty,
                )
            if not ok:
                self._record_failure(mid, "close", error)
            else:
                self._clear_failures(mid, "close")
            return
        stop = float(master.get("stop_px") or 0)
        for row in live:
            tighter = stop > row["stop_px"] if row["side"] == "long" else 0 < stop < row["stop_px"]
            if tighter:
                ok, error = await self.order_manager.update_trail(row["lot_id"], stop)
                if not ok:
                    self._record_failure(mid, "modify", error)
                else:
                    self._clear_failures(mid, "modify")
        if closing or held >= target or self.halted or not self.equity or self.equity <= 0:
            return
        if any(row["state"] in ("FAILED", "CANCELLED", "CLOSED")
               or row.get("close_filled_qty", 0) for row in rows):
            return  # Do not refill after cancellation, stop execution or manual intervention.
        if self.order_manager.recovery_status()["blocks_entry"]:
            return
        if mid in self._entry_blocks:
            return
        symbol, side = master.get("symbol"), master.get("side")
        avg = float(master.get("avg_fill") or 0)
        if not symbol or side not in ("long", "short") or not all(
            math.isfinite(p) and p > 0 for p in (avg, stop)
        ):
            return
        limit = self._entry_limit(side, avg)
        qty = min(target - held, config.RISK_MAX_ORDER_QTY)
        if self.cfg.get("size_mode", "risk") == "risk":
            dist = (limit - stop) * (1 if side == "long" else -1)
            if dist <= 0:
                return
            budget = float(self.cfg.get("risk_usd", 0)) * float(self.cfg.get("size_multiplier", 1))
            qty = min(qty, max(0, math.floor(budget / dist)))
        if qty <= 0:
            return
        if not rows:
            ratio = qty / actual
        attempt = uuid.uuid4().hex
        self._entry_blocks[mid] = f"unknown:{attempt}"
        self._save_map()
        try:
            placed, error = await self.order_manager.place_limit_stop_lot(
                symbol, side, qty, limit, stop, limit_type="copy", entry_order_type="LMT",
                trader=f"copy:{self.name}",
                signal_snapshot={"copy_master_lot_id": mid, "copy_size_ratio": ratio,
                                 "copy_attempt_id": attempt},
            )
        except Exception:
            self._next_reconcile = 0
            self._record_failure(mid, "entry", "券商受理结果未知，等待对账")
            logger.exception("跟单入场结果未知 %s", mid)
            return
        if placed is None:
            if any((row.get("signal_snapshot") or {}).get("copy_attempt_id") == attempt
                   for row in self._managed_groups().get(mid, [])):
                self._next_reconcile = 0
                self._record_failure(mid, "entry", "券商订单待对账")
                return
            self._entry_blocks[mid] = "rejected"
            self._record_failure(mid, "entry", error)
        else:
            self._entry_blocks.pop(mid)
            self._clear_failures(mid, "entry")
            self.synced += 1
        self._save_map()

    def _managed_status(self):
        orders = []
        recovery = self.order_manager.recovery_status()
        for mid, rows in self._managed_groups().items():
            for row in rows:
                orders.append({
                    "master_lot_id": mid, "follower_lot_id": row["lot_id"],
                    "symbol": row["symbol"], "side": row["side"],
                    "master_state": self._seen.get(mid, ""), "follower_state": row["state"],
                    "qty": row["filled_qty"] if row["state"] != "CLOSED" else 0,
                    "target_qty": self._targets.get(mid),
                    "pending_qty": max(0, row["qty"] - row["filled_qty"]) if row["state"] == "PENDING" else 0,
                    "protection_qty": row.get("protection_order_qty"),
                    "stop_px": row["stop_px"], "error": row.get("note", ""),
                })
        return {
            "name": self.name, "ts": time.time(), "connected": self.gateway._connected,
            "master_ws": self.master_ws, "equity": self.equity, "synced": self.synced,
            "pending_map": sum(row["follower_state"] in ("ACTIVE", "PENDING", "CLOSING") for row in orders),
            "failed": sum(not row.get("dismissed") for row in self.failures),
            "last_sync_ts": self.last_sync_ts, "drawdown_pct": self.drawdown_pct,
            "halted": self.halted, "orders": orders, "failures": self.failures,
            "entry_blocks": {mid: state.partition(":")[0] for mid, state in self._entry_blocks.items()},
            "recovery": recovery, "last_error": self._recovery_block or (
                "跟单账户尚未完成对账或持仓存在差异" if not self._managed_ready else
                "跟单订单待核对，暂停新入场" if recovery["blocks_entry"] else self.last_error
            ),
        }

    async def _request_entry(self, lot: dict) -> None:
        """首次转 ACTIVE → 市价入场 + STP 同发；失败进指数退避重试。"""
        mid = lot["lot_id"]
        result = await self._attempt_entry(lot)
        if result == "ok":
            return
        if result == "fatal":
            return   # 确定性失败（如股数不足 1），已记 failures，不重试
        self._entry_retry[mid] = {"attempts": 1,
                                  "next_ts": time.time() + RETRY_BACKOFF[0]}

    async def _attempt_entry(self, lot: dict) -> str:
        """执行一次入场尝试；返回 "ok" / "retry" / "fatal"。"""
        mid = lot["lot_id"]
        if lot.get("external_managed"):
            return "fatal"
        symbol, side = lot.get("symbol"), lot.get("side")
        avg_fill = float(lot.get("avg_fill") or 0)
        stop_px = float(lot.get("stop_px") or 0)
        if not symbol or side not in ("long", "short"):
            self._record_failure(mid, "entry", "master lot symbol/side 非法")
            self._event("copy_entry_failed", master_lot_id=mid,
                        error="symbol/side 非法")
            return "fatal"
        if avg_fill <= 0 or stop_px <= 0:
            self._record_failure(mid, "entry",
                                 "master avg_fill/stop_px 缺失")
            self._event("copy_entry_failed", master_lot_id=mid, symbol=symbol,
                        error="avg_fill/stop_px 缺失")
            return "fatal"
        qty, err = self._compute_qty(lot)
        if err:
            self._record_failure(mid, "entry", err)
            self._event("copy_entry_failed", master_lot_id=mid, symbol=symbol,
                        side=side, error=err)
            return "fatal"
        limit = self._entry_limit(side, avg_fill)
        if mid in self._entry_blocks:
            return "fatal"
        self._entry_blocks[mid] = "unknown:legacy"
        self._save_map()
        try:
            res = await self.gateway.place_limit_with_stop(
                symbol, side, qty, limit, stop_px)
        except Exception as e:
            logger.exception("入场下单异常 %s", symbol)
            self.last_error = f"entry {symbol}: {e}"
            self._record_failure(mid, "entry", "券商受理结果未知，等待人工对账")
            return "fatal"
        if not res.get("ok"):
            err = res.get("error") or f"status={res.get('status')}"
            logger.warning("入场失败 %s %s: %s", symbol, mid, err)
            self.last_error = f"entry {symbol}: {err}"
            self._record_failure(mid, "entry", err)
            return "fatal"
        self._entry_blocks.pop(mid)
        entry_id = res.get("entry_order_id")
        stop_id = res.get("stop_order_id")
        self.map[mid] = {
            "symbol": symbol, "side": side, "qty": qty,
            "stop_px": round(stop_px, 2), "state": F_ACTIVE,
            "follower_order_ids": [i for i in (entry_id, stop_id)
                                   if i is not None],
            "entry_order_id": entry_id, "stop_order_id": stop_id,
        }
        self._map_dirty = True
        self._units[mid] = SimpleNamespace(
            symbol=symbol, stop_px=round(stop_px, 2),
            trail_trade=res.get("stop_trade"),
            trail_modify_blocked_until=0.0)
        self.synced += 1
        self.last_error = ""
        logger.info("跟单入场 %s %s x%d @LMT %.2f | STP %.2f (master %s)",
                    symbol, side, qty, limit, stop_px, mid)
        self._event("copy_entry", master_lot_id=mid, symbol=symbol, side=side,
                    qty=qty, limit_px=limit, stop_px=round(stop_px, 2),
                    master_avg_fill=avg_fill,
                    entry_order_id=entry_id, stop_order_id=stop_id,
                    dry_run=bool(res.get("dry_run")))
        self._clear_failures(mid, "entry")
        # follower 自己 ACTIVE 后先对齐一次 master 最新 stop_px（防秒级窗口）
        cur = self._master_lots.get(mid) or {}
        mstop = float(cur.get("stop_px") or 0)
        if mstop > 0 and abs(mstop - self.map[mid]["stop_px"]) > 1e-9:
            self._schedule_stop_sync(mid, mstop)
        return "ok"

    def _schedule_stop_sync(self, mid: str, target: float) -> None:
        target = round(target, 2)
        st = self._stop_sync.get(mid)
        if st is not None:
            if abs(st["target"] - target) < 1e-9:
                return   # 同一目标已在进行/已放弃
        self._stop_sync[mid] = {"target": target, "attempts": 0,
                                "next_ts": time.time(), "gave_up": False}

    async def _attempt_stop_sync(self, mid: str, target: float) -> bool:
        rec = self.map.get(mid)
        unit = self._units.get(mid)
        if rec is None or unit is None:
            return False
        try:
            ok = await self.gateway.modify_stop(unit, target)
        except Exception as e:
            logger.exception("改止损异常 %s", rec["symbol"])
            self.last_error = f"modify {rec['symbol']}: {e}"
            ok = False
        if ok:
            rec["stop_px"] = round(target, 2)
            self._map_dirty = True
            self.synced += 1
            self.last_error = ""
            logger.info("止损同步 %s -> %.2f (master %s)",
                        rec["symbol"], target, mid)
            self._event("copy_stop_synced", master_lot_id=mid,
                        symbol=rec["symbol"], stop_px=round(target, 2))
            self._clear_failures(mid, "modify")
        else:
            self.last_error = f"modify {rec['symbol']} -> {target} 失败"
            self._event("copy_stop_sync_failed", master_lot_id=mid,
                        symbol=rec["symbol"], stop_px=round(target, 2))
        return ok

    async def _request_close(self, mid: str, lot: Optional[dict]) -> None:
        """master CLOSED → follower 市价平仓（无限重试，每 30s）。"""
        rec = self.map.get(mid)
        if rec is None or rec["state"] == F_CLOSED:
            return
        rec["state"] = F_CLOSING
        self._map_dirty = True
        # 先撤 STP 保护单（best-effort）：GTC 子单在平仓后仍存活，
        # 不撤会在空仓上触发反向开仓
        unit = self._units.get(mid)
        trail = getattr(unit, "trail_trade", None) if unit else None
        if trail is not None:
            try:
                if not self.gateway.cancel_order(trail):
                    logger.warning("%s STP 撤单失败（继续平仓，需人工核查残留保护单）",
                                   rec["symbol"])
            except Exception as e:
                logger.warning("%s STP 撤单异常: %s（继续平仓）", rec["symbol"], e)
        ok = await self._attempt_close(mid, lot)
        if not ok:
            self._close_retry[mid] = time.time() + CLOSE_RETRY_SECONDS

    async def _attempt_close(self, mid: str, lot: Optional[dict]) -> bool:
        rec = self.map.get(mid)
        if rec is None:
            return True
        lot = lot or self._master_lots.get(mid) or {}
        ref_px = (lot.get("exit_px") or lot.get("last_px")
                  or lot.get("avg_fill") or rec["stop_px"])
        try:
            res = await self.gateway.place_close(
                rec["symbol"], rec["side"], rec["qty"], float(ref_px),
                order_ref=f"copy-{self.name}-{mid}")
        except Exception as e:
            logger.exception("平仓异常 %s", rec["symbol"])
            res = {"ok": False, "error": str(e)}
        if res.get("ok"):
            rec["state"] = F_CLOSED
            self._map_dirty = True
            self._close_retry.pop(mid, None)
            self.synced += 1
            self.last_error = ""
            logger.info("跟单平仓 %s %s x%d (master %s)",
                        rec["symbol"], rec["side"], rec["qty"], mid)
            self._event("copy_close", master_lot_id=mid,
                        symbol=rec["symbol"], side=rec["side"],
                        qty=rec["qty"], ref_px=ref_px,
                        order_id=res.get("order_id"),
                        dry_run=bool(res.get("dry_run")))
            self._clear_failures(mid, "close")
            return True
        err = res.get("error") or f"status={res.get('status')}"
        self.last_error = f"close {rec['symbol']}: {err}"
        tries = next((f["retries"] for f in self.failures
                      if f["master_lot_id"] == mid and f["action"] == "close"
                      and not f.get("dismissed")), 0)
        self._record_failure(mid, "close", err, retries=tries + 1)
        logger.warning("跟单平仓失败 %s (master %s): %s——%ds 后重试",
                       rec["symbol"], mid, err, int(CLOSE_RETRY_SECONDS))
        self._event("copy_close_failed", master_lot_id=mid,
                    symbol=rec["symbol"], side=rec["side"],
                    qty=rec["qty"], error=err)
        return False

    # ------------------------------------------------------------------
    # 重试队列
    # ------------------------------------------------------------------

    async def _process_retries(self) -> None:
        now = time.time()
        # entry：指数退避最多 3 次
        for mid, st in list(self._entry_retry.items()):
            if now < st["next_ts"]:
                continue
            lot = self._master_lots.get(mid)
            if not lot or lot.get("state") != M_ACTIVE:
                self._entry_retry.pop(mid, None)
                continue
            if self.halted:
                continue   # 熔断期间暂停入场重试，解除后自动继续
            result = await self._attempt_entry(lot)
            if result == "ok" or result == "fatal":
                self._entry_retry.pop(mid, None)
                continue
            st["attempts"] += 1
            if st["attempts"] >= MAX_TRIES:
                self._entry_retry.pop(mid, None)
                err = self.last_error or "入场失败"
                self._record_failure(mid, "entry", err,
                                     retries=st["attempts"])
                self._event("copy_entry_failed", master_lot_id=mid,
                            symbol=lot.get("symbol"),
                            side=lot.get("side"), error=err,
                            retries=st["attempts"])
            else:
                st["next_ts"] = now + RETRY_BACKOFF[min(
                    st["attempts"], len(RETRY_BACKOFF) - 1)]
        # modify：指数退避最多 3 次，超过进 failures 等人工
        for mid, st in list(self._stop_sync.items()):
            if now < st["next_ts"] or st.get("gave_up"):
                continue
            rec = self.map.get(mid)
            if rec is None or rec["state"] != F_ACTIVE:
                self._stop_sync.pop(mid, None)
                continue
            ok = await self._attempt_stop_sync(mid, st["target"])
            if ok:
                self._stop_sync.pop(mid, None)
                continue
            st["attempts"] += 1
            if st["attempts"] >= MAX_TRIES:
                st["gave_up"] = True
                self._record_failure(mid, "modify",
                                     f"止损同步到 {st['target']} 失败",
                                     retries=st["attempts"])
            else:
                st["next_ts"] = now + RETRY_BACKOFF[min(
                    st["attempts"], len(RETRY_BACKOFF) - 1)]
        # close：无限重试（每 30s，风控动作）
        for mid, next_ts in list(self._close_retry.items()):
            if now < next_ts:
                continue
            rec = self.map.get(mid)
            if rec is None or rec["state"] == F_CLOSED:
                self._close_retry.pop(mid, None)
                continue
            ok = await self._attempt_close(mid, None)
            if not ok:
                self._close_retry[mid] = now + CLOSE_RETRY_SECONDS

    # ------------------------------------------------------------------
    # 净值 / 回撤熔断
    # ------------------------------------------------------------------

    async def _refresh_equity(self, force: bool = False) -> None:
        now = time.time()
        if not force and now - self._equity_ts < EQUITY_CACHE_SECONDS:
            return
        self._equity_ts = now
        try:
            eq = await self.gateway.net_liquidation()
        except Exception as e:
            logger.warning("净值查询异常: %s", e)
            return
        if eq is not None and eq > 0:
            self.equity = eq

    async def _check_drawdown(self) -> None:
        """回撤熔断：启动时（或每日首个）净值为基准，回撤超限 → halted。

        熔断只停新开仓镜像；已有仓位继续改止损/平仓同步。
        净值恢复不自动解除（页面 cmd {"unhalt": true} 手动解除）。"""
        await self._refresh_equity()
        if not self.equity or self.equity <= 0:
            return
        today = datetime.now(ET).date().isoformat()
        if self.baseline_equity <= 0 or self.baseline_date != today:
            if not self.halted:
                self.baseline_equity = self.equity
                self.baseline_date = today
                self._map_dirty = True
        if self.baseline_equity <= 0:
            return
        dd = max(0.0, (self.baseline_equity - self.equity)
                 / self.baseline_equity * 100.0)
        self.drawdown_pct = round(dd, 2)
        limit = float(self.cfg.get("max_drawdown_pct", 0) or 0)
        if not self.halted and limit > 0 and dd > limit:
            self.halted = True
            self._map_dirty = True
            logger.warning("回撤熔断触发: 净值 %.2f 较基准 %.2f 回撤 %.2f%% > %.2f%%"
                           "——停止新开仓镜像（已有仓位继续管理/平仓同步）",
                           self.equity, self.baseline_equity, dd, limit)
            self._event("copy_halted", equity=self.equity,
                        baseline_equity=self.baseline_equity,
                        drawdown_pct=self.drawdown_pct,
                        max_drawdown_pct=limit)

    # ------------------------------------------------------------------
    # 人工指令（master 页面写 cmd 文件，follower 每 2s 读，消费后删除）
    # ------------------------------------------------------------------

    async def _process_cmd(self) -> None:
        if not self.cmd_path.exists():
            return
        try:
            data = json.loads(self.cmd_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            logger.warning("cmd 文件读取失败: %s", e)
            return
        try:
            self.cmd_path.unlink()
        except OSError:
            pass
        if not isinstance(data, dict):
            return
        for mid in data.get("dismiss") or []:
            for f in self.failures:
                if f["master_lot_id"] == mid:
                    f["dismissed"] = True
            logger.info("人工忽略失败: %s", mid)
            self._event("copy_cmd_dismiss", master_lot_id=mid)
        if data.get("unhalt"):
            self.halted = False
            self._map_dirty = True
            self.drawdown_pct = 0.0
            if self.equity and self.equity > 0:
                self.baseline_equity = self.equity
                self.baseline_date = datetime.now(ET).date().isoformat()
            logger.warning("人工解除熔断（新基准净值 %.2f）", self.baseline_equity)
            self._event("copy_unhalted", baseline_equity=self.baseline_equity)
        for mid in data.get("retry") or []:
            if self._managed:
                self._next_reconcile = 0.0
                if self._entry_blocks.get(mid) == "rejected":
                    self._entry_blocks.pop(mid)
                    self._clear_failures(mid, "entry")
                    self._map_dirty = True
                continue
            self._clear_failures(mid)
            rec = self.map.get(mid)
            lot = self._master_lots.get(mid)
            if rec and rec["state"] == F_CLOSING:
                self._close_retry[mid] = 0.0
            elif rec and rec["state"] == F_ACTIVE and lot:
                mstop = float(lot.get("stop_px") or 0)
                if mstop > 0:
                    self._stop_sync.pop(mid, None)
                    self._schedule_stop_sync(mid, mstop)
            elif rec is None and lot and lot.get("state") == M_ACTIVE:
                self._entry_retry[mid] = {"attempts": 0, "next_ts": 0.0}
            logger.info("人工重放失败动作: %s", mid)
            self._event("copy_cmd_retry", master_lot_id=mid)
        await self._process_retries()
        if self._map_dirty:
            self._save_map()

    # ------------------------------------------------------------------
    # 状态文件（每 2s，供 master 页面读取）
    # ------------------------------------------------------------------

    def _build_status(self) -> dict:
        if self._managed and self.order_manager:
            return self._managed_status()
        orders = []
        for mid, rec in self.map.items():
            err = next((f["error"] for f in reversed(self.failures)
                        if f["master_lot_id"] == mid
                        and not f.get("dismissed")), "")
            orders.append({
                "master_lot_id": mid,
                "symbol": rec["symbol"],
                "side": rec["side"],
                "master_state": self._seen.get(mid, ""),
                "follower_state": rec["state"],
                "qty": rec["qty"],
                "stop_px": rec["stop_px"],
                "error": err,
            })
        return {
            "name": self.name,
            "ts": time.time(),
            "connected": bool(getattr(self.gateway, "_connected", False)),
            "equity": self.equity,
            "master_ws": self.master_ws,
            "synced": self.synced,
            "pending_map": sum(1 for r in self.map.values()
                               if r["state"] in (F_ACTIVE, F_CLOSING)),
            "failed": sum(1 for f in self.failures if not f.get("dismissed")),
            "last_sync_ts": self.last_sync_ts,
            "last_error": self.last_error,
            "drawdown_pct": self.drawdown_pct,
            "halted": self.halted,
            "orders": orders,
            "failures": self.failures,
        }

    def write_status(self) -> None:
        try:
            _atomic_write_json(self.status_path, self._build_status())
        except OSError as e:
            logger.warning("状态文件写入失败: %s", e)

    # ------------------------------------------------------------------
    # 周期任务 / 主循环
    # ------------------------------------------------------------------

    async def tick(self) -> None:
        """一拍周期工作：配置热更新、回撤检查、cmd、重试、状态落盘。"""
        self.reload_config()
        await self._check_drawdown()
        await self._process_cmd()
        await self._process_retries()
        self.write_status()

    async def _periodic_loop(self) -> None:
        """1s 一拍；状态/cmd 2s、配置 5s（按计数分频）。"""
        n = 0
        while not self._exit:
            await asyncio.sleep(1.0)
            n += 1
            try:
                if n % int(CONFIG_RELOAD_SECONDS) == 0:
                    self.reload_config()
                if self._managed:
                    async with self._copy_lock:
                        await self._managed_reconcile()
                        await self._check_drawdown()
                        if n % int(CMD_POLL_SECONDS) == 0:
                            await self._process_cmd()
                        if self._map_dirty:
                            self._save_map()
                        if n % int(STATUS_SECONDS) == 0:
                            self.write_status()
                    continue
                await self._check_drawdown()
                if n % int(CMD_POLL_SECONDS) == 0:
                    await self._process_cmd()
                await self._process_retries()
                if n % int(STATUS_SECONDS) == 0:
                    self.write_status()
                if self._map_dirty:
                    self._save_map()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.exception("周期任务异常: %s", e)

    async def _ws_loop(self) -> None:
        """master /ws 消费循环；断线指数退避重连（1s→2s→…→30s 封顶）。
        注意用 legacy 客户端实现：websockets 16 的新 asyncio 客户端在本环境
        （Python 3.11 + uvicorn）握手会被 EOF（实测连最小 uvicorn ws 也失败），
        legacy 实现稳定可用。"""
        from websockets.exceptions import SecurityError
        from websockets.legacy.client import Connect

        class CopyConnect(Connect):
            def handle_redirect(self, uri):
                raise SecurityError("跟单通道不接受重定向")

        backoff = 1.0
        while not self._exit:
            try:
                url = copy_channel_url(self.cfg.get("master_ws", "ws://127.0.0.1:8000/ws"))
                token = copy_channel_token()
                if not token:
                    raise RuntimeError("缺少有效的 COPY_CHANNEL_TOKEN")
                async with CopyConnect(url, extra_headers={"Authorization": f"Bearer {token}"},
                                      ping_interval=20,
                                      ping_timeout=10,
                                      max_size=16 * 1024 * 1024) as ws:
                    was_reconnect = backoff > 1.0 or self.last_sync_ts > 0
                    self.master_ws = True
                    if was_reconnect:
                        logger.warning("master WS 已重连: %s", url)
                        self._event("copy_reconnected", master_ws=url)
                    backoff = 1.0
                    async for raw in ws:
                        if self._exit:
                            break
                        try:
                            snap = json.loads(raw)
                        except ValueError:
                            continue
                        try:
                            await self.process_snapshot(snap)
                        except Exception as e:
                            logger.exception("快照处理异常: %s", e)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.last_error = f"master WS: {type(e).__name__}: {e}"
                logger.warning("master WS 断开 (%s)，%.0fs 后重连",
                               e, backoff)
            self.master_ws = False
            if self._exit:
                break
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, WS_BACKOFF_MAX)

    async def run(self) -> int:
        if not self.cfg.get("enabled", False):
            logger.warning("服务器 %s enabled=false，进程退出（监管方不应再拉起）",
                           self.name)
            return 0
        if self._managed:
            mode = "paper" if self._simulation else "live"
            directory = self.logs_dir / f"copy-{self.name}-{mode}"
            directory.mkdir(parents=True, exist_ok=True)
            if self._simulation:
                self.map_path = directory / "control.json"
                self.status_path = directory / "status.json"
                self.cmd_path = directory / "cmd.json"
                self.events_path = directory / "events.jsonl"
            binding = self._binding()
            gateway_key = hashlib.sha256(
                f"{binding['host']}:{binding['port']}".encode()
            ).hexdigest()[:16]
            lock = directory / "writer.lock" if self._simulation else self.logs_dir / f"copy-gateway-{gateway_key}.lock"
            if not acquire_writer_lock(str(lock.resolve()), owner=self):
                logger.error("跟单写进程已存在: %s", self.name)
                return 1
            if self.order_manager is None:
                risk = RiskGate(RiskConfig(
                    max_order_qty=config.RISK_MAX_ORDER_QTY,
                    max_open_positions=config.RISK_MAX_OPEN_POSITIONS,
                    max_total_risk_usd=config.RISK_MAX_TOTAL_RISK_USD,
                ), halt_dir=directory)
                self.order_manager = OrderManager(self.gateway, risk, directory / "lots.json")
                self.order_manager._load(allow_recovery=True)
        await self.startup()
        periodic = asyncio.create_task(self._periodic_loop())
        ws_task = asyncio.create_task(self._ws_loop())
        try:
            await asyncio.wait(
                [periodic, ws_task], return_when=asyncio.FIRST_EXCEPTION)
        finally:
            for t in (periodic, ws_task):
                t.cancel()
            await asyncio.gather(periodic, ws_task, return_exceptions=True)
            self.write_status()
            if self._map_dirty:
                self._save_map()
            try:
                await self.gateway.disconnect()
            except Exception:
                pass
            if self._managed:
                release_writer_lock(owner=self)
        return 0


def load_server_config(name: str,
                       servers_path: Path = DEFAULT_SERVERS_PATH) -> dict:
    """从 logs/copy_servers.json 读 name 对应的服务器配置。"""
    try:
        payload = json.loads(Path(servers_path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise SystemExit(f"服务器配置不存在: {servers_path}"
                         "（master 侧应先创建 logs/copy_servers.json）")
    except (OSError, ValueError) as e:
        raise SystemExit(f"服务器配置读取失败: {e}")
    for s in payload.get("servers") or []:
        if s.get("name") == name:
            return s
    raise SystemExit(f"配置中找不到 name=={name} 的服务器项")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="autotrade 跟单引擎")
    parser.add_argument("--name", required=True,
                        help="logs/copy_servers.json 中的服务器名")
    parser.add_argument("--dry-run", action="store_true",
                        help="强制 dry-run（虚拟成交，不发真实订单）")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, config.LOG_LEVEL, "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("ib_async").setLevel(logging.WARNING)

    server_cfg = load_server_config(args.name)
    follower = CopyFollower(args.name, server_cfg, dry_run=args.dry_run,
                            servers_path=DEFAULT_SERVERS_PATH)
    try:
        return asyncio.run(follower.run())
    except KeyboardInterrupt:
        logger.info("收到中断信号，退出")
        return 0


if __name__ == "__main__":
    sys.exit(main())
