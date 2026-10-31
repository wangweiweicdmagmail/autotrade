# -*- coding: utf-8 -*-
"""
下单控制台后端 (FastAPI)
=========================

提供给 Web 控制页（console/static）的 REST + WebSocket 接口：

- REST：状态 / 下单（LMT + STP 括号单，双向）/ 订单列表 / 聚合仓位 /
  改止损 / 撤单 / 平仓 / 选股 / 绩效 / 事件 / 跟单服务器管理 / 刷新。
- WebSocket /ws：每 1s 推送一次快照（状态 + 行情 + 仓位 + 全部 lot），
  前端据此实时刷新，无需轮询。

策略体系（2026-10-17 v3）：5M 周期 + 单指标 trend_state。
过滤 = ts_c.trend_state（1 单边多头 / -1 单边空头 / 0 震荡拦截）；
下单方向 = ST 方向（无反向订单）；入场 = ema_fast / channel_edge 两条
回踩限价线；止损 = 锚定价 ∓ STOP_ATR_MULT×ATR（STP 子单，唯一止损形态；
倍数为全局设置，运行时经 POST /api/settings 修改）。

运行（默认 dry-run，不发真实订单）：:

    EXECUTION_ENABLED=0 uvicorn console.app:app --host 127.0.0.1 --port 8000

实盘（先确认 config.RISK_* 与 TWS 已就绪）：:

    EXECUTION_ENABLED=1 uvicorn console.app:app --host 127.0.0.1 --port 8000
"""

import asyncio
import base64
import collections
import json
import logging
import os
import re
import signal
import sys
import time
import uuid
from pathlib import Path
from typing import Dict, List, Optional, Set

# 允许从项目根目录 import config / execution
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import config
from engine.auto_engine import LiveBroker
from engine.pyramid_engine import PyramidEngine
from engine.breakout_tracker import BreakoutTracker
from engine import news_digest
from engine.notifier import Notifier
from engine.realtime_collector import RealtimeCollector
from engine import shadow_report
from engine.shadow_tracker import ShadowTracker
from execution.ibkr import IBKRGateway
from execution.order_lot import OrderManager
from execution.risk import RiskConfig, RiskGate
from execution.writer_lock import acquire_writer_lock

logging.basicConfig(level=getattr(logging, config.LOG_LEVEL, "INFO"),
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("console")

# ib_async 日志降噪：持仓/成交回报极多，压到 WARNING
logging.getLogger("ib_async").setLevel(logging.WARNING)
logging.getLogger("ib_async.wrapper").setLevel(logging.WARNING)
logging.getLogger("ib_async.client").setLevel(logging.WARNING)

# 内存日志环形缓冲区（供前端日志窗口展示）
_log_buffer: "collections.deque" = collections.deque(maxlen=300)


class _BufferHandler(logging.Handler):
    """把日志写入内存环形缓冲区，供 /api/logs 与 WebSocket 推送。"""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            _log_buffer.append({
                "ts": record.created,
                "level": record.levelname,
                "name": record.name.split(".")[-1],
                "msg": record.getMessage(),
            })
        except Exception:
            pass


_buffer_handler = _BufferHandler()
_buffer_handler.setLevel(logging.INFO)
logging.getLogger().addHandler(_buffer_handler)

STATIC_DIR = Path(__file__).resolve().parent / "static"

# ---------------------------------------------------------------------------
# 请求模型
# ---------------------------------------------------------------------------


class UpdateTrailRequest(BaseModel):
    stop_px: float = Field(..., gt=0)


class LimitStopOrderRequest(BaseModel):
    symbol: str = Field(..., description="如 NVDA / NASDAQ:NVDA")
    side: str = "long"
    qty: int = 0                      # 0 = 按净值自动算股数
    limit_px: float = Field(..., gt=0)
    stop_px: float = Field(..., gt=0)
    limit_type: str = "ema"           # ema / channel / midpoint（5M 高低价中点）/ retest（上上根 K 线回踩）/ chase（FVG 追价）/ market（市价单）
    reason: str = ""
    risk_usd: float = 0               # 设定风险金额（风控偏差 10% 容差检查）
    max_position_pct: float = 0       # 仓位上限%（0=不封顶；回踩改价股数封顶用）
    force: bool = False               # 执意开仓：二次确认后放行，跳过 trend_state 过滤拦截
    twap_batches: int = 0             # TWAP 分批数（0/1=不分批；>1=分 N 批时间平均建仓）
    twap_interval_s: int = 60         # TWAP 批次间隔秒（默认 60s，测试可传小值）


class ClientRegisterRequest(BaseModel):
    name: str = Field(..., description="交易员名称（1-20 字符，无特殊符号）")


# ---------------------------------------------------------------------------
# 局域网多客户端：注册 / 审批 / 交易员闸门
# ---------------------------------------------------------------------------
# 本机（回环地址）= master，免确认；非本机客户端首次访问须提交交易员名称注册，
# master 在控制台审批后，客户端持 X-Trader-Token 头方可调用写操作端点。

_LOCAL_ADDRS = ("127.0.0.1", "::1")
# 名称：1-20 字符，字母/数字/中文/下划线/连字符（\w 在 str 模式下含中文）
_CLIENT_NAME_RE = re.compile(r"^[\w\-]{1,20}$")
_CLIENTS_PATH = Path(__file__).resolve().parent.parent / "logs" / "clients.json"
# token 有效期：自审批时刻起，过期后须重新登记并重新走 master 审批
_CLIENT_TOKEN_TTL_SECONDS = config.CLIENT_TOKEN_TTL_HOURS * 3600


def _load_clients() -> dict:
    """启动加载 clients.json（已审批 token 重启后保持有效）。"""
    try:
        payload = json.loads(_CLIENTS_PATH.read_text(encoding="utf-8"))
        tokens = payload.get("tokens")
        if isinstance(tokens, dict):
            return {"tokens": tokens}
    except FileNotFoundError:
        pass
    except (OSError, ValueError, TypeError) as e:
        logger.warning("clients.json 加载失败: %s", e)
    return {"tokens": {}}


def _save_clients() -> None:
    """原子写落盘（tmp + os.replace，参照 lots_state.json 的落盘模式）。"""
    try:
        _CLIENTS_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = _CLIENTS_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(_clients, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        os.replace(tmp, _CLIENTS_PATH)
    except OSError:
        logger.exception("clients.json 落盘失败")


_clients: dict = _load_clients()


# ---------------------------------------------------------------------------
# 全局设置（logs/settings.json 持久化，运行时经 POST /api/settings 修改）
# ---------------------------------------------------------------------------

_SETTINGS_PATH = Path(__file__).resolve().parent.parent / "logs" / "settings.json"
# 止损 ATR 倍数允许范围（越界 400）
_STOP_ATR_MULT_MIN, _STOP_ATR_MULT_MAX = 0.2, 5.0


def _load_settings() -> dict:
    """启动加载 settings.json；缺文件/损坏返回空 dict（各键回退 config 默认）。"""
    try:
        payload = json.loads(_SETTINGS_PATH.read_text(encoding="utf-8"))
        if isinstance(payload, dict):
            return payload
    except FileNotFoundError:
        pass
    except (OSError, ValueError, TypeError) as e:
        logger.warning("settings.json 加载失败: %s", e)
    return {}


def _save_settings() -> None:
    """原子写落盘（tmp + os.replace，与 clients.json 落盘模式一致）。"""
    try:
        _SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = _SETTINGS_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(_settings, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        os.replace(tmp, _SETTINGS_PATH)
    except OSError:
        logger.exception("settings.json 落盘失败")


_settings: dict = _load_settings()


def _stop_atr_mult() -> float:
    """当前全局止损 ATR 倍数（settings.json 优先，缺省回退 config.STOP_ATR_MULT）。"""
    try:
        v = float(_settings.get("stop_atr_mult"))
        if _STOP_ATR_MULT_MIN <= v <= _STOP_ATR_MULT_MAX:
            return v
    except (TypeError, ValueError):
        pass
    return config.STOP_ATR_MULT


def _screen_premarket_filter() -> bool:
    """选股盘前区间过滤开关（默认关：缺键/假值都不过滤）。"""
    return bool(_settings.get("screen_premarket_filter"))


def _fvg_premarket_filter() -> bool:
    """FVG 监控盘前区间过滤开关（默认关）。"""
    return bool(_settings.get("fvg_premarket_filter"))


def _attack_zone(data: dict):
    """快照里的进攻区域盘前区间（attack_c 确认值 pmh/pml）。
    无数据（空 dict / null / 区间非法）返回 (None, None)——过滤时一律保留。"""
    atk = data.get("attack_c") or {}
    pmh, pml = atk.get("pmh"), atk.get("pml")
    try:
        if pmh is not None and pml is not None and float(pmh) > float(pml) > 0:
            return float(pmh), float(pml)
    except (TypeError, ValueError):
        pass
    return None, None


def _premarket_pass(side: str, close, pmh, pml) -> bool:
    """盘前区间过滤判定：多头要求现价 > pmh，空头要求现价 < pml；
    区间或现价缺失一律保留（无法判断时不误杀）。"""
    if pmh is None or pml is None or close is None:
        return True
    if side == "long":
        return close > pmh
    return close < pml


def _token_valid(info: dict) -> bool:
    """token 当前是否有效：已审批且未超过有效期（自审批时刻起算）。"""
    if not info or not info.get("approved"):
        return False
    return (time.time() - info.get("approved_at", 0)) < _CLIENT_TOKEN_TTL_SECONDS


def _is_local(request: Request) -> bool:
    """是否本机请求（remote_addr 为回环地址）= master，免确认。"""
    client = request.client
    return bool(client) and client.host in _LOCAL_ADDRS


async def require_trader(request: Request) -> str:
    """交易员闸门（写操作统一依赖注入）：
    本机放行（trader="master"）；非本机 token 有效（已审批且未过期）→ 放行
    （trader=名称）；已过期 → 403「登录已过期」；否则 403「等待 master 审批」。"""
    if _is_local(request):
        return "master"
    token = request.headers.get("X-Trader-Token", "")
    info = _clients.get("tokens", {}).get(token)
    if _token_valid(info):
        return info["name"]
    if info and info.get("approved"):
        raise HTTPException(403, "登录已过期（token 超过有效期），请重新登记并经 master 审批")
    raise HTTPException(403, "客户端未确认，等待 master 审批")


def _clients_snapshot() -> dict:
    """快照给前端的客户端列表（只给 token 前 6 位，不泄露完整 token；
    approved 列只含未过期的）。"""
    pending, approved = [], []
    for token, info in _clients.get("tokens", {}).items():
        row = {"name": info.get("name", ""), "token_prefix": token[:6]}
        if _token_valid(info):
            approved.append(row)
        elif not info.get("approved"):
            row["created_at"] = info.get("created_at", 0)
            pending.append(row)
        # 已过期的 approved token 两列都不显示（自然淘汰）
    pending.sort(key=lambda r: r.get("created_at", 0))
    approved.sort(key=lambda r: r["name"])
    return {"pending": pending, "approved": approved}


# ---------------------------------------------------------------------------
# 跟单系统（master 侧）：服务器配置 + 进程监管 + 快照聚合
# ---------------------------------------------------------------------------
# master 拥有 logs/copy_servers.json；每台 enabled 服务器由 CopySupervisor
# 常驻一个 `python3 copy_trader.py --name {name}` 子进程（EXECUTION_ENABLED=0
# 时追加 --dry-run）。子进程每 2s 写 logs/copy-{name}-status.json（状态快照），
# 并轮询 logs/copy-{name}-cmd.json（retry/dismiss/unhalt 人工指令）。
# clientId 本机分配表：11=autotrade 主，12=临时测试，2/3/46=nautilus，
# 跟单服务器从 13 起自动分配。

_COPY_SERVERS_PATH = Path(__file__).resolve().parent.parent / "logs" / "copy_servers.json"
_COPY_TRADER_PY = Path(__file__).resolve().parent.parent / "copy_trader.py"
# 服务器名：1-10 字符，字母/数字/中文/下划线/连字符
_COPY_SERVER_NAME_RE = re.compile(r"^[\w\-]{1,10}$")
# 状态文件原样合入列表/快照的字段（copy_trader.py 每 2s 写一次）
_COPY_STATUS_FIELDS = ("connected", "equity", "synced", "failed", "halted",
                       "drawdown_pct", "last_sync_ts", "last_error",
                       "orders", "failures")
# 跟单 client_id 自动分配下限与本机保留号段（见上方分配表注释）
_COPY_CLIENT_ID_MIN = 13
_COPY_CLIENT_ID_RESERVED = (2, 3, 12, 46)


def _load_copy_servers() -> list:
    """加载 logs/copy_servers.json（master 拥有）；缺失/损坏返回空列表。"""
    try:
        payload = json.loads(_COPY_SERVERS_PATH.read_text(encoding="utf-8"))
        servers = payload.get("servers")
        if isinstance(servers, list):
            return servers
    except FileNotFoundError:
        pass
    except (OSError, ValueError, TypeError) as e:
        logger.warning("copy_servers.json 加载失败: %s", e)
    return []


def _save_copy_servers(servers: list) -> None:
    """原子写落盘（tmp + os.replace，与 clients.json 落盘模式一致）。"""
    try:
        _COPY_SERVERS_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = _COPY_SERVERS_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"servers": servers}, ensure_ascii=False,
                                  indent=2), encoding="utf-8")
        os.replace(tmp, _COPY_SERVERS_PATH)
    except OSError:
        logger.exception("copy_servers.json 落盘失败")


def _copy_status_path(name: str) -> Path:
    return _COPY_SERVERS_PATH.parent / f"copy-{name}-status.json"


def _copy_cmd_path(name: str) -> Path:
    return _COPY_SERVERS_PATH.parent / f"copy-{name}-cmd.json"


def _copy_status(name: str) -> Optional[dict]:
    """读取跟单进程状态文件 logs/copy-{name}-status.json；缺失/损坏返回 None。"""
    try:
        payload = json.loads(_copy_status_path(name).read_text(encoding="utf-8"))
        if isinstance(payload, dict):
            return payload
    except FileNotFoundError:
        pass
    except (OSError, ValueError, TypeError):
        pass
    return None


def _alloc_copy_client_id(servers: list) -> int:
    """自动分配 client_id：max(13, 现有最大+1)，避开主程序与本机保留号段。"""
    used = {int(s["client_id"]) for s in servers if s.get("client_id")}
    cid = max([_COPY_CLIENT_ID_MIN] + [c + 1 for c in used])
    reserved = set(_COPY_CLIENT_ID_RESERVED) | {config.IBKR_CLIENT_ID}
    while cid in used or cid in reserved:
        cid += 1
    return cid


def _validate_copy_server(cfg: dict, others: list) -> Optional[str]:
    """跟单服务器配置校验；返回错误信息（None=通过）。others=除自身外的服务器。"""
    if cfg.get("size_mode") not in ("risk", "multiplier"):
        return "size_mode 仅支持 risk / multiplier"
    if cfg.get("direction") not in ("both", "long", "short"):
        return "direction 仅支持 both / long / short"
    if not (1 <= int(cfg.get("port") or 0) <= 65535):
        return "port 非法（1-65535）"
    if float(cfg.get("risk_usd") or 0) <= 0:
        return "risk_usd 必须为正"
    if float(cfg.get("size_multiplier") or 0) <= 0:
        return "size_multiplier 必须为正"
    if float(cfg.get("max_drawdown_pct") or 0) <= 0:
        return "max_drawdown_pct 必须为正"
    if not str(cfg.get("master_ws") or "").startswith(("ws://", "wss://")):
        return "master_ws 必须是 ws:// 或 wss:// 地址"
    cid = int(cfg.get("client_id") or 0)
    if cid <= 0:
        return "client_id 必须为正"
    if cid == config.IBKR_CLIENT_ID:
        return f"client_id 与主程序冲突（{cid}）"
    if any(int(o.get("client_id") or 0) == cid for o in others):
        return f"client_id 已被其他跟单服务器占用（{cid}）"
    return None


class CopySupervisor:
    """跟单进程监管（参照 RealtimeCollector 的 subprocess 模式）：
    每台 enabled 服务器常驻一个 copy_trader.py 子进程；崩溃自动重启
    （指数退避，一小时内最多 10 次，超限放弃并告警）；CRUD 配置变更时
    reload 增量比对——新增/参数变化的（重）启动，停用/删除的 terminate
    （先 SIGTERM 等 5s 再 SIGKILL）。"""

    def __init__(self) -> None:
        self._procs: dict = {}            # name -> asyncio 子进程
        self._configs: dict = {}          # name -> 最近一次生效的配置（reload 比对用）
        self._restarts: dict = {}         # name -> 最近重启时间戳（限频用）
        self._pending_restart: dict = {}  # name -> 退避到点的重启时刻
        self._stopping: set = set()       # 主动停止中的 name（watchdog 不重启）
        self._watchdog_task: Optional[asyncio.Task] = None
        self._running = False

    async def start(self, servers: list) -> None:
        """启动监管：copy_trader.py 缺失只告警不阻塞（另一 agent 可能在写）。"""
        self._configs = {s["name"]: s for s in servers if s.get("name")}
        if not _COPY_TRADER_PY.exists():
            logger.warning("copy_trader.py 不存在（%s），跟单监管未启动",
                           _COPY_TRADER_PY)
            return
        self._running = True
        for s in servers:
            if s.get("enabled"):
                await self._spawn(s)
        self._watchdog_task = asyncio.create_task(self._watchdog_loop())

    async def stop_all(self) -> None:
        """停止监管并 terminate 全部跟单进程。"""
        self._running = False
        if self._watchdog_task:
            self._watchdog_task.cancel()
            self._watchdog_task = None
        for name in list(self._procs) + list(self._pending_restart):
            await self.terminate(name)

    async def reload(self, servers: list) -> None:
        """配置变更增量比对：enabled 新增/参数变化 → （重）启动；
        停用/删除 → terminate。监管未启动时只更新配置缓存。"""
        all_new = {s["name"]: s for s in servers if s.get("name")}
        if self._running:
            enabled = {n: s for n, s in all_new.items() if s.get("enabled")}
            # 停用/删除 → terminate
            for name in list(self._procs) + list(self._pending_restart):
                if name not in enabled:
                    await self.terminate(name)
            # 新增/参数变化 → （重）启动（无变化且存活则跳过）
            for name, cfg in enabled.items():
                if self.process_alive(name) and self._configs.get(name) == cfg:
                    continue
                await self.terminate(name)   # 无进程时为空操作
                await self._spawn(cfg)
        self._configs = all_new

    async def _spawn(self, cfg: dict) -> None:
        """启动一台服务器的跟单子进程（EXECUTION_ENABLED=0 时加 --dry-run）。"""
        name = cfg["name"]
        args = ["python3", str(_COPY_TRADER_PY), "--name", name]
        if not config.EXECUTION_ENABLED:
            args.append("--dry-run")
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            cwd=str(_COPY_TRADER_PY.parent),
        )
        self._procs[name] = proc
        logger.info("跟单进程启动: %s (pid=%s, client_id=%s%s)", name,
                    proc.pid, cfg.get("client_id"),
                    ", dry-run" if not config.EXECUTION_ENABLED else "")

    async def terminate(self, name: str) -> None:
        """停止一台跟单进程：先 SIGTERM 等 5s，超时再 SIGKILL。"""
        self._stopping.add(name)
        self._pending_restart.pop(name, None)
        proc = self._procs.pop(name, None)
        try:
            if proc is not None and proc.returncode is None:
                proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=5)
                except asyncio.TimeoutError:
                    proc.kill()
                    await proc.wait()
                logger.info("跟单进程已停止: %s", name)
        except ProcessLookupError:
            pass
        finally:
            self._stopping.discard(name)

    def process_alive(self, name: str) -> bool:
        """该服务器的跟单进程是否存活。"""
        proc = self._procs.get(name)
        return bool(proc is not None and proc.returncode is None)

    async def _watchdog_loop(self) -> None:
        """守护：每 2s 检查进程存活，崩溃按指数退避重启（10 次/小时封顶）。"""
        while True:
            await asyncio.sleep(2.0)
            try:
                await self._check_once()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.exception("跟单监管循环异常: %s", e)

    async def _check_once(self) -> None:
        """单拍检查：收割已退出进程并按退避计划重启。"""
        now = time.time()
        for name, proc in list(self._procs.items()):
            if proc.returncode is None or name in self._stopping:
                continue
            # 进程意外退出
            self._procs.pop(name, None)
            record_event("copy_process_died", name=name,
                         returncode=proc.returncode)
            logger.warning("跟单进程 %s 意外退出 (rc=%s)", name, proc.returncode)
            cfg = self._configs.get(name)
            if not cfg or not cfg.get("enabled"):
                continue
            # 限频：一小时内最多 10 次重启，超限放弃（人工排查后再 enable）
            stamps = [t for t in self._restarts.get(name, []) if now - t < 3600]
            self._restarts[name] = stamps
            if len(stamps) >= 10:
                logger.error("跟单进程 %s 一小时内重启已达 10 次，停止自动重启",
                             name)
                record_event("copy_process_died", name=name, gave_up=True)
                continue
            # 指数退避：1/2/4/8... 封顶 60s
            self._pending_restart[name] = now + min(2 ** len(stamps), 60)
        for name, at in list(self._pending_restart.items()):
            if now < at or name in self._stopping:
                continue
            self._pending_restart.pop(name, None)
            cfg = self._configs.get(name)
            if not cfg or not cfg.get("enabled") or not self._running:
                continue
            self._restarts.setdefault(name, []).append(now)
            try:
                await self._spawn(cfg)
            except Exception as e:
                logger.warning("跟单进程 %s 重启失败（5s 后重试）: %s", name, e)
                self._pending_restart[name] = now + 5


# 跟单进程监管实例（startup 启动，shutdown 全部 terminate）
copy_supervisor: CopySupervisor = CopySupervisor()


def _copy_server_row(cfg: dict) -> dict:
    """单台服务器合并行：配置字段 + 状态文件（无文件时状态字段为 null）
    + process_alive（监管进程存活）。"""
    status = _copy_status(cfg.get("name", "")) or {}
    row = dict(cfg)
    for k in _COPY_STATUS_FIELDS:
        row[k] = status.get(k)
    # 状态文件的 master_ws(bool) 与配置的 master_ws(URL) 撞名：别名导出连接状态
    row["master_ws_connected"] = status.get("master_ws")
    row["process_alive"] = copy_supervisor.process_alive(cfg.get("name", ""))
    return row


def _copy_snapshot() -> dict:
    """快照 copy 段：每台服务器的 配置 + 状态文件原样合入 + 进程存活。"""
    return {"servers": [_copy_server_row(c) for c in _load_copy_servers()]}


class CopyServerCreateRequest(BaseModel):
    """新增跟单服务器（client_id=0 = 自动分配，13 起）。"""
    name: str = Field(..., description="服务器名（1-10 字符，唯一）")
    master_ws: str = "ws://127.0.0.1:8000/ws"
    host: str = "127.0.0.1"
    port: int = 7497
    client_id: int = 0                # 0 = 自动分配
    account_id: str = ""
    fa_group: str = ""
    enabled: bool = False             # 新增默认停用，确认参数后再 enable
    size_mode: str = "risk"           # risk=按风险金额 / multiplier=按倍数
    risk_usd: float = 300
    size_multiplier: float = 0.1
    max_drawdown_pct: float = 3.0
    direction: str = "both"           # both / long / short
    copy_existing_on_start: bool = False


class CopyServerUpdateRequest(BaseModel):
    """改参数：只更新显式传入的字段（None = 不动）；name/enabled 走专用端点。"""
    master_ws: Optional[str] = None
    host: Optional[str] = None
    port: Optional[int] = None
    client_id: Optional[int] = None
    account_id: Optional[str] = None
    fa_group: Optional[str] = None
    size_mode: Optional[str] = None
    risk_usd: Optional[float] = None
    size_multiplier: Optional[float] = None
    max_drawdown_pct: Optional[float] = None
    direction: Optional[str] = None
    copy_existing_on_start: Optional[bool] = None


class CopyCmdRequest(BaseModel):
    """人工指令（合并进 logs/copy-{name}-cmd.json，不覆盖已有指令）。"""
    retry: List[str] = Field(default_factory=list)    # 重试的失败单号
    dismiss: List[str] = Field(default_factory=list)  # 忽略的失败单号
    unhalt: bool = False                              # 解除熔断


# ---------------------------------------------------------------------------
# 依赖组装
# ---------------------------------------------------------------------------

gateway: IBKRGateway = IBKRGateway.from_config()
risk_gate: RiskGate = RiskGate(RiskConfig(
    per_trade_pct=config.RISK_PER_TRADE_PCT,
    per_trade_usd=config.RISK_PER_TRADE_USD,
    max_order_qty=config.RISK_MAX_ORDER_QTY,
    max_open_positions=config.RISK_MAX_OPEN_POSITIONS,
    max_total_risk_usd=config.RISK_MAX_TOTAL_RISK_USD,
    daily_loss_limit_usd=config.RISK_DAILY_LOSS_LIMIT_USD,
))
order_mgr: OrderManager = OrderManager(gateway, risk_gate)

app = FastAPI(title="autotrade 下单控制台", version="0.1.0")

# WebSocket 连接集合
_ws_clients: Set[WebSocket] = set()
_broadcast_task: Optional[asyncio.Task] = None
_eod_task: Optional[asyncio.Task] = None
_reconcile_task: Optional[asyncio.Task] = None
_midpoint_reprice_task: Optional[asyncio.Task] = None
_retest_reprice_task: Optional[asyncio.Task] = None
_external_cleanup_task: Optional[asyncio.Task] = None
_midpoint_escalate_attempted: set = set()  # (lot_id, bar_time) 突破升级每 bar 只尝试一次

# TV 实时订阅采集器（5M K线 + trend_state 单指标）
collector: RealtimeCollector = RealtimeCollector(config.WATCHLIST)
order_mgr.market_health = collector.data_health

# 净值/日盈亏缓存（避免每秒查 IBKR）
_equity_cache = {"equity": None, "daily_pnl": None, "ts": 0.0}

# 持仓扫描状态（持仓方向与 trend_state 方向冲突 → 自动平仓/平仓提醒）
_scan_alerts: dict = {}   # ticker -> 平仓提醒（自动平仓失败或开关关闭时）
_scan_task: Optional[asyncio.Task] = None

# 止损单守卫状态（持仓缺少 STP 保护单 → 每分钟提醒）
_stop_alerts: dict = {}   # ticker -> 裸仓提醒
_stop_guard_task: Optional[asyncio.Task] = None

# 未快速突破提醒（开仓超过 SLOW_BREAK_SECONDS 仍未保本 → 语音提醒减仓）
_slow_break_alerts: dict = {}   # lot_id -> 提醒
_slow_break_task: Optional[asyncio.Task] = None

# 自动 2R 保本（浮盈 ≥ 2R 自动止损移成本）
_auto_breakeven_task: Optional[asyncio.Task] = None

# 趋势反向自动平仓记录（前端语音/通知用，ticker -> 最近一次自动平仓）
_auto_close_notices: dict = {}

# 持仓反向 FVG 守卫（5M 收盘确认口径）：反向缺口确认时，浮亏 lot 立即市价
# 平仓，浮盈 lot 立即保本 + 语音/界面提醒平仓锁利。只管本工具 ACTIVE lot。
_fvg_reverse_alerts: dict = {}   # lot_id -> 提醒（profit_lock / close_failed）
_fvg_reverse_notices: dict = {}  # ticker -> 最近一次反向 FVG 自动平仓（前端语音用）
_fvg_reverse_acted: dict = {}    # f"{lot_id}:{int(bar_time)}" -> ts（每 lot 每 bar 一次）
_fvg_reverse_task: Optional[asyncio.Task] = None

# 影子回测跟踪器（回踩事件 → 逐 bar 回测推演 → 信号有效性统计）
shadow_tracker: ShadowTracker = ShadowTracker()
_shadow_task: Optional[asyncio.Task] = None
_shadow_proposal_recorded: set = set()   # 已建档的策略候选 proposal_id（当天去重）

# EMA9 金字塔全自动策略（2R 解锁 + 3-2-1 风险预算 + 整组仓位管理）
auto_engine: PyramidEngine = PyramidEngine(
    LiveBroker(order_mgr),
    emit=lambda t, **d: record_event(f"auto_{t}" if not t.startswith("auto_") else t, **d),
    max_instances=config.AUTO_MAX_INSTANCES)
_auto_enabled: bool = False        # /api/auto/start 开启，stop 关闭
_auto_task: Optional[asyncio.Task] = None
_auto_last_bar: dict = {}          # symbol -> 已处理的 bar_c.time
_auto_strategy_name: str = ""

# FVG 事件环形缓冲（保留最近 4 小时；指标只导出最后一个 FVG 边沿，
# 事件当根必须在服务端固化快照，否则同标的再出新缺口旧边沿即被覆盖）
FVG_EVENT_TTL_SECONDS = 4 * 3600
_fvg_events: "collections.deque" = collections.deque()
# FVG 首次回踩事件环形缓冲（同 4h TTL）：一个 FVG 终身只报一次回踩，
# 触发即把边沿快照固化进此缓冲（避免旧事件被淘汰后回踩记录丢失）
_fvg_retests: "collections.deque" = collections.deque()
_fvg_last_bar: dict = {}           # symbol -> 已扫描的最大 bar_c.time
_fvg_pending: dict = {}            # symbol -> (bar_time, 剩余重试拍数)：daemon 的
                                   # chart 与 FVG study 回调跨 1s 推送边界时 fvg_c
                                   # 还是旧 bar 的值，挂起等它追上（防事件永久丢失）
_fvg_warnings: "collections.deque" = collections.deque()  # FVG 预警（形成中，同 4h TTL）
_fvg_warned: dict = {}             # warn_key -> 记录时间戳：每 bar 每方向只预警一次
_fvg_task: Optional[asyncio.Task] = None

# 盘前突破+FVG确认 流式信号（engine.breakout_tracker，口径=research_breakout.py）
breakout_tracker: BreakoutTracker = BreakoutTracker()
_breakout_last_bar: dict = {}      # symbol -> 已喂入的 bar_c.time
_breakout_pending: dict = {}       # symbol -> (bar_time, bar, 剩余重试拍数)：
                                   # 同 FVG 固化，fvg_c 未追上 bar_c 时挂起等齐
_breakout_signals: "collections.deque" = collections.deque()  # 当日已出信号（4h TTL）
_breakout_injected: dict = {}    # symbol -> 已注入盘前区间的 ET 交易日（每天一次）
_breakout_task: Optional[asyncio.Task] = None
_naked_position_task: Optional[asyncio.Task] = None   # 裸仓巡检（STP 缺失自动补挂）
_health_watch_task: Optional[asyncio.Task] = None     # 健康巡检（TWS 断线/数据陈旧）
_tv_check_task: Optional[asyncio.Task] = None         # 启动 TV 数据源检查（一次性）
_report_task: Optional[asyncio.Task] = None           # 每日复盘（交易日 16:20 ET）
_precheck_task: Optional[asyncio.Task] = None         # 盘前检查单（交易日 09:25 ET）
_startup_task: Optional[asyncio.Task] = None          # 启动加载页 + 浏览器/全链路就绪
_startup_state: dict = {
    "started_at": time.time(), "core_ready": False, "browser_ready": False,
    "browser_error": "", "ready": False,
}
_system_stopping = False


# ---------------------------------------------------------------------------
# 快照
# ---------------------------------------------------------------------------

async def get_status_cached() -> dict:
    """净值/日盈亏缓存（每 10s 刷新一次）。"""
    global _equity_cache
    if time.time() - _equity_cache["ts"] > 10.0:
        _equity_cache["equity"] = await gateway.net_liquidation()
        _equity_cache["daily_pnl"] = await gateway.daily_pnl()
        _equity_cache["ts"] = time.time()
    return _equity_cache


def _is_trading_hours() -> bool:
    """是否在美股常规交易时段（9:30-16:00 ET，周一~周五）。"""
    from zoneinfo import ZoneInfo
    import datetime as _dt
    now = _dt.datetime.now(ZoneInfo("America/New_York"))
    if now.weekday() >= 5:            # 周六/周日
        return False
    minutes = now.hour * 60 + now.minute
    return 9 * 60 + 30 <= minutes < 16 * 60


def _ts_c(data: dict) -> dict:
    """trend_state 确认值（上一根已收盘 5M bar）；缺失时退化实时值。"""
    return data.get("ts_c") or data.get("ts") or {}


def _smart_stop_dist(data: dict, side: str, ref_px: float, atr: float) -> float:
    """智能止损距离（优先读 smart_stop 指标 plot，所见即所得；缺失回退本地计算）。
    指标口径（smart_stop.pine）：距离 = close − Smart_Stop_Long（多）/
    Smart_Stop_Short − close（空），内部已是「3 根极值 + ATR 钳制」。
    本地回退：同样的逻辑止损点（最近三根 K 线极值，含当前 bar）+
    ATR 钳制 [1×ATR, 上限×ATR]（上限 = 全局 stop_atr_mult）。"""
    dist = _smart_stop_dist_from_indicator(data, side)
    if dist is not None:
        return dist
    lows, highs = [], []
    for key in ("bar", "bar_c", "bar_c2"):
        b = data.get(key) or {}
        lo, hi = b.get("low"), b.get("high")
        if lo is not None and hi is not None:
            lows.append(lo)
            highs.append(hi)
    if side == "long":
        logical = ref_px - min(lows) if lows else 0.0
    else:
        logical = max(highs) - ref_px if highs else 0.0
    lo, hi = atr, max(_stop_atr_mult(), 1.0) * atr
    return min(max(logical, lo), hi)


def _smart_stop_dist_from_indicator(data: dict, side: str):
    """读 smart_stop 指标的止损距离（ts_c 确认值，对应 bar_c 的 close）。
    缺失/非法返回 None（调用方回退本地计算）。"""
    ts = _ts_c(data)
    close = (data.get("bar_c") or {}).get("close")
    ssl = ts.get("smart_stop_long") if side == "long" else ts.get("smart_stop_short")
    if ssl is None or close is None:
        return None
    dist = (close - ssl) if side == "long" else (ssl - close)
    return dist if dist > 0 else None


def _midpoint_prices(data: dict, side: str):
    """由上一根已收盘 5M bar 计算中点限价、ATR 止损与突破触发价。

    突破触发价：多单 = bar 高点 + 0.01，空单 = bar 低点 - 0.01，
    供 OCA 突破腿（STP stop-market）使用；止损 = 中点限价 ∓ 智能距离
    （最近三根 K 线极值逻辑止损，钳制在 1×ATR ~ 上限×ATR，上限=全局倍数）。"""
    bar = data.get("bar_c") or {}
    ts = _ts_c(data)
    high, low = bar.get("high"), bar.get("low")
    atr = ts.get("atr")
    if high is None or low is None or not bar.get("time"):
        raise ValueError("缺少已收盘 5M K 线，无法计算中点限价")
    if high < low:
        raise ValueError("5M K 线 high/low 数据非法")
    if not atr or atr <= 0:
        raise ValueError("ATR 数据缺失，无法计算中点单止损")
    limit = round((high + low) / 2, 2)
    stop = round(limit + (-1 if side == "long" else 1)
                 * _smart_stop_dist(data, side, limit, atr), 2)
    breakout = round(high + 0.01 if side == "long" else low - 0.01, 2)
    if limit <= 0 or stop <= 0 or breakout <= 0:
        raise ValueError("中点限价/止损/突破价必须为正")
    if side == "long" and stop >= limit:
        raise ValueError("多仓止损必须低于中点限价")
    if side == "short" and stop <= limit:
        raise ValueError("空仓止损必须高于中点限价")
    return limit, stop, breakout, bar, float(atr)


def _retest_prices(data: dict, side: str):
    """由上上根已收盘 5M bar（bar_c2，periods[2]）计算回踩限价与逻辑止损。

    多头：限价 = bar_c2.high（等回踩成交，不回踩形成 gap 不成交），
    止损 = bar_c2.low；空头镜像（限价 = bar_c2.low，止损 = bar_c2.high）。
    止损取 K 线极值，不依赖 ATR（与中点单的 ∓N×ATR 不同，N = 全局止损倍数）。"""
    bar = data.get("bar_c2") or {}
    high, low = bar.get("high"), bar.get("low")
    if high is None or low is None or not bar.get("time"):
        raise ValueError("缺少上上根已收盘 5M K 线（bar_c2），无法计算回踩限价")
    if high < low:
        raise ValueError("5M K 线 high/low 数据非法")
    limit = round(high if side == "long" else low, 2)
    stop = round(low if side == "long" else high, 2)
    if limit <= 0 or stop <= 0:
        raise ValueError("回踩限价/止损必须为正")
    if side == "long" and stop >= limit:
        raise ValueError("多仓止损必须低于回踩限价")
    if side == "short" and stop <= limit:
        raise ValueError("空仓止损必须高于回踩限价")
    return limit, stop, bar


def _fvg_chase_prices(data: dict, side: str, symbol: str):
    """FVG 追价定价（服务端，不信浏览器价）：
    以该 symbol + 方向在 4h 缓冲内的最新 FVG 事件边沿为锚（多=top / 空=bottom），
    现价距边沿 ≤ FVG_CHASE_MAX_ATR_RATIO × ATR 才允许追价；
    限价 = 实时 close × (1 ± ENTRY_LIMIT_PREMIUM_PCT/100)（marketable 限价，
    与系统不用裸 MARKET 的惯例一致），止损 = 现价 ∓ N×ATR（N = 全局止损倍数）。"""
    bar = data.get("bar") or {}
    ts = _ts_c(data)
    close = bar.get("close")
    atr = ts.get("atr")
    if close is None or close <= 0:
        raise ValueError("缺少实时价格，无法追价")
    if not atr or atr <= 0:
        raise ValueError("ATR 数据缺失，无法计算追价止损")
    # 4h 缓冲内该 symbol + 方向的最新 FVG 事件（deque 按 bar_time 递增，
    # 反向取第一个匹配即最新）
    latest = None
    for ev in reversed(_fvg_events):
        if ev["symbol"] == symbol and ev["side"] == side:
            latest = ev
            break
    if latest is None:
        raise ValueError("无可用 FVG 事件，不可追价")
    edge = latest["top"] if side == "long" else latest["bottom"]
    dist_ratio = abs(close - edge) / atr
    max_ratio = config.FVG_CHASE_MAX_ATR_RATIO
    if dist_ratio > max_ratio:
        raise ValueError(f"价格已偏离 FVG 边沿过远"
                         f"（{dist_ratio:.2f}×ATR > {max_ratio}×ATR），不可追价")
    premium = config.ENTRY_LIMIT_PREMIUM_PCT / 100
    limit = round(close * (1 + premium if side == "long" else 1 - premium), 2)
    # 止损锚定实时价（≈预期成交价），不锚定含溢价的限价：
    # 溢价 0.5%×close > ATR 时按限价锚定会把止损挂到现价错误一侧，立即触发；
    # 距离 = 智能止损（最近三根 K 线极值逻辑点，钳制 1×ATR ~ 上限×ATR）
    stop = round(close + (-1 if side == "long" else 1)
                 * _smart_stop_dist(data, side, close, atr), 2)
    if limit <= 0 or stop <= 0:
        raise ValueError("追价限价/止损必须为正")
    return limit, stop


def _market_prices(data: dict, side: str):
    """市价单定价（服务端，不信浏览器价）：marketable 限价 =
    实时 close × (1 ± ENTRY_LIMIT_PREMIUM_PCT/100)（不用裸 MARKET：延迟行情
    会被券商拒单 error 10349），止损 = 现价 ∓ N×ATR（ts_c 确认值，
    N = 全局止损倍数 _stop_atr_mult()，默认 1.0）。"""
    bar = data.get("bar") or {}
    ts = _ts_c(data)
    close = bar.get("close")
    atr = ts.get("atr")
    if close is None or close <= 0:
        raise ValueError("缺少实时价格，无法下市价单")
    if not atr or atr <= 0:
        raise ValueError("ATR 数据缺失，无法计算市价单止损")
    premium = config.ENTRY_LIMIT_PREMIUM_PCT / 100
    limit = round(close * (1 + premium if side == "long" else 1 - premium), 2)
    # 止损锚定实时价（≈预期成交价），不锚定含溢价的限价（原因同追价：
    # GOOGL/NOW 两单曾因按限价锚定，止损落在现价错误一侧秒触发）；
    # 距离 = 智能止损（最近三根 K 线极值逻辑点，钳制 1×ATR ~ 上限×ATR）
    stop = round(close + (-1 if side == "long" else 1)
                 * _smart_stop_dist(data, side, close, atr), 2)
    if limit <= 0 or stop <= 0:
        raise ValueError("市价单限价/止损必须为正")
    return limit, stop


def _check_trend_filter(symbol: str, side: str,
                        force: bool = False, reason: str = "") -> None:
    """SuperTrend 方向闸门（下单拦截）：
    做多需 ts_c.direction == 1（SuperTrend 多头），做空需 == -1；
    震荡（trend_state=0）但 ST 方向正确也放行。side 必须与 ST 方向同向。
    force=执意开仓放行（需填原因，记 WARN 日志）。"""
    if force:
        logger.warning("执意开仓放行（force）: %s %s 原因: %s",
                       symbol, side, reason)
        return
    snap = (collector.latest() or {}).get(symbol, {})
    ts = _ts_c(snap)
    direction = ts.get("direction")
    if ts.get("atr") is None:
        raise HTTPException(400, f"{symbol} ATR 数据缺失，"
                                 "请检查图表上的 trend_state 指标（Atr Value plot）")
    if side == "long" and direction != 1:
        raise HTTPException(400, {"code": "not_long_candidate",
                                  "msg": f"{symbol} 不在多头列表"
                                         f"（ST方向={direction}）"})
    if side == "short" and direction != -1:
        raise HTTPException(400, {"code": "not_long_candidate",
                                  "msg": f"{symbol} 不在空头列表"
                                         f"（ST方向={direction}）"})


async def _close_on_flip(ticker: str, sym: str, pos: float,
                         close_px: Optional[float]) -> bool:
    """趋势反向时只处理本工具 lot；外部持仓由独立清理循环处理。"""
    ok = True
    for lot in order_mgr.list_lots(active_only=True, symbol=sym):
        if lot["state"] == "PENDING":
            lok, err = await order_mgr.cancel_lot(lot["lot_id"])
        elif lot["state"] == "ACTIVE":
            lok, err = await order_mgr.close_lot(
                lot["lot_id"], ref_px=close_px, exit_reason="st_flip")
        else:
            continue   # CLOSING 等由各自 close watcher 完成，不重复发单
        if not lok:
            ok = False
            logger.warning("趋势反向平仓 %s lot %s 失败: %s",
                           ticker, lot["lot_id"], err)
    return ok


_external_cleanup_inflight: dict = {}
_external_alerts: list = []          # 告警模式下的外部订单/持仓检测快照（每秒快照广播）
_external_alert_last: dict = {}      # (kind, ticker) -> 上次事件时间（60s 节流）


def _legacy_tool_order_ids() -> set:
    ids = set()
    for lot in order_mgr.list_lots():
        values = list(lot.get("owned_order_ids") or [])
        values.extend(lot.get(key) for key in (
            "entry_order_id", "trail_order_id", "close_order_id", "close_permId"))
        for value in values:
            if value is not None:
                try:
                    ids.add(int(value))
                except (TypeError, ValueError):
                    continue
    return ids


def _external_symbol(ticker: str) -> Optional[str]:
    for symbol in (collector.latest() or {}):
        if symbol.split(":")[-1].upper() == ticker.upper():
            return symbol
    return None


def _external_account_ok(order, accounts: set) -> bool:
    account = getattr(order, "account", "") or ""
    fa_group = getattr(order, "faGroup", "") or ""
    if fa_group == gateway.fa_group:
        return True
    return bool(account and account in accounts)


async def _wait_trade_terminal(trade, timeout: float = 8.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if trade.isDone():
            return True
        await asyncio.sleep(0.25)
    return trade.isDone()


async def _external_cleanup_once() -> dict:
    """外部（号段外/非工具白名单）订单与持仓的处理。
    EXTERNAL_ORDER_CLEANUP_ENABLED=1：自动撤挂单 + 市价平残余持仓（原行为）；
    =0（默认）：只检测不动作——写 `_external_alerts` 快照 + 节流事件，
    前端弹框/语音提醒人工处理（IBKR 手动单绝不自动市价平仓）；
    快照不完整时禁止任何写操作。"""
    stats = {"cancelled": 0, "closed": 0, "pending": 0,
             "failed": 0, "skipped": 0}
    if not (gateway.enabled and gateway._connected):
        return stats
    positions, trades = await gateway.positions(), await gateway.trade_map()
    if positions is None or trades is None:
        logger.warning("外部清理跳过：IBKR 订单或持仓快照不完整")
        stats["failed"] += 1
        return stats
    accounts = set(await gateway.fa_accounts())
    legacy_ids = _legacy_tool_order_ids()

    if not config.EXTERNAL_ORDER_CLEANUP_ENABLED:
        # 告警模式：检测号段外挂单 + 外部残余持仓，只提醒不动单
        global _external_alerts
        alerts = []
        for trade in list(trades.values()):
            if trade.isDone():
                continue
            order = trade.order
            order_id = int(order.orderId)
            if gateway.is_tool_order_id(order_id) or order_id in legacy_ids:
                continue
            if not _external_account_ok(order, accounts):
                continue
            alerts.append({"kind": "order", "ticker": trade.contract.symbol,
                           "side": order.action, "qty": float(order.totalQuantity or 0),
                           "order_type": order.orderType, "ts": time.time()})
        tool_qty = {g["symbol"]: g["net_qty"] for g in order_mgr.aggregate()}
        for ticker in sorted(set(positions) | set(tool_qty)):
            residual = positions.get(ticker, 0) - tool_qty.get(ticker, 0)
            if abs(residual) < 1:
                continue
            alerts.append({"kind": "position", "ticker": ticker,
                           "side": "long" if residual > 0 else "short",
                           "qty": abs(int(residual)), "ts": time.time()})
        _external_alerts = alerts
        # 事件节流：同标的同类型 60s 内只记一次
        for a in alerts:
            key = (a["kind"], a["ticker"])
            if time.time() - _external_alert_last.get(key, 0) < 60:
                continue
            _external_alert_last[key] = time.time()
            record_event("external_position_alert", **{k: v for k, v in a.items() if k != "ts"})
        return stats

    for trade in list(trades.values()):
        if trade.isDone():
            continue
        order = trade.order
        order_id = int(order.orderId)
        if gateway.is_tool_order_id(order_id) or order_id in legacy_ids:
            continue
        if not _external_account_ok(order, accounts):
            stats["skipped"] += 1
            logger.warning("外部挂单账户无法确认，跳过 orderId=%s", order_id)
            continue
        if order_id == 0:
            # TWS 手动单：API 侧 orderId=0 无法定位撤单（与 SKHY/META 手动
            # 止损单同款限制），白试永远失败——只告警一次跳过，等 TWS 自终结
            stats["skipped"] += 1
            key = ("manual_order", trade.contract.symbol)
            if time.time() - _external_alert_last.get(key, 0) >= 300:
                _external_alert_last[key] = time.time()
                logger.warning("TWS 手动单 %s（orderId=0）API 不可撤，"
                               "请在 TWS 手动处理或等其终结", trade.contract.symbol)
            continue
        ok = gateway.cancel_order(trade)
        if not ok:
            stats["failed"] += 1
            record_event("external_order_cancel_failed", order_id=order_id,
                         error="cancel_order 调用失败")
            continue
        await _wait_trade_terminal(trade, timeout=5.0)
        status = trade.orderStatus.status
        if trade.isDone() and status != "Filled":
            stats["cancelled"] += 1
            record_event("external_order_cancelled", order_id=order_id,
                         symbol=trade.contract.symbol, status=status)
        else:
            stats["failed"] += 1
            record_event("external_order_cancel_failed", order_id=order_id,
                         symbol=trade.contract.symbol, status=status)

    positions = await gateway.positions()
    if positions is None:
        logger.warning("外部持仓清理跳过：撤单后持仓快照不可用")
        stats["failed"] += 1
        return stats
    tool_qty = {g["symbol"]: g["net_qty"] for g in order_mgr.aggregate()}
    for ticker in sorted(set(positions) | set(tool_qty)):
        residual = positions.get(ticker, 0) - tool_qty.get(ticker, 0)
        if abs(residual) < 1:
            continue
        existing = _external_cleanup_inflight.get(ticker)
        if existing is not None and not existing.isDone():
            stats["pending"] += 1
            continue
        _external_cleanup_inflight.pop(ticker, None)
        registered = next((t for t in trades.values()
                           if getattr(t.order, "orderRef", "") == "external_cleanup"
                           and t.contract.symbol == ticker and not t.isDone()), None)
        if registered is not None:
            _external_cleanup_inflight[ticker] = registered
            stats["pending"] += 1
            continue
        symbol = _external_symbol(ticker)
        if symbol is None:
            stats["failed"] += 1
            record_event("external_position_close_failed", ticker=ticker,
                         qty=abs(int(residual)), error="标的不在订阅列表")
            continue
        side = "long" if residual > 0 else "short"
        qty = abs(int(residual))
        # 参考价只取 TV（唯一价格源）：collector 实时 close，不用 TWS 行情
        ref_px = _tv_last_price(symbol)
        if ref_px is None:
            stats["failed"] += 1
            record_event("external_position_close_failed", symbol=symbol,
                         qty=qty, error="TV 现价不可用")
            continue
        result = await gateway.place_close(symbol, side, qty, ref_px,
                                           order_ref="external_cleanup")
        if not result.get("ok") or result.get("trade") is None:
            stats["failed"] += 1
            record_event("external_position_close_failed", symbol=symbol,
                         qty=qty, error=result.get("error") or "平仓提交失败")
            continue
        trade = result["trade"]
        _external_cleanup_inflight[ticker] = trade
        done = await _wait_trade_terminal(trade)
        status = trade.orderStatus.status
        if done and status == "Filled":
            stats["closed"] += 1
            _external_cleanup_inflight.pop(ticker, None)
            record_event("external_position_closed", symbol=symbol, side=side,
                         qty=qty, order_id=result.get("order_id"),
                         avg_fill=trade.orderStatus.avgFillPrice)
        elif done:
            stats["failed"] += 1
            _external_cleanup_inflight.pop(ticker, None)
            record_event("external_position_close_failed", symbol=symbol,
                         qty=qty, status=status)
        else:
            stats["pending"] += 1
            record_event("external_position_close_pending", symbol=symbol,
                         qty=qty, order_id=result.get("order_id"), status=status)
    return stats


async def _external_cleanup_loop() -> None:
    while True:
        await asyncio.sleep(30.0)
        try:
            await _external_cleanup_once()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.exception("外部订单清理异常: %s", e)


async def _position_scan_loop() -> None:
    """持仓扫描（每分钟，双向）：持仓方向与 trend_state 确认值（ts_c，上一根
    已收盘 5M bar，不看盘中实时值）冲突时——多仓见 direction==-1、空仓见
    direction==1——实盘自动平仓；平仓失败或开关关闭时每分钟弹窗提醒人工处理。"""
    while True:
        await asyncio.sleep(60.0)
        try:
            if not _is_trading_hours() or collector.data_health()["stale"]:
                continue
            positions = await gateway.positions()   # {ticker: qty}；None=快照不可用
            if positions is None:
                logger.warning("持仓扫描跳过：IBKR 持仓快照不可用")
                continue
            market = collector.latest()
            for ticker, pos in positions.items():
                if pos == 0:
                    continue
                side = "long" if pos > 0 else "short"
                # ticker（无交易所前缀）→ 完整 symbol
                sym = None
                for s, data in market.items():
                    if s.split(":")[-1] == ticker:
                        sym = s
                        break
                if sym is None:
                    continue
                data = market[sym]
                # 只用上一根已收盘 bar 的确认值（ts_c），不看盘中实时值（ts）
                ts_c = _ts_c(data)
                direction = ts_c.get("direction")
                flipped = ((pos > 0 and direction == -1)
                           or (pos < 0 and direction == 1))
                if not flipped:
                    continue   # 趋势未反向
                st_line = ts_c.get("supertrend")
                bar = data.get("bar") or {}
                close = bar.get("close")
                live = bool(gateway.enabled and gateway._connected)
                if config.AUTO_CLOSE_ON_ST_FLIP and live:
                    logger.warning("持仓扫描: %s %s %s 股趋势已反向(ST %.2f)，自动平仓",
                                   ticker, side, abs(pos), st_line or 0)
                    ok = await _close_on_flip(ticker, sym, pos, close)
                    record_event("auto_close_st_flip", symbol=sym, ticker=ticker,
                                 side=side, qty=abs(pos), st_line=st_line,
                                 close=close, ok=ok, status="requested")
                    if ok:
                        _scan_alerts.pop(ticker, None)
                        _auto_close_notices[ticker] = {
                            "symbol": sym, "ticker": ticker, "side": side,
                            "qty": abs(pos), "st_line": st_line, "close": close,
                            "closed_at": time.time(), "pending": True,
                        }
                        continue
                    # 自动平仓失败 → 落入提醒逻辑，下分钟重试
                _scan_alerts[ticker] = {
                    "symbol": sym,
                    "ticker": ticker,
                    "side": side,
                    "qty": abs(pos),
                    "st_line": st_line,
                    "close": close,
                    "auto_close_failed": bool(config.AUTO_CLOSE_ON_ST_FLIP and live),
                    "scanned_at": time.time(),
                }
                logger.info("持仓扫描: %s %s %s 股趋势已反向(ST %.2f)，提醒平仓",
                            ticker, side, abs(pos), st_line or 0)
                record_event("position_scan_alert", symbol=sym, ticker=ticker,
                             side=side, qty=abs(pos), st_line=st_line, close=close)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.exception("持仓扫描异常: %s", e)


async def _stop_guard_loop() -> None:
    """止损单守卫（每分钟）：FA 组每个持仓必须有足够的 STP/TRAIL 保护单覆盖，
    裸仓（保护单股数 < 持仓股数）每分钟提醒一次，直到补齐。只告警不自动补单。"""
    while True:
        await asyncio.sleep(60.0)
        try:
            if not config.STOP_GUARD_ENABLED or not gateway._connected:
                continue
            positions = await gateway.positions()
            if positions is None:
                logger.warning("止损守卫跳过：IBKR 持仓快照不可用")
                continue
            covers = await gateway.open_protective_qty()
            if covers is None:
                logger.warning("止损守卫跳过：IBKR 开放订单快照不可用")
                continue
            now = time.time()
            missing: Set[str] = set()
            for ticker, pos in positions.items():
                side = "long" if pos > 0 else "short"
                need = "sell" if pos > 0 else "buy"   # 平多要 SELL 保护单
                covered = (covers.get(ticker) or {}).get(need, 0.0)
                if covered >= abs(pos):
                    continue
                missing.add(ticker)
                prev = _stop_alerts.get(ticker)
                _stop_alerts[ticker] = {
                    "ticker": ticker,
                    "side": side,
                    "qty": abs(pos),
                    "covered_qty": covered,
                    "since": prev["since"] if prev else now,
                    "checked_at": now,
                }
                logger.warning("⚠️ 止损单缺失: %s %s %s 股，%s 保护单仅 %s 股"
                               "——裸仓，请立即补挂止损",
                               ticker, side, abs(pos), need.upper(), covered)
                record_event("missing_stop_alert", ticker=ticker, side=side,
                             qty=abs(pos), covered_qty=covered)
            # 已补齐/已平仓的标的移出提醒
            for t in list(_stop_alerts):
                if t not in missing:
                    _stop_alerts.pop(t, None)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.exception("止损单守卫异常: %s", e)


def _lot_protection_alive(lot, trades: Optional[dict]) -> bool:
    """ACTIVE lot 的 STP 保护单是否在 IBKR 侧存活：内存句柄未终结，或
    快照里 trail_order_id 对应订单未终结（重启后句柄丢失靠快照认回）。
    trades=None（快照不完整）时调用方应整拍跳过，不得据此告警。"""
    t = getattr(lot, "trail_trade", None)
    if t is not None and not t.isDone():
        return True
    oid = getattr(lot, "trail_order_id", None)
    if oid and trades:
        tr = trades.get(oid)
        if tr is not None and not tr.isDone():
            return True
    return False


async def _naked_position_check_once(fails: dict) -> None:
    """裸仓巡检单拍（每 10s 由循环调用）：
    ACTIVE lot 的 STP 保护单缺失 → critical 告警 + 自动补挂（价格
    lot.stop_px，走 OrderManager._restore_protection——含幻影守卫与快照
    完整性检查，重建后认回句柄）；连续 3 次补挂失败 → 升级市价平仓 +
    最高级别告警。只补止损，绝不影响正常仓位；dry-run/未连接整拍跳过。"""
    if not (gateway.enabled and gateway._connected):
        return
    actives = [l for l in order_mgr.list_lots(active_only=True)
               if l["state"] == "ACTIVE" and (l.get("filled_qty") or 0) > 0]
    if not actives:
        return
    trades = await gateway.trade_map()
    if trades is None:
        logger.warning("裸仓巡检跳过：IBKR 订单快照不完整")
        return
    for d in actives:
        lot = order_mgr.get(d["lot_id"])
        if lot is None or lot.state != "ACTIVE":
            continue
        key = f"naked:{lot.lot_id}"
        if _lot_protection_alive(lot, trades):
            fails.pop(lot.lot_id, None)
            await notifier.recover(key, "STP 保护单已恢复存活")
            continue
        await notifier.notify(key, "critical", "裸仓告警",
                              f"{lot.symbol} ACTIVE 仓 {lot.filled_qty} 股 "
                              f"STP 保护单缺失，自动补挂中 @ {lot.stop_px}")
        logger.warning("裸仓巡检: %s STP 缺失，自动补挂 @%.2f",
                       lot.symbol, lot.stop_px)
        ok = await order_mgr._restore_protection(lot)
        if ok:
            fails.pop(lot.lot_id, None)
            await notifier.recover(key, f"已自动补挂 STP @ {lot.stop_px}")
            continue
        fails[lot.lot_id] = fails.get(lot.lot_id, 0) + 1
        logger.error("裸仓巡检: %s 补挂失败（连续 %d 次）",
                     lot.lot_id, fails[lot.lot_id])
        if fails[lot.lot_id] >= 3:
            ref_px = (_tv_last_price(lot.symbol) or lot.last_px
                      or lot.avg_fill or lot.entry_px)
            ok2, err = await order_mgr.close_lot(
                lot.lot_id, ref_px=ref_px, exit_reason="naked_guard")
            await notifier.notify(
                key + ":force_closed", "critical", "裸仓强平",
                f"{lot.symbol} 补挂 STP 连续 3 次失败，"
                + ("已提交市价平仓，请核查成交" if ok2
                   else f"平仓也失败（{err}），请立即人工处理"))
            fails.pop(lot.lot_id, None)


async def _naked_position_loop() -> None:
    """裸仓巡检循环（每 10s，单拍见 _naked_position_check_once）。"""
    fails: dict = {}
    while True:
        await asyncio.sleep(10.0)
        try:
            await _naked_position_check_once(fails)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.exception("裸仓巡检异常: %s", e)


async def _tv_startup_check() -> None:
    """启动首要任务：TV 数据源就绪检查，正常/异常都给出提示确认（一次性）。

    就绪 = 快照标的数达到股票池且全部 ts_c 有 trend_state；
    异常 = study 通道系统性故障（collector.study_health().systemic，
    覆盖 2026-10-27 的 pine 协议故障模式）或 240s 超时未就绪。
    正常 → info（前端横幅 + 事件，不打扰手机）；异常 → critical（含 iMessage）。
    """
    def _ready_counts() -> tuple:
        snap = collector.latest() or {}
        total = len(snap)
        ready = sum(1 for v in snap.values()
                    if isinstance(v.get("ts_c"), dict)
                    and v["ts_c"].get("trend_state") is not None)
        return total, ready

    t0 = time.time()
    deadline = t0 + 240.0    # 分批订阅 ~30-90s + 指标 warmup，宽限到 4 分钟
    expected = len(config.WATCHLIST)
    while time.time() < deadline:
        health = collector.study_health()
        if health.get("systemic"):
            await notifier.notify(
                "tv_startup", "critical", "TV 数据源异常",
                f"启动检查发现 study 通道系统性故障（60s 内 study 报错 "
                f"{health['study_errors_60s']} 条，ts_c 就绪 "
                f"{health['ts_ready']}/{health['ts_total']}），请检查 daemon 日志")
            return
        total, ready = _ready_counts()
        if total >= expected and ready >= total:
            await notifier.notify(
                "tv_startup", "info", "TV 数据源正常",
                f"{ready}/{expected} 标的数据就绪，启动耗时 {time.time() - t0:.0f}s")
            return
        await asyncio.sleep(5.0)
    total, ready = _ready_counts()
    await notifier.notify(
        "tv_startup", "critical", "TV 数据源异常",
        f"启动 240s 后数据仍未就绪：ts_c {ready}/{total}（股票池 {expected}，"
        f"daemon 运行中={collector.running}），请检查 daemon/网络")


async def _health_check_once(was_connected: Optional[bool]) -> bool:
    """健康检查单拍（TWS 断线 + 数据陈旧），返回本轮 connected 状态。

    - TWS 断线：connected 跳变 False → critical「TWS 已断线，请人工重启
      7496」；恢复 → 解除通知。首轮只建基线不告警（启动瞬间未连接不报）。
    - 数据陈旧：交易时段内传输中断、内容停止变化或 bar 超龄 → warning；
      恢复自动解除并通知；非交易时段/周末静默（挂着的话静默摘除，不发恢复通知）。"""
    connected = bool(gateway._connected)
    if was_connected is not None:
        if was_connected and not connected:
            await notifier.notify("tws_down", "critical", "TWS 断线",
                                  "TWS 已断线，请人工重启 7496")
        elif not was_connected and connected:
            await notifier.recover("tws_down", "TWS 已恢复连接")
    health = collector.data_health()
    if health["trading_hours"]:
        if health["stale"]:
            content_age = health["content_age"]
            age_txt = "∞" if content_age is None else f"{int(content_age)}s"
            await notifier.notify(
                "data_stale", "warning", "行情数据陈旧",
                f"TV 内容 {age_txt} 未变化（{health['reason']}），正在自动重建订阅")
        else:
            await notifier.recover("data_stale", "行情数据已恢复更新")
    else:
        notifier.clear("data_stale")   # 非交易时段/周末静默
    # study（指标）通道：K线正常但指标全灭时 data_stale 检测不到
    # （2026-10-27 协议故障实锤）。systemic 判定与宽限在 collector 内。
    sh = collector.study_health()
    if sh["systemic"]:
        await notifier.notify(
            "tv_study_dead", "critical", "TV 指标数据中断",
            f"study_error 60s内 {sh['study_errors_60s']} 条，ts_c 就绪 "
            f"{sh['ts_ready']}/{sh['ts_total']}（持续 {int(sh['ts_dead_for'])}s）。"
            "运行 python3 service/tv_diag.py 分层定位")
    else:
        await notifier.recover("tv_study_dead", "指标数据已恢复")
    return connected


async def _health_watch_loop() -> None:
    """健康巡检循环（每 5s，单拍见 _health_check_once）。"""
    was_connected: Optional[bool] = None
    while True:
        await asyncio.sleep(5.0)
        try:
            was_connected = await _health_check_once(was_connected)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.exception("健康巡检异常: %s", e)


# ---------------------------------------------------------------------------
# 每日复盘报告（shadow_report）：交易日 16:20 ET 自动生成 + 手动重算
# ---------------------------------------------------------------------------

_REPORTS_DIR = Path(__file__).resolve().parent.parent / "logs"


async def _generate_daily_report(day: str, db_path: Optional[Path] = None
                                 ) -> dict:
    """生成并落盘当日复盘报告（logs/report-YYYYMMDD.json）+ 事件 +
    iMessage 摘要（未配置收件人自动跳过）。"""
    report = shadow_report.generate_report(day, db_path=db_path)
    path = _REPORTS_DIR / f"report-{day.replace('-', '')}.json"
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(report, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    os.replace(tmp, path)
    bm = report["big_moves"]
    record_event("shadow_report", day=day, total=report["total_signals"],
                 big_moves=bm["total_big_moves"], caught=bm["caught_count"],
                 missed=bm["missed_count"], path=str(path))
    lines = shadow_report.summary_lines(report)
    await notifier.notify(f"report:{day}", "info", "每日复盘", lines[0])
    await notifier.send_imessage(f"report:{day}", "\n".join(lines))
    logger.info("每日复盘报告已生成: %s（%d 笔影子单）", path,
                report["total_signals"])
    return report


async def _report_check_once(now, done_days: set,
                             db_path: Optional[Path] = None) -> bool:
    """调度单拍（可测）：ET 工作日且时间 ≥16:20 且当天未生成 → 生成。
    返回本拍是否生成。周末（weekday>=5）不跑。"""
    if now.weekday() >= 5:
        return False
    hm = now.hour * 60 + now.minute
    day_key = now.strftime("%Y-%m-%d")
    if hm < 16 * 60 + 20 or day_key in done_days:
        return False
    await _generate_daily_report(day_key, db_path=db_path)
    done_days.add(day_key)
    return True


async def _report_loop() -> None:
    """每日复盘调度（每 60s 检查；单拍见 _report_check_once）。"""
    from zoneinfo import ZoneInfo
    import datetime as _dt
    et = ZoneInfo("America/New_York")
    done_days: set = set()
    while True:
        await asyncio.sleep(60.0)
        try:
            await _report_check_once(_dt.datetime.now(et), done_days)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.exception("每日复盘调度异常: %s", e)


# ---------------------------------------------------------------------------
# 盘前检查单（precheck）：开盘前绿灯清单，09:25 ET 自动跑 + 手动随时跑
# ---------------------------------------------------------------------------

_PRECHECK_DIR = Path(__file__).resolve().parent.parent / "logs"


def _pc_item(key: str, name: str, status: str, detail: str) -> dict:
    return {"key": key, "name": name, "status": status, "detail": detail}


async def run_precheck() -> dict:
    """盘前检查单（纯读现有状态，无副作用）。
    每项 {key, name, status: ok/bad/neutral/skip, detail}；
    overall = 有红则 bad，否则 ok（neutral/skip 不拖红）。"""
    from zoneinfo import ZoneInfo
    import datetime as _dt
    et = ZoneInfo("America/New_York")
    now = _dt.datetime.now(et)
    hm = now.hour * 60 + now.minute
    items = []

    # 1) 数据流：快照标的覆盖率 + 最新 bar 新鲜度
    market = collector.latest() or {}
    n_syms, n_watch = len(market), len(config.WATCHLIST)
    missing = sorted(set(config.WATCHLIST) - set(market))
    bt = collector.latest_bar_time or 0
    age = (time.time() - bt) if bt else None
    fresh = age is not None and age < 600
    detail = (f"标的 {n_syms}/{n_watch}"
              + (f"，缺 {', '.join(m[:12] for m in missing[:5])}"
                 + ("…" if len(missing) > 5 else "") if missing else "")
              + ("；最新 bar " + ("无数据" if age is None
                 else f"{int(age // 60)} 分钟前" + ("（新鲜）" if fresh
                 else "（>10 分钟，陈旧）"))))
    items.append(_pc_item("data_flow", "数据流",
                          "ok" if (n_syms >= n_watch and fresh) else "bad",
                          detail))

    # 2) 盘前区间注入（attack_c）：09:30 前未注入属正常（中性「待注入」）
    n_attack = sum(1 for d in market.values()
                   if (d.get("attack_c") or {}).get("pmh") is not None)
    if hm < 570:
        items.append(_pc_item("attack_zone", "盘前区间注入", "neutral",
                              f"待注入（09:30 锁定后注入）：当前 {n_attack}/"
                              f"{n_syms} 已有 attack_c 值"))
    else:
        items.append(_pc_item("attack_zone", "盘前区间注入",
                              "ok" if (n_syms and n_attack >= n_syms)
                              else "bad",
                              (f"{n_attack}/{n_syms} 标的已注入盘前区间"
                               if n_syms else "无快照数据（数据流先修）")))

    # 3) IBKR：连接 + 净值可取（取不到则日亏损熔断处于 fail-open）
    status = await get_status_cached()
    connected = bool(gateway._connected)
    equity = status.get("equity")
    ok3 = connected and equity is not None
    items.append(_pc_item(
        "ibkr", "IBKR 连接", "ok" if ok3 else "bad",
        ("已连接" if connected else "未连接")
        + (f"，净值 ${equity:,.0f}" if equity is not None
           else "，净值取不到（日盈亏熔断 fail-open，风控不设防）")))

    # 4) 风险预算：设置值 / 已用 / 余量
    exp = order_mgr.risk_exposure()["total"]
    budget = config.RISK_MAX_TOTAL_RISK_USD
    if budget <= 0:
        items.append(_pc_item("risk_budget", "风险预算", "neutral",
                              f"未设总敞口预算（0=不限制）；当前已用 ${exp:,.0f}"))
    else:
        over = exp > budget
        items.append(_pc_item("risk_budget", "风险预算",
                              "bad" if over else "ok",
                              f"预算 ${budget:,.0f} / 已用 ${exp:,.0f} / "
                              f"余量 ${budget - exp:,.0f}"
                              + ("（超预算！）" if over else "")))

    # 5) 跟单进程：enabled 服务器进程存活 + follower 净值同步
    servers = [s for s in _load_copy_servers() if s.get("enabled")]
    if not servers:
        items.append(_pc_item("copy", "跟单进程", "skip", "无启用的跟单服务器"))
    else:
        bad5 = []
        for s in servers:
            name = s.get("name", "?")
            alive = copy_supervisor.process_alive(name)
            st = _copy_status(name) or {}
            if not alive:
                bad5.append(f"{name} 进程不在")
            elif st and st.get("master_ws") is False:
                bad5.append(f"{name} 未连上 master")
            elif st and st.get("last_error"):
                bad5.append(f"{name} 报错: {str(st['last_error'])[:30]}")
        items.append(_pc_item("copy", "跟单进程",
                              "bad" if bad5 else "ok",
                              "；".join(bad5) if bad5
                              else f"{len(servers)} 台 enabled 服务器进程均在运行"))

    # 6) 时钟：本机 vs IBKR 服务器（未连接跳过）vs 数据时钟
    if not connected:
        items.append(_pc_item("clock", "时钟", "skip",
                              "IBKR 未连接，服务器时钟比对跳过"))
    else:
        ibkr_t = gateway.ibkr_time or 0
        drift = abs(time.time() - ibkr_t) if ibkr_t else None
        ok6 = drift is not None and drift < 30
        items.append(_pc_item("clock", "时钟",
                              "ok" if ok6 else "bad",
                              (f"IBKR 时钟偏差 {drift:.1f}s" if drift is not None
                               else "IBKR 时钟未取得")
                              + (f"；数据时钟 {int(age)}s 前"
                                 if age is not None else "")))

    # 7) 昨日遗留：昨天事件日志里未恢复的 warning/critical 告警
    yday = (now - _dt.timedelta(days=1)).strftime("%Y%m%d")
    ev_path = _PRECHECK_DIR / f"events-{yday}.jsonl"
    if not ev_path.exists():
        items.append(_pc_item("leftover", "昨日遗留告警", "neutral",
                              f"无昨日事件日志（{yday}）"))
    else:
        alerts: dict = {}
        try:
            for line in ev_path.read_text(encoding="utf-8").splitlines():
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if rec.get("type") == "alert" and rec.get("level") in (
                        "warning", "critical"):
                    alerts[rec.get("key")] = (rec.get("level"),
                                              rec.get("title"), rec.get("msg"))
                elif rec.get("type") == "alert_recovered":
                    alerts.pop(rec.get("key"), None)
        except OSError:
            alerts = {}
        crits = {k: v for k, v in alerts.items() if v[0] == "critical"}
        if crits:
            items.append(_pc_item("leftover", "昨日遗留告警", "bad",
                                  "；".join(f"{v[1]}" for v in
                                            list(crits.values())[:3])
                                  + (f" 等 {len(crits)} 条" if len(crits) > 3
                                     else "") + "（昨日未恢复）"))
        elif alerts:
            items.append(_pc_item("leftover", "昨日遗留告警", "neutral",
                                  f"{len(alerts)} 条 warning 级昨日未恢复"))
        else:
            items.append(_pc_item("leftover", "昨日遗留告警", "ok",
                                  "昨日无未恢复告警"))

    overall = "bad" if any(i["status"] == "bad" for i in items) else "ok"
    return {"ts": time.time(), "day": now.strftime("%Y-%m-%d"),
            "overall": overall, "items": items}


async def _precheck_check_once(now, done_days: set) -> bool:
    """调度单拍（可测）：ET 工作日且时间 ≥09:25 且当天未跑 → 跑检查单并落盘
    （logs/precheck-YYYYMMDD.json）+ 事件 + 有红项走 notifier（critical：
    声音 + 前端 + iMessage 摘要列出红项）。返回本拍是否执行。"""
    if now.weekday() >= 5:
        return False
    hm = now.hour * 60 + now.minute
    day_key = now.strftime("%Y-%m-%d")
    if hm < 9 * 60 + 25 or day_key in done_days:
        return False
    report = await run_precheck()
    path = _PRECHECK_DIR / f"precheck-{day_key.replace('-', '')}.json"
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(report, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    os.replace(tmp, path)
    bads = [i for i in report["items"] if i["status"] == "bad"]
    record_event("precheck", day=day_key, overall=report["overall"],
                 bad=[i["key"] for i in bads], path=str(path))
    if bads:
        await notifier.notify(
            f"precheck:{day_key}", "critical", "盘前检查有红项",
            "；".join(f"{i['name']}（{i['detail']}）" for i in bads))
    logger.info("盘前检查单已生成: %s（%s，红项 %d）", path, report["overall"],
                len(bads))
    done_days.add(day_key)
    return True


async def _precheck_loop() -> None:
    """盘前检查调度（每 60s 检查；ET 工作日 09:25 自动跑一次；
    单拍见 _precheck_check_once，周末不跑）。"""
    from zoneinfo import ZoneInfo
    import datetime as _dt
    et = ZoneInfo("America/New_York")
    done_days: set = set()
    while True:
        await asyncio.sleep(60.0)
        try:
            await _precheck_check_once(_dt.datetime.now(et), done_days)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.exception("盘前检查调度异常: %s", e)


async def _slow_break_loop() -> None:
    """未快速突破提醒（每分钟）：ACTIVE 持仓开仓超过 SLOW_BREAK_SECONDS 仍未保本
    → 记录提醒，前端语音播报「未快速突破，是否减仓」。每个 lot 只提醒一次。"""
    while True:
        await asyncio.sleep(60.0)
        try:
            if not _is_trading_hours():
                continue
            now = time.time()
            active = order_mgr.list_lots(active_only=True)
            active_ids = set()
            for lot in active:
                lot_id = lot.get("lot_id")
                if not lot_id:
                    continue
                active_ids.add(lot_id)
                if lot.get("breakeven_reached"):
                    continue   # 已保本，不提醒
                entry_time = lot.get("entry_time") or 0
                if entry_time <= 0:
                    continue   # 无实际成交时间，不提醒
                elapsed = now - entry_time
                if elapsed < config.SLOW_BREAK_SECONDS:
                    continue   # 未到阈值
                if lot_id not in _slow_break_alerts:
                    _slow_break_alerts[lot_id] = {
                        "lot_id": lot_id,
                        "symbol": lot.get("symbol"),
                        "ticker": (lot.get("symbol") or "").split(":")[-1],
                        "qty": lot.get("filled_qty") or lot.get("qty"),
                        "elapsed_seconds": round(elapsed, 1),
                        "alerted_at": now,
                    }
                    logger.warning("未快速突破提醒: %s 开仓 %.0f 分钟未保本，提示是否减仓",
                                   lot_id, elapsed / 60)
                    record_event("slow_break_alert", lot_id=lot_id,
                                 symbol=lot.get("symbol"),
                                 elapsed_seconds=round(elapsed, 1))
            # 已平仓/已保本的 lot 移出提醒
            for lid in list(_slow_break_alerts):
                if lid not in active_ids:
                    _slow_break_alerts.pop(lid, None)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.exception("未快速突破提醒异常: %s", e)


async def _auto_breakeven_loop() -> None:
    """自动 2R 保本（每秒检查）：ACTIVE 持仓浮盈 ≥ AUTO_BREAKEVEN_R×初始风险
    → 自动把止损移到实际入场成本。覆盖所有本地工具 ACTIVE lot。"""
    scan_tick = 0
    last_error: dict = {}
    last_attempt: dict = {}
    while True:
        await asyncio.sleep(1.0)
        try:
            if (not _is_trading_hours() or not gateway._connected
                    or collector.data_health()["stale"]):
                continue
            scan_tick += 1
            active = order_mgr.list_lots(active_only=True)
            if scan_tick % 30 == 0:
                logger.info("自动保本循环扫描: %d 个活跃 lot", len(active))
            now = time.time()
            for lot in active:
                lot_id = lot.get("lot_id")
                if lot.get("state") != "ACTIVE" or lot.get("breakeven_reached"):
                    continue
                side = lot.get("side")
                avg_fill = lot.get("avg_fill") or lot.get("entry_px")
                init_entry = lot.get("init_entry_px") or lot.get("entry_px")
                init_stop = lot.get("init_stop_px") or lot.get("stop_px")
                filled = lot.get("filled_qty") or lot.get("qty")
                if not avg_fill or not init_entry or not init_stop or filled <= 0:
                    continue
                risk = abs(init_entry - init_stop)
                if risk <= 0:
                    continue
                # 用 TV 当前 bar 极值：多仓看 high、空仓看 low，避免盘中越过 2R 后回落漏保
                _snap = (collector.latest() or {}).get(lot.get("symbol"), {})
                _bar = _snap.get("bar") or {}
                price = _bar.get("high") if side == "long" else _bar.get("low")
                if not price or price <= 0:
                    continue
                profit = (price - avg_fill) if side == "long" else (avg_fill - price)
                rr = profit / risk
                if rr < config.AUTO_BREAKEVEN_R:
                    continue
                # 每 lot 5s 调用节流（与策略引擎 breakeven_attempt_at 一致）：
                # breakeven 内部有 placeOrder + 2.5s 确认/全量订单快照，不能每秒打
                if now - last_attempt.get(lot_id, 0) < 5.0:
                    continue
                last_attempt[lot_id] = now
                ok, err = await order_mgr.breakeven(lot_id)
                if ok:
                    logger.warning("自动保本 %s %s 浮盈 %.2fR，止损移成本 %.2f",
                                   lot.get("symbol"), side, rr, avg_fill)
                    record_event("auto_breakeven", lot_id=lot_id,
                                 symbol=lot.get("symbol"), side=side,
                                 unrealized_r=round(rr, 2), new_stop=round(avg_fill, 2))
                    last_error.pop(lot_id, None)
                elif now - last_error.get(lot_id, 0) >= 30:
                    last_error[lot_id] = now
                    logger.warning("自动保本失败 %s %s: %s",
                                   lot.get("symbol"), side, err)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.exception("自动保本循环异常: %s", e)


def _fvg_reverse_action(side: str, fvg_event: int, avg_fill: Optional[float],
                        price: Optional[float]) -> Optional[str]:
    """反向 FVG 收盘确认后的动作判定（纯函数）：
    同向缺口/数据缺失 → None；浮亏 → "close"；浮盈（含持平）→ "protect"。"""
    if side == "long" and fvg_event != -1:
        return None
    if side == "short" and fvg_event != 1:
        return None
    if not avg_fill or not price or price <= 0:
        return None
    floating = (price - avg_fill) if side == "long" else (avg_fill - price)
    return "close" if floating < 0 else "protect"


async def _fvg_reverse_guard_loop() -> None:
    """持仓反向 FVG 守卫（每秒，5M 收盘确认口径）：本工具 ACTIVE lot 的标的出现
    反向缺口确认（fvg_c.time == bar_c.time 对齐）时——浮亏立即市价平仓，浮盈
    立即保本 + 前端语音/弹框提醒平仓锁利。每 lot 每根确认 bar 只动作一次。"""
    while True:
        await asyncio.sleep(1.0)
        try:
            if not (config.FVG_REVERSE_GUARD_ENABLED and _is_trading_hours()
                    and gateway._connected and not collector.data_health()["stale"]):
                continue
            market = collector.latest() or {}
            active = [l for l in order_mgr.list_lots(active_only=True)
                      if l.get("state") == "ACTIVE"]
            for lot in active:
                lot_id = lot.get("lot_id")
                symbol = lot.get("symbol")
                side = lot.get("side")
                data = market.get(symbol) or {}
                bar_c = data.get("bar_c") or {}
                fvg_c = data.get("fvg_c") or {}
                bt = bar_c.get("time") or 0
                event = fvg_c.get("fvg_event")
                if not bt or event not in (1, -1) or fvg_c.get("time") != bt:
                    continue   # 无确认缺口，或 fvg_c 未追上 bar_c（等下一拍）
                act_key = f"{lot_id}:{int(bt)}"
                if act_key in _fvg_reverse_acted:
                    continue
                price = _tv_last_price(symbol) or bar_c.get("close")
                avg_fill = lot.get("avg_fill") or lot.get("entry_px")
                action = _fvg_reverse_action(side, event, avg_fill, price)
                if action is None:
                    continue
                _fvg_reverse_acted[act_key] = time.time()
                ticker = symbol.split(":")[-1]
                qty = lot.get("filled_qty") or lot.get("qty")
                fvg_top = fvg_c.get("fvg_last_top")
                fvg_bottom = fvg_c.get("fvg_last_bottom")
                pnl = ((price - avg_fill) if side == "long"
                       else (avg_fill - price)) * qty
                if action == "close" and config.FVG_REVERSE_AUTO_CLOSE:
                    logger.warning("反向FVG守卫: %s %s 浮亏 %.2f，缺口 %.2f~%.2f，市价平仓",
                                   symbol, side, pnl, fvg_bottom or 0, fvg_top or 0)
                    ok, err = await order_mgr.close_lot(
                        lot_id, ref_px=price, exit_reason="fvg_reverse")
                    record_event("fvg_reverse_close", lot_id=lot_id, symbol=symbol,
                                 ticker=ticker, side=side, qty=qty, price=price,
                                 avg_fill=avg_fill, pnl=round(pnl, 2),
                                 fvg_top=fvg_top, fvg_bottom=fvg_bottom,
                                 ok=ok, error=err or "", status="requested")
                    if ok:
                        _fvg_reverse_alerts.pop(lot_id, None)
                        _fvg_reverse_notices[ticker] = {
                            "symbol": symbol, "ticker": ticker, "side": side,
                            "qty": qty, "price": price, "pnl": round(pnl, 2),
                            "closed_at": time.time(), "pending": True,
                        }
                        continue
                    # 平仓失败 → 落提醒，下一根确认 bar 重试
                    logger.warning("反向FVG守卫: %s 自动平仓失败: %s", symbol, err)
                    _fvg_reverse_alerts[lot_id] = {
                        "kind": "close_failed", "lot_id": lot_id,
                        "symbol": symbol, "ticker": ticker, "side": side,
                        "qty": qty, "price": price, "pnl": round(pnl, 2),
                        "alerted_at": time.time(),
                    }
                    continue
                if action == "close":
                    # 自动平仓开关关闭 → 只提醒
                    _fvg_reverse_alerts[lot_id] = {
                        "kind": "close_failed", "lot_id": lot_id,
                        "symbol": symbol, "ticker": ticker, "side": side,
                        "qty": qty, "price": price, "pnl": round(pnl, 2),
                        "alerted_at": time.time(),
                    }
                    continue
                # protect：浮盈 → 立即保本 + 提醒锁利
                if not lot.get("breakeven_reached"):
                    ok, err = await order_mgr.breakeven(lot_id)
                    if ok:
                        logger.warning("反向FVG守卫: %s %s 浮盈 %.2f，已保本",
                                       symbol, side, pnl)
                        record_event("fvg_reverse_breakeven", lot_id=lot_id,
                                     symbol=symbol, ticker=ticker, side=side,
                                     qty=qty, price=price, pnl=round(pnl, 2),
                                     new_stop=round(avg_fill, 2))
                    else:
                        logger.warning("反向FVG守卫: %s 保本失败: %s", symbol, err)
                _fvg_reverse_alerts[lot_id] = {
                    "kind": "profit_lock", "lot_id": lot_id,
                    "symbol": symbol, "ticker": ticker, "side": side,
                    "qty": qty, "price": price, "pnl": round(pnl, 2),
                    "fvg_top": fvg_top, "fvg_bottom": fvg_bottom,
                    "alerted_at": time.time(),
                }
                record_event("fvg_reverse_alert", lot_id=lot_id, symbol=symbol,
                             ticker=ticker, side=side, qty=qty, price=price,
                             pnl=round(pnl, 2), fvg_top=fvg_top,
                             fvg_bottom=fvg_bottom)
            # 已平仓的 lot 移出提醒（notice 保留供前端语音去重）
            active_ids = {l.get("lot_id") for l in active}
            for lid in list(_fvg_reverse_alerts):
                if lid not in active_ids:
                    _fvg_reverse_alerts.pop(lid, None)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.exception("反向FVG守卫异常: %s", e)


# ---------------------------------------------------------------------------
# 事件记录与快照辅助
# ---------------------------------------------------------------------------

def _shadow_record_event(tr) -> None:
    """影子单建档事件（全类型统一出口）。"""
    record_event("shadow_signal", shadow_id=tr.shadow_id, type=tr.signal_type,
                 symbol=tr.symbol, ticker=tr.ticker, side=tr.side,
                 fill_mode=tr.fill_mode, entry=tr.entry, stop=tr.stop,
                 atr=tr.atr, bar_time=tr.bar_time, meta=tr.meta)


def _shadow_signal(signal_type: str, symbol: str, side: str, trigger_px: float,
                   stop_px: float, atr: float, bar_time: float,
                   meta: Optional[dict] = None, fill_mode: str = "market",
                   limit_px: float = 0.0, dedup_key: Optional[str] = None):
    """统一影子建档（各信号源调用）：登记 + 写事件流。已建档返回 None。"""
    tr = shadow_tracker.record(
        signal_type, symbol, side, trigger_px, stop_px, atr, bar_time,
        meta=meta, fill_mode=fill_mode, limit_px=limit_px,
        dedup_key=dedup_key)
    if tr is not None:
        _shadow_record_event(tr)
    return tr


def _shadow_stop_for(data: dict, side: str, px: float, atr: float) -> float:
    """影子止损位：smart_stop 指标距离优先（所见即所得），缺失回退 1×ATR；
    返回 0 表示无法确定（tracker 还会再兜一次 1×ATR）。"""
    if atr and atr > 0 and px:
        try:
            dist = _smart_stop_dist(data, side, px, atr)
            stop = round(px + (-1 if side == "long" else 1) * dist, 2)
            if stop > 0:
                return stop
        except Exception:
            pass
    return 0.0


async def _shadow_loop() -> None:
    """统一影子跟踪循环（每 1s）：新收盘 5M bar 喂入 ShadowTracker——
    限价单触及成交判定 + 逐 bar 推演（MFE/MAE/止损/EOD 定稿）+ pullback
    回踩事件自检；事件与结算写入当日事件流（shadow_signal/shadow_settled）。
    外部信号源（FVG 事件/回踩/预警、突破、策略候选）在各自产出处建档。"""
    while True:
        await asyncio.sleep(1.0)
        try:
            market = collector.latest()
            if not market:
                continue
            events, settled = shadow_tracker.process(market)
            for tr in events:
                record_event("pullback_event", shadow_id=tr.shadow_id,
                             symbol=tr.symbol, ticker=tr.ticker, side=tr.side,
                             line_type=tr.line_type, bar_time=tr.bar_time,
                             entry=tr.entry, stop=tr.stop, atr=tr.atr)
                _shadow_record_event(tr)   # 统一建档事件（shadow_signal）
            for tr in settled:
                record_event("shadow_settled", shadow_id=tr.shadow_id,
                             symbol=tr.symbol, ticker=tr.ticker, side=tr.side,
                             line_type=tr.line_type, stopped=tr.stopped,
                             mfe_rr=tr.mfe_rr, mfe_rr_eod=tr.mfe_rr_eod,
                             outcome=tr.outcome, outcome_rr=tr.outcome_rr)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.exception("影子回测循环异常: %s", e)


def _fvg_record_event(sym: str, bt: float, data: dict) -> bool:
    """固化一根确认 bar 的 FVG 事件（如有）。返回是否有事件被固化。"""
    fvg_c = data.get("fvg_c") or {}
    event = fvg_c.get("fvg_event")
    if event not in (1, -1):
        return False
    top = fvg_c.get("fvg_last_top")
    bottom = fvg_c.get("fvg_last_bottom")
    if top is None or bottom is None:
        return False   # 边沿缺失无法固化，跳过
    ts_c = data.get("ts_c") or data.get("ts") or {}
    atr = ts_c.get("atr")
    height = round(top - bottom, 4)
    ratio = round(height / atr, 2) if atr and atr > 0 else None
    side = "long" if event == 1 else "short"
    _fvg_events.append({
        "id": f"{sym}:{bt}:{side}",   # symbol+bar+方向，天然去重
        "symbol": sym,
        "ticker": sym.split(":")[-1],
        "side": side,
        "bar_time": bt,
        "top": top,
        "bottom": bottom,
        "height": height,
        "atr": round(atr, 4) if atr else None,
        "ratio": ratio,
    })
    logger.info("FVG 事件固化: %s %s %.2f~%.2f 高 %.2f ratio %s",
                sym, side, bottom, top, height, ratio)
    # 统一影子建档：FVG 事件市价成交（事件确认 bar 收盘价）
    close = (data.get("bar_c") or {}).get("close")
    if close and atr:
        _shadow_signal("fvg_event", sym, side, trigger_px=close,
                       stop_px=_shadow_stop_for(data, side, close, atr),
                       atr=atr, bar_time=bt,
                       meta={"top": top, "bottom": bottom, "height": height,
                             "ratio": ratio},
                       dedup_key=f"fvg_event:{sym}:{int(bt)}:{side}")
    return True


async def _fvg_monitor_loop() -> None:
    """FVG 事件固化循环（每 1s）：某 symbol 的 bar_c.time 前进（= 新 5M bar
    收盘确认）时读 fvg_c.fvg_event，±1 即固化缺口边沿快照进环形缓冲。
    daemon 的 K线回调与 FVG Study 回调是独立通道，同拍可能读到旧 fvg_c：
    用 fvg_c.time == bar_c.time 校验对齐，不齐则挂起下一拍重试（最多 5 拍）。"""
    while True:
        await asyncio.sleep(1.0)
        try:
            market = collector.latest()
            if not market:
                continue
            for sym, data in market.items():
                bar_c = data.get("bar_c") or {}
                bt = bar_c.get("time") or 0
                if not bt:
                    continue
                # ---- FVG 预警（实时通道）：指标在第三根 K 线形成过程中就实时
                # 计算缺口条件（fvg = periods[0]）。等收盘确认会晚一根大 K，
                # 这里在条件成立的第一拍预警一次（每 bar 每方向仅一次）；
                # 盘中回补（event 翻回 0）不影响——预警本来就是提前量。
                bar_rt = data.get("bar") or {}
                fvg_rt = data.get("fvg") or {}
                b_rt = bar_rt.get("time") or 0
                if (b_rt and fvg_rt.get("time") == b_rt
                        and fvg_rt.get("fvg_event") in (1, -1)):
                    side_rt = "long" if fvg_rt["fvg_event"] == 1 else "short"
                    warn_key = f"{sym}:{b_rt}:{side_rt}"
                    if warn_key not in _fvg_warned:
                        _fvg_warned[warn_key] = time.time()
                        top = fvg_rt.get("fvg_last_top")
                        bottom = fvg_rt.get("fvg_last_bottom")
                        ts_rt = data.get("ts") or data.get("ts_c") or {}
                        atr = ts_rt.get("atr")
                        height = (round(top - bottom, 4)
                                  if top is not None and bottom is not None else None)
                        ratio = (round(height / atr, 2)
                                 if height and atr and atr > 0 else None)
                        w = {
                            "id": warn_key + ":warn",
                            "symbol": sym,
                            "ticker": sym.split(":")[-1],
                            "side": side_rt,
                            "bar_time": b_rt,
                            "top": top,
                            "bottom": bottom,
                            "height": height,
                            "atr": round(atr, 4) if atr else None,
                            "ratio": ratio,
                            "warn_time": time.time(),
                        }
                        _fvg_warnings.append(w)
                        record_event("fvg_warn", **{k: w[k] for k in (
                            "symbol", "ticker", "side", "bar_time", "top",
                            "bottom", "height", "atr", "ratio")})
                        logger.info("FVG 预警: %s %s 缺口形成中 %.2f~%.2f ratio %s",
                                    sym, side_rt, bottom or 0, top or 0, ratio)
                        # 统一影子建档：FVG 预警市价成交（预警时实时价）
                        close_rt = bar_rt.get("close")
                        if close_rt and atr:
                            _shadow_signal(
                                "fvg_warn", sym, side_rt, trigger_px=close_rt,
                                stop_px=_shadow_stop_for(data, side_rt,
                                                         close_rt, atr),
                                atr=atr, bar_time=b_rt,
                                meta={"top": top, "bottom": bottom,
                                      "height": height, "ratio": ratio,
                                      "forming": True},
                                dedup_key=f"fvg_warn:{warn_key}")
                # ---- 确认值通道（收盘固化） ----
                # 先冲刷挂起的待固化 bar（等 daemon 的 FVG study 追上 K线）
                pend = _fvg_pending.get(sym)
                if pend is not None:
                    p_bt, retries = pend
                    fvg_c = data.get("fvg_c") or {}
                    if fvg_c.get("time") == p_bt:
                        _fvg_pending.pop(sym, None)
                        _fvg_record_event(sym, p_bt, data)
                    elif retries <= 0:
                        _fvg_pending.pop(sym, None)
                        logger.warning("FVG 事件固化超时放弃: %s bar_time=%s"
                                       "（fvg_c 始终未对齐，事件丢失）", sym, p_bt)
                    else:
                        _fvg_pending[sym] = (p_bt, retries - 1)
                prev = _fvg_last_bar.get(sym)
                if prev is None:
                    _fvg_last_bar[sym] = bt   # 首次见仅建基线，不补录历史事件
                    continue
                if bt <= prev:
                    continue
                _fvg_last_bar[sym] = bt
                fvg_c = data.get("fvg_c") or {}
                fvg_t = fvg_c.get("time")
                if fvg_t is not None and fvg_t != bt:
                    # fvg_c 还是旧 bar 的值（study 回调未追上）：挂起重试
                    _fvg_pending[sym] = (bt, 5)
                    continue
                _fvg_record_event(sym, bt, data)
            # 首次回踩检测：用该 symbol 的实时 bar（未收盘）判首次触区——
            # 多 bar.low <= top / 空 bar.high >= bottom；触发即标记并固化
            # 回踩快照进 _fvg_retests，一个 FVG 终身只报一次
            for sym, data in market.items():
                bar = data.get("bar") or {}
                low, high = bar.get("low"), bar.get("high")
                rt = bar.get("time") or 0
                if low is None or high is None or not rt:
                    continue
                for ev in _fvg_events:
                    if ev["symbol"] != sym or ev.get("retested"):
                        continue
                    if ev["side"] == "long" and low <= ev["top"]:
                        retest_px = low
                    elif ev["side"] == "short" and high >= ev["bottom"]:
                        retest_px = high
                    else:
                        continue
                    ev["retested"] = True
                    _fvg_retests.append({
                        "id": f"{ev['id']}:rt",
                        "symbol": sym,
                        "ticker": ev["ticker"],
                        "side": ev["side"],
                        "bar_time": ev["bar_time"],
                        "top": ev["top"],
                        "bottom": ev["bottom"],
                        "height": ev["height"],
                        "atr": ev["atr"],
                        "ratio": ev["ratio"],
                        "retest_time": rt,       # 当前实时 bar 时间
                        "retest_px": retest_px,  # 触发价（多=low / 空=high）
                    })
                    logger.info("FVG 首次回踩: %s %s %.2f~%.2f 触价 %.2f",
                                sym, ev["side"], ev["bottom"], ev["top"],
                                retest_px)
                    # 统一影子建档：限价挂区域边沿（多=top/空=bottom），
                    # 触及才成交；bar_time 用 FVG 形成 bar（回踩 bar 确认时
                    # 触及判定生效——本次回踩本身即首次触及，届时成交）
                    edge = ev["top"] if ev["side"] == "long" else ev["bottom"]
                    atr_rt = ev.get("atr")
                    if edge and atr_rt:
                        _shadow_signal(
                            "fvg_retest", sym, ev["side"], trigger_px=edge,
                            stop_px=_shadow_stop_for(data, ev["side"],
                                                     edge, atr_rt),
                            atr=atr_rt, bar_time=ev["bar_time"],
                            meta={"top": ev["top"], "bottom": ev["bottom"],
                                  "height": ev["height"], "ratio": ev["ratio"],
                                  "retest_px": retest_px},
                            fill_mode="limit", limit_px=edge,
                            dedup_key=f"fvg_retest:{ev['id']}")
            # 清理 4 小时前的旧事件（deque 按时间递增，头部弹出即可）
            cutoff = time.time() - FVG_EVENT_TTL_SECONDS
            while _fvg_events and _fvg_events[0]["bar_time"] < cutoff:
                _fvg_events.popleft()
            while _fvg_warnings and _fvg_warnings[0]["warn_time"] < cutoff:
                _fvg_warnings.popleft()
            for k in [k for k, ts in _fvg_warned.items() if ts < cutoff]:
                del _fvg_warned[k]
            while _fvg_retests and _fvg_retests[0]["bar_time"] < cutoff:
                _fvg_retests.popleft()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.exception("FVG 监控循环异常: %s", e)


def _feed_breakout(sym: str, bar_c: dict, data: dict, fed_times: set) -> None:
    """把一根已确认 bar（带对齐的 fvg_c.fvg_event）喂给突破跟踪器。"""
    fvg_c = data.get("fvg_c") or {}
    bar = {
        "time": bar_c.get("time"),
        "open": bar_c.get("open"), "high": bar_c.get("high"),
        "low": bar_c.get("low"), "close": bar_c.get("close"),
        "fvg_event": fvg_c.get("fvg_event") or 0,
    }
    fed_times.add(bar["time"])
    sig = breakout_tracker.on_bar(sym, bar)
    if sig:
        _emit_breakout(sig)


def _emit_breakout(sig: dict) -> None:
    """突破信号出口：固化进缓冲（前端快照下发）+ 写当日事件流。"""
    sig = dict(sig)
    sig["id"] = f"{sig['symbol']}:{sig['confirm_epoch']}:breakout"
    sig["ts"] = time.time()
    _breakout_signals.append(sig)
    record_event("breakout_signal", **{k: sig[k] for k in (
        "symbol", "ticker", "side", "confirm_hm", "confirm_time",
        "break_time", "close", "premarket_high", "premarket_low",
        "zone_width", "crowd", "triple")})
    logger.warning("盘前突破信号: %s %s 确认 %s 突破 %s 收盘 %.2f 盘前 %.2f~%.2f"
                   "（拥挤 %d%s）", sig["symbol"], sig["side"],
                   sig["confirm_time"], sig["break_time"], sig["close"],
                   sig["premarket_low"], sig["premarket_high"], sig["crowd"],
                   "，三连确认" if sig["triple"] else "")
    # 统一影子建档：盘前突破/跌破市价成交（确认 bar 收盘价），
    # meta 带 crowd/triple/盘前区间
    sym = sig["symbol"]
    side = "long" if sig["side"] == "站上高点" else "short"
    data = (collector.latest() or {}).get(sym, {})
    bar_c = data.get("bar_c") or {}
    close = (bar_c.get("close") if bar_c.get("time") == sig.get("confirm_epoch")
             else None) or sig["close"]
    ts_c = data.get("ts_c") or data.get("ts") or {}
    atr = ts_c.get("atr")
    if close and atr:
        _shadow_signal(
            "breakout", sym, side, trigger_px=close,
            stop_px=_shadow_stop_for(data, side, close, atr),
            atr=atr, bar_time=sig["confirm_epoch"],
            meta={"crowd": sig["crowd"], "triple": sig["triple"],
                  "premarket_high": sig["premarket_high"],
                  "premarket_low": sig["premarket_low"],
                  "zone_width": sig["zone_width"],
                  "break_time": sig["break_time"], "side_text": sig["side"]},
            dedup_key=f"breakout:{sig['id']}")


async def _breakout_loop() -> None:
    """盘前突破+FVG确认 流式信号（每 1s）：某 symbol 的 bar_c.time 前进
    （= 新 5M bar 收盘确认）时把这根已确认 bar 喂给 BreakoutTracker；
    fvg_c 与 bar_c 的 time 对齐校验同 FVG 固化（不齐挂起最多 5 拍）。
    同一拍全部 symbol 喂完后按 bar 时间调 flush 做拥挤终判出信号。
    盘前区间优先用「5M 进攻区域」指标（attack_c 与 bar_c 的 time 对齐
    且 pmh/pml 有效时注入，每天一次；不对齐本拍跳过下拍自然重试；
    门控 bar 时间 hm>=570——指标盘前输出是累积中的半成品，09:30 首根
    常规K线才锁定终值，盘前启动时提前注入会把半成品区间锁死一整天）——
    指标在 TV 侧自算盘前区间，盘中重启控制台也能拿到正确区间。"""
    from zoneinfo import ZoneInfo
    import datetime as _dt
    et = ZoneInfo("America/New_York")
    while True:
        await asyncio.sleep(1.0)
        try:
            market = collector.latest()
            if not market:
                continue
            fed_times: set = set()
            for sym, data in market.items():
                bar_c = data.get("bar_c") or {}
                bt = bar_c.get("time") or 0
                if not bt:
                    continue
                # ---- 进攻区域指标盘前区间注入（权威来源） ----
                # 指标的 premarketHigh/Low 在盘前时段是【累积中的半成品】
                # （实测 pml 一路下探，09:30 第一根常规K线才锁定终值），
                # 因此注入门控在 hm >= 570：盘前启动 → 09:30 首根才注；
                # 开盘后启动 → 第一拍即注。否则盘前启动会把半成品区间
                # 注进去且一天不再更新，全天区间错误。
                atk = data.get("attack_c") or {}
                pmh, pml = atk.get("pmh"), atk.get("pml")
                dt_et = _dt.datetime.fromtimestamp(bt, et)
                if (atk.get("time") == bt and pmh is not None
                        and pml is not None and pmh > pml > 0
                        and dt_et.hour * 60 + dt_et.minute >= 570):
                    day = dt_et.strftime("%Y-%m-%d")
                    if _breakout_injected.get(sym) != day:
                        if breakout_tracker.set_premarket(sym, pmh, pml, day):
                            _breakout_injected[sym] = day
                            logger.info("盘前区间注入（进攻区域指标）: %s %.2f~%.2f",
                                        sym, pml, pmh)
                # 先冲刷挂起的待喂 bar（等 daemon 的 FVG study 追上 K线）
                pend = _breakout_pending.get(sym)
                if pend is not None:
                    p_bt, p_bar, retries = pend
                    fvg_c = data.get("fvg_c") or {}
                    if fvg_c.get("time") == p_bt:
                        _breakout_pending.pop(sym, None)
                        _feed_breakout(sym, p_bar, data, fed_times)
                    elif retries <= 0:
                        _breakout_pending.pop(sym, None)
                        logger.warning("突破信号喂入超时放弃: %s bar_time=%s"
                                       "（fvg_c 始终未对齐）", sym, p_bt)
                    else:
                        _breakout_pending[sym] = (p_bt, p_bar, retries - 1)
                prev = _breakout_last_bar.get(sym)
                if prev is None:
                    _breakout_last_bar[sym] = bt   # 首次见仅建基线，不补录历史
                    continue
                if bt <= prev:
                    continue
                _breakout_last_bar[sym] = bt
                fvg_c = data.get("fvg_c") or {}
                fvg_t = fvg_c.get("time")
                if fvg_t is not None and fvg_t != bt:
                    _breakout_pending[sym] = (bt, dict(bar_c), 5)
                    continue
                _feed_breakout(sym, bar_c, data, fed_times)
            for t in sorted(fed_times):
                for sig in breakout_tracker.flush(t):
                    _emit_breakout(sig)
            # 清理 4 小时前的旧信号（与 FVG 事件缓冲同 TTL）
            cutoff = time.time() - FVG_EVENT_TTL_SECONDS
            while _breakout_signals and _breakout_signals[0]["ts"] < cutoff:
                _breakout_signals.popleft()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.exception("盘前突破监控循环异常: %s", e)


async def _auto_loop() -> None:
    """EMA9 金字塔调度：实时 e9 触线建仓，确认 e9_c 管理整组仓位。"""
    from zoneinfo import ZoneInfo
    import datetime as _dt
    et = ZoneInfo("America/New_York")
    adopt_tick = 0
    while True:
        await asyncio.sleep(1.0)
        try:
            market = collector.latest()
            if not market:
                continue
            now = _dt.datetime.now(et)
            now_hm = now.hour * 60 + now.minute
            trading = _is_trading_hours()
            if trading and collector.data_health()["stale"]:
                continue
            for sym, data in market.items():
                bar_c = data.get("bar_c") or {}
                bt = bar_c.get("time") or 0
                if bt and _auto_last_bar.get(sym) != bt:
                    _auto_last_bar[sym] = bt
                    try:
                        await auto_engine.on_bar(sym, bar_c, data.get("e9_c") or {})
                    except Exception:
                        logger.exception("auto on_bar 异常 %s", sym)
                if trading:
                    bar = data.get("bar") or {}
                    low, high = bar.get("low"), bar.get("high")
                    if low or high:
                        try:
                            await auto_engine.on_price_extremes(
                                sym, low, high, data.get("e9") or {}, now_hm)
                        except Exception:
                            logger.exception("auto on_price 异常 %s", sym)
            adopt_tick += 1
            if adopt_tick >= 5:
                adopt_tick = 0
                try:
                    adopted = await auto_engine.adopt_manual_lots(
                        order_mgr.list_lots(active_only=True), signals=market)
                    for sym in adopted:
                        data = market.get(sym) or {}
                        bar_c = data.get("bar_c") or {}
                        if bar_c:
                            await auto_engine.on_bar(sym, bar_c, data.get("e9_c") or {})
                except Exception:
                    logger.exception("auto 收养扫描异常")
            # 统一影子建档：自动策略开仓候选（ST/EMA 回踩限价单，触及才成交）
            try:
                for p in auto_engine.proposals():
                    pid = p.get("proposal_id")
                    if not pid or pid in _shadow_proposal_recorded:
                        continue
                    sym = p.get("symbol")
                    data = market.get(sym) or {}
                    bt = p.get("bar_time") or (data.get("bar_c") or {}).get("time")
                    atr = p.get("atr") or abs((p.get("limit_px") or 0)
                                              - (p.get("stop_px") or 0))
                    if not (sym and bt and p.get("limit_px") and atr):
                        continue
                    tr = _shadow_signal(
                        "proposal", sym, p.get("side") or "long",
                        trigger_px=p["limit_px"],
                        stop_px=p.get("stop_px") or 0,
                        atr=atr, bar_time=bt,
                        meta={"strategy": p.get("strategy"),
                              "line_type": p.get("line"),
                              "divergence": p.get("divergence"),
                              "st_duration_bars": p.get("st_duration_bars"),
                              "st_push_count": p.get("st_push_count")},
                        fill_mode="limit", limit_px=p["limit_px"],
                        dedup_key=f"proposal:{pid}")
                    if tr is not None:
                        _shadow_proposal_recorded.add(pid)
            except Exception:
                logger.exception("影子建档（策略候选）异常")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.exception("全自动策略循环异常: %s", e)


async def _midpoint_limit_reprice_loop() -> None:
    """中点限价单监控循环（每秒）。

    2026-10-20 起中点单一次定价不再逐 bar 改价：OCA 双腿（LMT 中点 +
    STP 突破）挂出后价格固定，直到成交/撤单/收盘。本循环仅为旧式
    单腿中点单（无突破腿）保留"突破升级市价"的兜底。"""
    while True:
        await asyncio.sleep(1.0)
        try:
            pending = [l for l in order_mgr.list_lots(active_only=True)
                       if l["state"] == "PENDING"
                       and l.get("limit_type") == "midpoint"
                       and not l.get("breakout_escalated")
                       and not l.get("breakout_order_id")]
            if not pending:
                continue
            market = collector.latest() or {}
            for lot in pending:
                data = market.get(lot["symbol"]) or {}
                bar = data.get("bar_c") or {}
                # 旧式单腿中点单的突破升级兜底；OCA 双腿单由券商端
                # STP 突破腿原生处理，不走这里。
                cur = data.get("bar") or {}
                high, low = bar.get("high"), bar.get("low")
                cur_high, cur_low = cur.get("high"), cur.get("low")
                triggered = False
                if high is not None and low is not None:
                    if (lot["side"] == "long" and cur_high is not None
                            and cur_high > high):
                        triggered = True
                    elif (lot["side"] == "short" and cur_low is not None
                            and cur_low < low):
                        triggered = True
                if triggered:
                    bt0 = bar.get("time") or 0
                    esc_key = (lot["lot_id"], bt0)
                    if esc_key not in _midpoint_escalate_attempted:
                        _midpoint_escalate_attempted.add(esc_key)
                        ok, error = await order_mgr.escalate_midpoint_to_market(
                            lot["lot_id"])
                        record_event(
                            "midpoint_breakout_escalated" if ok
                            else "midpoint_breakout_escalate_failed",
                            lot_id=lot["lot_id"], symbol=lot["symbol"],
                            side=lot["side"], trigger_high=high,
                            trigger_low=low, cur_high=cur_high,
                            cur_low=cur_low, bar_time=bt0, error=error)
                        if ok:
                            logger.warning(
                                "%s 中点单未等到回调，价格突破上根K线%s，"
                                "已升级为市价单", lot["symbol"],
                                "高点" if lot["side"] == "long" else "低点")
                        else:
                            logger.warning("%s 中点单突破升级失败: %s",
                                           lot["symbol"], error)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.exception("中点限价单改价循环异常: %s", e)


async def _retest_limit_reprice_loop() -> None:
    """回踩限价单逐 bar 改价循环（每秒检查）。

    每根新确认 5M bar 收盘后 bar_c2 随之前移，对 PENDING 的回踩 lot
    用新 bar_c2 重算限价/止损（多 limit=bar_c2.high / stop=bar_c2.low，
    空头镜像），并按固定风险预算（lot.init_risk_usd，下单时的计划风险
    金额，含仓位%封顶效果，作为锚点不动）以新止损距离重新以损定量：
    qty_new = floor(init_risk_usd / |new_limit − new_stop|)，封顶
    RISK_MAX_ORDER_QTY；仅 filled_qty == 0 时改股数（部分成交后只改价）。
    仅 PENDING、每 bar 每 lot 最多一次（reprice_bar_time 幂等）、
    失败保留原单下根重试，ACTIVE 后自动停止。"""
    while True:
        await asyncio.sleep(1.0)
        try:
            pending = [l for l in order_mgr.list_lots(active_only=True)
                       if l["state"] == "PENDING"
                       and l.get("limit_type") == "retest"]
            if not pending:
                continue
            market = collector.latest() or {}
            for lot in pending:
                data = market.get(lot["symbol"]) or {}
                bar2 = data.get("bar_c2") or {}
                bt = bar2.get("time") or 0
                # bar_c2 未前移（无新确认 bar）或本 bar 已尝试过改价 → 跳过
                if not bt or bt <= (lot.get("reprice_bar_time") or 0):
                    continue
                try:
                    new_limit, new_stop, _ = _retest_prices(data, lot["side"])
                except ValueError as e:
                    logger.warning("%s 回踩单改价跳过: %s", lot["symbol"], e)
                    continue
                # 固定风险预算重新以损定量（部分成交后不改股数，传 0）
                qty_new = 0
                if not lot.get("filled_qty"):
                    init_risk = lot.get("init_risk_usd") or 0
                    dist = abs(new_limit - new_stop)
                    if init_risk > 0 and dist > 0:
                        qty_new = int(init_risk / dist)
                        if config.RISK_MAX_ORDER_QTY > 0:
                            qty_new = min(qty_new, config.RISK_MAX_ORDER_QTY)
                        # 净值占比封顶（下单时的 max_position_pct；止损距离收窄
                        # 时防止名义市值超出下单口径）
                        max_pct = lot.get("max_position_pct") or 0
                        equity = (_equity_cache.get("equity") or 0)
                        if max_pct > 0 and equity > 0 and new_limit > 0:
                            qty_new = min(qty_new,
                                          int(equity * max_pct / 100 / new_limit))
                    else:
                        qty_new = lot["qty"]   # 无风险锚点（历史单）：股数不动
                ok, error = await order_mgr.reprice_retest_lot(
                    lot["lot_id"], new_limit, new_stop, bt, new_qty=qty_new)
                if ok and error == "unchanged":
                    continue   # 价格股数均未变，未发起修改，不记事件
                cur = order_mgr.get(lot["lot_id"])
                record_event(
                    "retest_limit_repriced" if ok
                    else "retest_limit_reprice_failed",
                    lot_id=lot["lot_id"], symbol=lot["symbol"],
                    side=lot["side"], limit_px=new_limit, stop_px=new_stop,
                    qty=(cur.qty if cur is not None else lot["qty"]),
                    bar_time=bt, error=error)
                if not ok:
                    logger.warning(
                        "%s 回踩单改价失败（保留原挂单，下根 bar 重试）: %s",
                        lot["symbol"], error)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.exception("回踩限价单改价循环异常: %s", e)


def record_event(event_type: str, **data) -> None:
    """追加一条事件到当日 JSONL 日志（持久化，可回溯分析）。"""
    import json as _json
    try:
        day = time.strftime("%Y%m%d")
        path = Path(__file__).resolve().parent.parent / "logs" / f"events-{day}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        rec = {"ts": time.time(), "type": event_type, **data}
        with open(path, "a", encoding="utf-8") as f:
            f.write(_json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception as e:
        logger.warning("事件记录失败: %s", e)


# 统一告警出口（engine/notifier.py）：record_event 延迟绑定（调用时才解析
# 模块函数，测试可 monkeypatch）；iMessage 收件人读全局设置，未配置跳过
notifier: Notifier = Notifier(
    record_event=lambda t, **d: record_event(t, **d),
    imessage_recipient=lambda: str(_settings.get("imessage_recipient") or ""))


def _snapshot_for(symbol: str) -> dict:
    """下单时抓取该标的的 trend_state 快照（复盘用）。"""
    snap = (collector.latest() or {}).get(symbol, {})
    return {
        "ts": snap.get("ts") or {},                    # 实时值（显示）
        "ts_c": snap.get("ts_c") or snap.get("ts") or {},   # 确认值（策略）
        "close": (snap.get("bar") or {}).get("close"),
    }


def compute_screen() -> dict:
    """横截面选股（双向）：SuperTrend 方向（ts_c.direction）1 进多头列、-1 进空头列；
    震荡（trend_state=0）但 ST 方向明确也列入。发散度仅作列内强→弱排序展示。
    盘前区间过滤（settings.screen_premarket_filter，默认关）：多头候选现价
    <= attack_c.pmh 剔除、空头候选现价 >= attack_c.pml 剔除；无 attack 数据
    的标的一律保留（不误杀）。现价 = collector 实时 bar close。"""
    market = collector.latest()
    pm_filter = _screen_premarket_filter()
    long_rows, short_rows = [], []
    for symbol, data in market.items():
        # 策略用「确认值」（上一根已收盘），稳定不抖
        ts = _ts_c(data)
        direction = ts.get("direction")
        close = (data.get("bar") or {}).get("close")
        if direction not in (1, -1) or close is None:
            continue
        pmh, pml = _attack_zone(data)
        if pm_filter and not _premarket_pass(
                "long" if direction == 1 else "short", close, pmh, pml):
            continue
        divergence = ts.get("divergence")
        if divergence is None:
            ema_f, ema_s, atr = ts.get("ema_fast"), ts.get("ema_slow"), ts.get("atr")
            if ema_f is not None and ema_s is not None and atr and atr > 0:
                divergence = abs(ema_f - ema_s) / atr
        # FVG 同 ts 回退：确认值优先，缺失为 None
        fvg = data.get("fvg_c") or data.get("fvg") or {}
        row = {
            "symbol": symbol,
            "ticker": symbol.split(":")[-1],
            "side": "long" if direction == 1 else "short",
            "trend_state": ts.get("trend_state"),
            "divergence": (round(abs(divergence), 2)
                           if divergence is not None else None),
            "st_duration_bars": ts.get("st_duration_bars"),
            "st_push_count": ts.get("st_push_count"),
            "fvg_cum_count": fvg.get("fvg_cum_count"),
            "fvg_last_dir": fvg.get("fvg_last_dir"),
            "fvg_event": fvg.get("fvg_event"),
            "close": round(close, 2),
            "pmh": round(pmh, 2) if pmh is not None else None,   # 盘前高（进攻区域）
            "pml": round(pml, 2) if pml is not None else None,   # 盘前低
            "qualified": True,     # 已过 ST 方向过滤（前端兼容字段）
        }
        (long_rows if direction == 1 else short_rows).append(row)
    long_rows.sort(key=lambda x: -(x["divergence"] or 0))
    short_rows.sort(key=lambda x: -(x["divergence"] or 0))
    return {"long": long_rows, "short": short_rows}


def _fvg_event_state(side: str, close: Optional[float],
                     top: float, bottom: float) -> str:
    """由现价实时计算缺口状态：open=未回踩 / retest=回踩中 / filled=已回补。
    多头：现价 ≤ top 回踩中，≤ bottom 已回补；空头镜像。"""
    if close is None:
        return "open"
    if side == "long":
        if close <= bottom:
            return "filled"
        if close <= top:
            return "retest"
        return "open"
    if close >= top:
        return "filled"
    if close >= bottom:
        return "retest"
    return "open"


def _fvg_monitor_snapshot() -> dict:
    """FVG 监控快照：以全部 symbol 的最大 bar_c.time 为锚，向前按 300s 步进
    生成最近 12 个 bar 槽位（60 分钟），缓冲内事件实时计算回踩状态后入槽。
    盘前区间过滤（settings.fvg_premarket_filter，默认关）：多头事件现价
    <= attack_c.pmh 剔除、空头事件现价 >= attack_c.pml 剔除（槽位事件、
    首次回踩、预警同口径）；无 attack 数据的一律保留。现价=实时 bar close。"""
    market = collector.latest() or {}
    pm_filter = _fvg_premarket_filter()
    anchor = 0
    closes: dict = {}
    zones: dict = {}
    for sym, data in market.items():
        bt = (data.get("bar_c") or {}).get("time") or 0
        if bt > anchor:
            anchor = bt
        closes[sym] = (data.get("bar") or {}).get("close")
        zones[sym] = _attack_zone(data)
    if not anchor:
        return {"slots": [], "retests": [], "warnings": []}

    def _pm_ok(side: str, sym: str) -> bool:
        if not pm_filter:
            return True
        pmh, pml = zones.get(sym, (None, None))
        return _premarket_pass(side, closes.get(sym), pmh, pml)

    slot_map = {anchor - i * 300: {"bar_time": anchor - i * 300,
                                   "long": [], "short": []}
                for i in range(12)}
    oldest = anchor - 11 * 300
    for ev in _fvg_events:
        bt = ev["bar_time"]
        if bt < oldest or bt > anchor:
            continue
        # atr / 边沿缺失的事件跳过不入槽
        if ev.get("atr") is None or ev.get("top") is None \
                or ev.get("bottom") is None:
            continue
        if not _pm_ok(ev["side"], ev["symbol"]):
            continue
        slot = slot_map.get(bt)
        if slot is None:
            continue
        item = dict(ev)
        item["state"] = _fvg_event_state(ev["side"], closes.get(ev["symbol"]),
                                         ev["top"], ev["bottom"])
        item["retested"] = bool(ev.get("retested"))   # 前端标记「已回踩」用
        slot[ev["side"]].append(item)
    slots = sorted(slot_map.values(), key=lambda s: -s["bar_time"])
    # 首次回踩记录按回踩时间倒序（最新在前），最多 50 条
    retests = sorted((r for r in _fvg_retests if _pm_ok(r["side"], r["symbol"])),
                     key=lambda r: r.get("retest_time") or 0,
                     reverse=True)[:50]
    # 预警（形成中）按预警时间倒序，最多 20 条（前端去重弹窗用）
    warnings = sorted((w for w in _fvg_warnings if _pm_ok(w["side"], w["symbol"])),
                      key=lambda w: w.get("warn_time") or 0,
                      reverse=True)[:20]
    return {"slots": slots, "retests": retests, "warnings": warnings}


def _total_position_pct(equity) -> Optional[float]:
    """全部开放 lot 市值合计 ÷ 净值 ×100；净值为空返回 None。

    市值口径：PENDING 用 qty×entry_px（计划值）；ACTIVE/CLOSING 用
    filled_qty×last_px（last_px≤0 时回退 avg_fill/entry_px）。
    """
    if not equity or equity <= 0:
        return None
    amount = 0.0
    for lot in order_mgr.list_lots(active_only=True):
        if lot["state"] == "PENDING":
            amount += lot["qty"] * lot["entry_px"]
        else:
            px = lot["last_px"] or lot["avg_fill"] or lot["entry_px"]
            amount += lot["filled_qty"] * px
    return round(amount / equity * 100, 1)


async def build_snapshot() -> dict:
    status = await get_status_cached()
    open_symbols = len({l["symbol"] for l in order_mgr.list_lots(active_only=True)})
    return {
        "type": "snapshot",
        "ts": time.time(),
        "status": {
            "execution_enabled": bool(gateway.enabled),
            "connected": bool(gateway._connected),
            "dry_run": not bool(gateway.enabled),
            "equity": status["equity"],
            "daily_pnl": status["daily_pnl"],
            "halted": risk_gate.is_halted(),
            "open_symbols": open_symbols,
            "total_risk_usd": order_mgr.risk_exposure()["total"],  # 总风险敞口（顶栏「风险 $X/预算」）
            "max_total_risk_usd": config.RISK_MAX_TOTAL_RISK_USD,  # 总风险敞口预算（0=不限制）
            "total_position_pct": _total_position_pct(status["equity"]),  # 全部持仓市值 ÷ 净值 %
            "max_order_qty": config.RISK_MAX_ORDER_QTY,  # 单笔股数硬顶（前端封顶用）
            "allow_short": config.ALLOW_SHORT,  # 是否允许做空
            "fvg_chase_max_ratio": config.FVG_CHASE_MAX_ATR_RATIO,  # 追价最大偏离（×ATR）
            "stop_atr_mult": _stop_atr_mult(),  # 全局止损 ATR 倍数（前端预览/下单同口径）
            "screen_premarket_filter": _screen_premarket_filter(),  # 选股盘前过滤开关
            "fvg_premarket_filter": _fvg_premarket_filter(),  # FVG 监控盘前过滤开关
            "news": _news_status(),  # 盘前新闻小摘要（徽标联动 hot ticker→gist）
            "imessage_recipient": str(_settings.get("imessage_recipient") or ""),  # iMessage 告警收件人（未配置跳过）
            "market_age": (None if collector._last_content_update == 0
                           else round(collector.age(), 1)),
            "market_transport_age": collector.data_health()["transport_age"],
            "market_bar_age": collector.data_health()["bar_age"],
            "market_stale": collector.data_health()["stale"],
            "market_stale_reason": collector.data_health()["reason"],
            "market_running": collector.running,
            "data_time": collector.latest_bar_time,   # TV 数据时钟（最新 K 线时间）
            "server_time": time.time(),               # 本地时钟
            "ibkr_time": gateway.ibkr_time,           # IBKR 服务器时钟
            "bar_update_delay": collector.bar_update_delay,  # K线收盘→收到 的延时（秒）
            "tv_study": collector.study_health(),  # 指标通道健康（systemic=全体故障）
            "tv_view": _tv_view_info,             # TV 画面位（panes/slots，启动校验）
        },
        "market": collector.latest(),
        "positions": order_mgr.aggregate(),
        "lots": order_mgr.list_lots(),
        "logs": list(_log_buffer),
        "screen": compute_screen(),
        "scan_alerts": sorted(_scan_alerts.values(),
                              key=lambda x: x.get("scanned_at", 0), reverse=True),
        "stop_alerts": sorted(_stop_alerts.values(),
                              key=lambda x: x.get("checked_at", 0), reverse=True),
        # 外部（IBKR 手动）订单/持仓检测告警（清理循环告警模式；自动清理开启时为空）
        "external_alerts": list(_external_alerts),
        "slow_break_alerts": sorted(_slow_break_alerts.values(),
                                    key=lambda x: x.get("alerted_at", 0), reverse=True),
        "auto_close_notices": sorted(_auto_close_notices.values(),
                                     key=lambda x: x.get("closed_at", 0), reverse=True),
        # 持仓反向 FVG 守卫：提醒（锁利/平仓失败）+ 自动平仓记录
        "fvg_reverse_alerts": sorted(_fvg_reverse_alerts.values(),
                                     key=lambda x: x.get("alerted_at", 0), reverse=True),
        "fvg_reverse_notices": sorted(_fvg_reverse_notices.values(),
                                      key=lambda x: x.get("closed_at", 0), reverse=True),
        "shadow": {"stats": shadow_tracker.stats(),
                   "events": shadow_tracker.events(100)},
        "fvg_monitor": _fvg_monitor_snapshot(),
        "breakout_signals": list(_breakout_signals),   # 盘前突破信号（前端信号栏）
        "alerts": notifier.active_alerts(),            # 活跃告警（前端横幅+声音）
        "auto": {"enabled": _auto_enabled, "strategy": _auto_strategy_name,
                 "instances": _auto_instances(), "proposals": []},
        "clients": _clients_snapshot(),
        "copy": _copy_snapshot(),
    }


def _tv_last_price(symbol: str) -> Optional[float]:
    """TV 现价（唯一价格源）：collector 实时 bar close；不可用返回 None。
    全系统统一用 TV，不依赖 TWS 行情权限（10089 延时通道）。"""
    data = (collector.latest() or {}).get(symbol) or {}
    close = (data.get("bar") or {}).get("close")
    return close if close and close > 0 else None


def _refresh_marks_from_tv() -> None:
    """用 TV 实时价刷新 ACTIVE lot 的 mark 价/PnL/MAE/MFE（每秒调用，不落盘）。
    替代昂贵的 IBKR last_price 轮询——collector 里本来就有全标的实时收盘价。"""
    prices: dict = {}
    for sym, data in collector.latest().items():
        bar = data.get("bar") or {}
        close = bar.get("close")
        if close and close > 0:
            prices[sym] = close
    if prices:
        order_mgr.apply_marks(prices, save=False)


async def _broadcast_loop() -> None:
    """每 1s 广播一次快照（含 TV 实时数据）。"""
    while True:
        try:
            _refresh_marks_from_tv()
            snapshot = await build_snapshot()
            dead = []
            for ws in list(_ws_clients):
                try:
                    await ws.send_json(snapshot)
                except Exception:
                    dead.append(ws)
            for ws in dead:
                _ws_clients.discard(ws)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.exception("广播循环异常: %s", e)
        await asyncio.sleep(1.0)


# ---------------------------------------------------------------------------
# 生命周期
# ---------------------------------------------------------------------------

async def _eod_force_close_loop() -> None:
    """15:45 美东时间强制平掉所有活跃仓位（日内不过夜）。"""
    from zoneinfo import ZoneInfo
    import datetime as _dt
    et = ZoneInfo("America/New_York")
    done_days: set = set()
    while True:
        await asyncio.sleep(30.0)
        try:
            now_et = _dt.datetime.now(et)
            if now_et.hour == 15 and now_et.minute >= 45 and now_et.minute < 59:
                day_key = now_et.strftime("%Y%m%d")
                if day_key in done_days:
                    continue
                active = order_mgr.list_lots(active_only=True)
                logger.warning("EOD 强平触发：%d 个活跃仓位", len(active))
                all_ok = True
                for lot in active:
                    if lot["state"] == "PENDING":
                        ok, err = await order_mgr.cancel_lot(lot["lot_id"])
                    elif lot["state"] == "ACTIVE":
                        ok, err = await order_mgr.close_lot(lot["lot_id"])
                    else:
                        continue   # CLOSING 由 close watcher 完成，不重复发单
                    if not ok:
                        all_ok = False
                    logger.info("EOD 处理 %s: %s", lot["lot_id"],
                                "成功" if ok else f"失败({err})")
                if all_ok:
                    done_days.add(day_key)   # 失败时保留下轮重试，已 CLOSING 的不会重复发单
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.exception("EOD 强平循环异常: %s", e)


async def _reconcile_loop() -> None:
    """每 60s 订单对账：重挂丢失的 watcher、补登系统外平仓记录
    （重启 / 断线重连 / TWS 手动操作后的自愈）。"""
    while True:
        await asyncio.sleep(60.0)
        try:
            await order_mgr.reconcile()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.exception("订单对账循环异常: %s", e)


def _on_lot_closed(d: dict) -> None:
    """保护单触发 / 对账补登的平仓事件：完整字段落事件流，供复盘。"""
    record_event("lot_closed", **{k: d.get(k) for k in (
        "lot_id", "symbol", "side", "qty", "filled_qty", "avg_fill",
        "exit_px", "realized_pnl", "actual_rr", "target_rr", "mae", "mfe",
        "exit_reason", "limit_type", "entry_time", "closed_at", "reason")})


def _on_lot_revived(d: dict) -> None:
    """OCA 竞态漏单后的对账复活：落事件流，供审计回溯。"""
    record_event("lot_revived", **{k: d.get(k) for k in (
        "lot_id", "symbol", "side", "qty", "filled_qty", "avg_fill",
        "stop_px", "limit_type", "note")})


def _startup_status() -> dict:
    snap = collector.latest() or {}
    total = len(snap)
    confirmed = sum(1 for v in snap.values()
                    if isinstance(v.get("ts_c"), dict)
                    and v["ts_c"].get("trend_state") is not None)
    expected = len(config.WATCHLIST)
    market_ready = collector.running and total >= expected and confirmed >= total
    ibkr_required = bool(gateway.enabled)
    ibkr_ready = bool(gateway._connected) or not ibkr_required
    core_ready = bool(_startup_state["core_ready"])
    browser_ready = bool(_startup_state["browser_ready"])
    stages = [
        {"key": "core", "label": "交易服务", "ready": core_ready,
         "detail": "风控、订单与后台任务已启动" if core_ready else "正在启动"},
        {"key": "ibkr", "label": "IBKR", "ready": ibkr_ready,
         "detail": ("TWS 已连接" if gateway._connected else
                    "DRY-RUN 模式" if not ibkr_required else "等待 TWS 连接（7496）")},
        {"key": "market", "label": "行情与指标", "ready": market_ready,
         "detail": f"确认指标 {confirmed}/{expected}"},
        {"key": "browser", "label": "图表工作区", "ready": browser_ready,
         "detail": ("控制台与 TradingView 双标签页已就绪" if browser_ready
                    else _startup_state["browser_error"] or "正在准备浏览器标签页")},
    ]
    errors = []
    if _startup_state["browser_error"] and not browser_ready:
        errors.append(_startup_state["browser_error"])
    if collector.study_health().get("systemic") and not market_ready:
        errors.append("TradingView 指标通道异常，请运行 python3 service/tv_diag.py 检查")
    return {
        "ready": bool(_startup_state["ready"]), "stages": stages,
        "error": "；".join(errors), "elapsed": round(time.time() - _startup_state["started_at"]),
        "all_ready": all(s["ready"] for s in stages),
    }


@app.on_event("startup")
async def on_startup() -> None:
    global _broadcast_task, _eod_task, _scan_task, _stop_guard_task, _reconcile_task
    global _shadow_task, _auto_task, _midpoint_reprice_task, _external_cleanup_task
    global _slow_break_task, _auto_breakeven_task, _fvg_task, _retest_reprice_task
    global _breakout_task, _naked_position_task, _health_watch_task
    global _report_task, _precheck_task, _fvg_reverse_task, _tv_check_task, _startup_task
    global _system_stopping
    _system_stopping = False
    config.validate_config()
    if not acquire_writer_lock("logs/autotrade_writer.lock"):
        raise RuntimeError("另一个 autotrade 执行写进程正在运行，禁止重复管理订单")
    connected = await gateway.connect()
    if config.EXECUTION_ENABLED and not connected:
        logger.error("EXECUTION_ENABLED=1 但 IBKR 未连接，将持续重连，就绪后进入主页面")
        gateway._start_reconnect()
    elif not connected:
        logger.info("IBKR 未连接，运行于纯 dry-run（虚拟成交）")
    order_mgr.on_lot_closed = _on_lot_closed
    order_mgr.on_lot_revived = _on_lot_revived
    order_mgr.price_provider = _tv_last_price   # 平仓参考价统一用 TV（唯一价格源）
    if connected:
        await order_mgr.reconcile()
    await collector.start()
    await copy_supervisor.start(_load_copy_servers())
    _broadcast_task = asyncio.create_task(_broadcast_loop())
    _eod_task = asyncio.create_task(_eod_force_close_loop())
    _scan_task = asyncio.create_task(_position_scan_loop())
    _stop_guard_task = asyncio.create_task(_stop_guard_loop())
    _slow_break_task = asyncio.create_task(_slow_break_loop())
    _auto_breakeven_task = asyncio.create_task(_auto_breakeven_loop())
    _reconcile_task = asyncio.create_task(_reconcile_loop())
    _shadow_task = asyncio.create_task(_shadow_loop())
    _auto_task = asyncio.create_task(_auto_loop())
    _midpoint_reprice_task = asyncio.create_task(_midpoint_limit_reprice_loop())
    _retest_reprice_task = asyncio.create_task(_retest_limit_reprice_loop())
    _external_cleanup_task = asyncio.create_task(_external_cleanup_loop())
    _fvg_task = asyncio.create_task(_fvg_monitor_loop())
    _fvg_reverse_task = asyncio.create_task(_fvg_reverse_guard_loop())
    _breakout_task = asyncio.create_task(_breakout_loop())
    _naked_position_task = asyncio.create_task(_naked_position_loop())
    _health_watch_task = asyncio.create_task(_health_watch_loop())
    _tv_check_task = asyncio.create_task(_tv_startup_check())
    _report_task = asyncio.create_task(_report_loop())
    _precheck_task = asyncio.create_task(_precheck_loop())
    _startup_state["core_ready"] = True
    _startup_task = asyncio.create_task(_startup_prepare())


@app.on_event("shutdown")
async def on_shutdown() -> None:
    for t in (_broadcast_task, _eod_task, _scan_task,
              _stop_guard_task, _reconcile_task, _shadow_task, _auto_task,
              _midpoint_reprice_task, _retest_reprice_task,
              _external_cleanup_task, _slow_break_task,
              _auto_breakeven_task, _fvg_task, _fvg_reverse_task, _breakout_task,
              _naked_position_task, _health_watch_task, _report_task,
              _precheck_task, _tv_check_task, _startup_task):
        if t:
            t.cancel()
    await copy_supervisor.stop_all()
    await collector.stop()
    await gateway.disconnect()


_TWS_PROCESS_PATTERN = "/Trader Workstation/Trader Workstation.app/Contents/MacOS/JavaApplicationStub"


async def _tws_pids() -> List[int]:
    proc = await asyncio.create_subprocess_exec(
        "pgrep", "-f", _TWS_PROCESS_PATTERN,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    out, _ = await proc.communicate()
    return sorted(int(pid) for pid in out.decode().split())


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


async def _wait_tws_exit(pids: List[int], timeout: float) -> List[int]:
    deadline = time.monotonic() + timeout
    remaining = [pid for pid in pids if _pid_alive(pid)]
    while remaining and time.monotonic() < deadline:
        await asyncio.sleep(0.25)
        remaining = [pid for pid in remaining if _pid_alive(pid)]
    return remaining


async def _close_tws_process() -> bool:
    """关闭 TWS 主进程；先正常退出，超时后 TERM/KILL 确保不残留。"""
    pids = await _tws_pids()
    if not pids:
        return True
    try:
        await _osascript('tell application "Trader Workstation" to quit')
    except Exception as e:
        logger.warning("TWS 正常退出请求失败，将直接结束进程: %s", e)
    remaining = await _wait_tws_exit(pids, 8.0)
    if remaining:
        for pid in remaining:
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        remaining = await _wait_tws_exit(remaining, 3.0)
    if remaining:
        for pid in remaining:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        remaining = await _wait_tws_exit(remaining, 2.0)
    if remaining:
        logger.error("TWS 进程仍未退出: %s", remaining)
        return False
    logger.info("TWS 进程已关闭")
    return True


def _schedule_console_exit() -> None:
    async def _exit() -> None:
        await asyncio.sleep(0.5)   # 先让 shutdown 响应到达浏览器
        os.kill(os.getpid(), signal.SIGTERM)
    asyncio.create_task(_exit())


@app.post("/api/system/shutdown")
async def api_system_shutdown(request: Request) -> dict:
    """仅本机：关交易门 → 停跟单 → 关 TWS → 退出控制台。"""
    global _system_stopping
    if not _is_local(request):
        raise HTTPException(403, "仅本机（master）可关闭系统")
    if _system_stopping:
        return {"ok": True, "stopping": True, "tws_closed": None, "errors": []}
    _system_stopping = True
    logger.warning("收到手动关闭系统请求，停止全部 IBKR 控制")
    record_event("system_shutdown_requested")
    errors = []
    try:
        await gateway.disable_control()
    except Exception as e:
        logger.exception("关闭 IBKR 交易门异常")
        errors.append(f"IBKR API: {e}")
    try:
        await copy_supervisor.stop_all()
    except Exception as e:
        logger.exception("停止跟单进程异常")
        errors.append(f"跟单进程: {e}")
    try:
        tws_closed = await _close_tws_process()
    except Exception as e:
        logger.exception("关闭 TWS 异常")
        tws_closed = False
        errors.append(f"TWS: {e}")
    record_event("system_shutdown_ready", tws_closed=tws_closed, errors=errors)
    _schedule_console_exit()
    return {"ok": tws_closed and not errors, "stopping": True,
            "tws_closed": tws_closed, "errors": errors}


# ---------------------------------------------------------------------------
# REST 接口
# ---------------------------------------------------------------------------

@app.get("/api/clients/self")
async def api_clients_self(request: Request) -> dict:
    """当前客户端身份：本机=master（免确认）；非本机按 X-Trader-Token 解析。"""
    if _is_local(request):
        return {"role": "master", "name": "master", "approved": True}
    token = request.headers.get("X-Trader-Token", "")
    info = _clients.get("tokens", {}).get(token)
    if info:
        expired = bool(info.get("approved")) and not _token_valid(info)
        return {"role": "trader", "name": info.get("name", ""),
                "approved": _token_valid(info), "expired": expired,
                "ttl_hours": config.CLIENT_TOKEN_TTL_HOURS}
    return {"role": "guest", "approved": False}


@app.post("/api/clients/register")
async def api_clients_register(req: ClientRegisterRequest,
                               request: Request) -> dict:
    """非本机客户端注册交易员名称 → 签发 token（pending，等 master 审批）。
    同名 pending 返回原 token（幂等）；名称已被已审批 token 占用 → 409。"""
    if _is_local(request):
        raise HTTPException(400, "本机即 master，无需注册")
    name = req.name.strip()
    if not name or not _CLIENT_NAME_RE.match(name):
        raise HTTPException(400, "名称非法（1-20 字符，仅允许中英文/数字/_/-）")
    if name.lower() == "master":
        raise HTTPException(400, "master 为保留名称")
    tokens = _clients.setdefault("tokens", {})
    # 同名查找：pending → 幂等返回原 token；已过期 approved → 作废旧 token
    # 换新重走审批；有效 approved → 409 占用
    for old_token, info in list(tokens.items()):
        if info.get("name") == name:
            if _token_valid(info):
                raise HTTPException(409, "名称已被使用")
            if info.get("approved"):
                del tokens[old_token]   # 已过期：允许重新登记（需重新审批）
                logger.info("客户端 %s 旧 token 已过期，重新登记", name)
                break
            return {"token": old_token, "name": name, "approved": False}
    token = uuid.uuid4().hex
    tokens[token] = {"name": name, "approved": False,
                     "created_at": time.time(), "approved_at": 0}
    _save_clients()
    record_event("client_registered", name=name, token_prefix=token[:6])
    logger.info("客户端注册: %s（token %s…），等待 master 审批", name, token[:6])
    return {"token": token, "name": name, "approved": False}


def _resolve_client_token(token_or_prefix: str):
    """按完整 token 或前缀解析客户端 token（快照只暴露前 6 位，
    审批端点须同时支持两种入参）。返回 (token, info) 或 (None, None)。"""
    tokens = _clients.get("tokens", {})
    info = tokens.get(token_or_prefix)
    if info is not None:
        return token_or_prefix, info
    matches = [(t, i) for t, i in tokens.items()
               if t.startswith(token_or_prefix)]
    if len(matches) == 1:
        return matches[0]
    return None, None


@app.post("/api/clients/{token}/approve")
async def api_clients_approve(token: str, request: Request) -> dict:
    """master 审批客户端 token（仅本机可调用，支持 token 前缀）。"""
    if not _is_local(request):
        raise HTTPException(403, "仅本机（master）可审批客户端")
    full, info = _resolve_client_token(token)
    if info is None:
        raise HTTPException(404, "token 不存在或前缀不唯一")
    info["approved"] = True
    info["approved_at"] = time.time()
    _save_clients()
    record_event("client_approved", name=info.get("name", ""),
                 token_prefix=full[:6])
    logger.info("客户端已审批: %s（token %s…）", info.get("name"), full[:6])
    return {"ok": True, "name": info.get("name", ""), "approved": True}


@app.post("/api/clients/{token}/reject")
async def api_clients_reject(token: str, request: Request) -> dict:
    """master 拒绝客户端 token：直接删除（仅本机可调用，支持 token 前缀）。"""
    if not _is_local(request):
        raise HTTPException(403, "仅本机（master）可拒绝客户端")
    full, info = _resolve_client_token(token)
    if info is None:
        raise HTTPException(404, "token 不存在或前缀不唯一")
    del _clients["tokens"][full]
    _save_clients()
    record_event("client_rejected", name=info.get("name", ""),
                 token_prefix=full[:6])
    logger.info("客户端已拒绝并删除: %s（token %s…）",
                info.get("name"), full[:6])
    return {"ok": True, "name": info.get("name", ""), "approved": False}


# ---------------------------------------------------------------------------
# 跟单服务器管理（CRUD 全套 require_trader 闸门，仅已审批客户端可改）
# ---------------------------------------------------------------------------

@app.get("/api/copy/servers")
async def api_copy_servers(trader: str = Depends(require_trader)) -> dict:
    """跟单服务器列表：配置 + 状态文件合并（无状态文件时状态字段为 null）。"""
    return {"servers": [_copy_server_row(c) for c in _load_copy_servers()]}


@app.post("/api/copy/servers")
async def api_copy_server_add(req: CopyServerCreateRequest,
                              trader: str = Depends(require_trader)) -> dict:
    """新增跟单服务器：name 唯一、client_id 缺省自动分配（13 起），
    落盘后触发监管 reload。"""
    name = req.name.strip()
    if not _COPY_SERVER_NAME_RE.match(name):
        raise HTTPException(400, "名称非法（1-10 字符，仅允许中英文/数字/_/-）")
    servers = _load_copy_servers()
    if any(s.get("name") == name for s in servers):
        raise HTTPException(409, "名称已存在")
    cfg = req.dict()
    cfg["name"] = name
    if not cfg.get("client_id"):
        cfg["client_id"] = _alloc_copy_client_id(servers)
    err = _validate_copy_server(cfg, servers)
    if err:
        raise HTTPException(400, err)
    servers.append(cfg)
    _save_copy_servers(servers)
    await copy_supervisor.reload(servers)
    record_event("copy_server_added", name=name, client_id=cfg["client_id"],
                 enabled=cfg["enabled"], trader=trader)
    logger.info("跟单服务器新增: %s (client_id=%s, enabled=%s, by %s)",
                name, cfg["client_id"], cfg["enabled"], trader)
    return {"ok": True, "server": _copy_server_row(cfg)}


@app.post("/api/copy/servers/{name}/update")
async def api_copy_server_update(name: str, req: CopyServerUpdateRequest,
                                 trader: str = Depends(require_trader)) -> dict:
    """改参数（name/enabled 不可在此改）；落盘后触发监管 reload。"""
    servers = _load_copy_servers()
    cfg = next((s for s in servers if s.get("name") == name), None)
    if cfg is None:
        raise HTTPException(404, "服务器不存在")
    updates = {k: v for k, v in req.dict().items() if v is not None}
    if not updates:
        raise HTTPException(400, "无可更新字段")
    merged = {**cfg, **updates}
    err = _validate_copy_server(merged, [s for s in servers if s is not cfg])
    if err:
        raise HTTPException(400, err)
    cfg.update(updates)
    _save_copy_servers(servers)
    await copy_supervisor.reload(servers)
    record_event("copy_server_updated", name=name,
                 changed=sorted(updates), trader=trader)
    logger.info("跟单服务器改参: %s %s (by %s)", name, sorted(updates), trader)
    return {"ok": True, "server": _copy_server_row(cfg)}


@app.post("/api/copy/servers/{name}/delete")
async def api_copy_server_delete(name: str,
                                 trader: str = Depends(require_trader)) -> dict:
    """删除跟单服务器：先停进程，再落盘并触发监管 reload。"""
    servers = _load_copy_servers()
    if not any(s.get("name") == name for s in servers):
        raise HTTPException(404, "服务器不存在")
    await copy_supervisor.terminate(name)
    servers = [s for s in servers if s.get("name") != name]
    _save_copy_servers(servers)
    await copy_supervisor.reload(servers)
    record_event("copy_server_deleted", name=name, trader=trader)
    logger.info("跟单服务器删除: %s (by %s)", name, trader)
    return {"ok": True, "name": name}


async def _copy_set_enabled(name: str, enabled: bool, trader: str) -> dict:
    """enable/disable 公共逻辑：落盘 + 监管 reload（启用 spawn / 停用 terminate）。"""
    servers = _load_copy_servers()
    cfg = next((s for s in servers if s.get("name") == name), None)
    if cfg is None:
        raise HTTPException(404, "服务器不存在")
    cfg["enabled"] = enabled
    _save_copy_servers(servers)
    await copy_supervisor.reload(servers)
    record_event("copy_server_enabled" if enabled else "copy_server_disabled",
                 name=name, trader=trader)
    logger.info("跟单服务器%s: %s (by %s)",
                "启用" if enabled else "停用", name, trader)
    return {"ok": True, "server": _copy_server_row(cfg)}


@app.post("/api/copy/servers/{name}/enable")
async def api_copy_server_enable(name: str,
                                 trader: str = Depends(require_trader)) -> dict:
    """启用跟单服务器（监管 spawn 子进程）。"""
    return await _copy_set_enabled(name, True, trader)


@app.post("/api/copy/servers/{name}/disable")
async def api_copy_server_disable(name: str,
                                  trader: str = Depends(require_trader)) -> dict:
    """停用跟单服务器（监管 terminate 子进程）。"""
    return await _copy_set_enabled(name, False, trader)


@app.post("/api/copy/servers/{name}/cmd")
async def api_copy_server_cmd(name: str, req: CopyCmdRequest,
                              trader: str = Depends(require_trader)) -> dict:
    """人工指令：合并写入 logs/copy-{name}-cmd.json（不覆盖已有指令——
    retry/dismiss 列表去重追加，unhalt 只置位不清除）。"""
    servers = _load_copy_servers()
    if not any(s.get("name") == name for s in servers):
        raise HTTPException(404, "服务器不存在")
    if not req.retry and not req.dismiss and not req.unhalt:
        raise HTTPException(400, "空指令（retry/dismiss/unhalt 至少一项）")
    cmd = {"retry": [], "dismiss": [], "unhalt": False}
    try:
        old = json.loads(_copy_cmd_path(name).read_text(encoding="utf-8"))
        if isinstance(old, dict):
            cmd["retry"] = list(old.get("retry") or [])
            cmd["dismiss"] = list(old.get("dismiss") or [])
            cmd["unhalt"] = bool(old.get("unhalt"))
    except FileNotFoundError:
        pass
    except (OSError, ValueError, TypeError):
        logger.warning("copy-%s-cmd.json 损坏，按空指令合并", name)
    for key, new_items in (("retry", req.retry), ("dismiss", req.dismiss)):
        for item in new_items:
            if item not in cmd[key]:
                cmd[key].append(item)
    cmd["unhalt"] = cmd["unhalt"] or req.unhalt
    path = _copy_cmd_path(name)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cmd, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    os.replace(tmp, path)
    record_event("copy_server_cmd", name=name, retry=req.retry,
                 dismiss=req.dismiss, unhalt=req.unhalt, trader=trader)
    logger.info("跟单人工指令: %s retry=%s dismiss=%s unhalt=%s (by %s)",
                name, req.retry, req.dismiss, req.unhalt, trader)
    return {"ok": True, "cmd": cmd}


class SettingsRequest(BaseModel):
    stop_atr_mult: Optional[float] = Field(None, description="止损 ATR 倍数（0.2~5.0）")
    screen_premarket_filter: Optional[bool] = Field(None, description="选股盘前区间过滤开关")
    fvg_premarket_filter: Optional[bool] = Field(None, description="FVG 监控盘前区间过滤开关")
    imessage_recipient: Optional[str] = Field(None, description="iMessage 告警收件人（空串=清除，未配置跳过该通道）")


def _settings_payload() -> dict:
    """全局设置当前值（GET /api/settings 与 POST 响应共用）。"""
    return {
        "stop_atr_mult": _stop_atr_mult(),
        "screen_premarket_filter": _screen_premarket_filter(),
        "fvg_premarket_filter": _fvg_premarket_filter(),
        "imessage_recipient": str(_settings.get("imessage_recipient") or ""),
    }


@app.get("/api/settings")
async def api_get_settings() -> dict:
    """全局设置（所有客户端共享，快照 status 同步下发）。"""
    return _settings_payload()


# ---------------------------------------------------------------------------
# CDP 联动：选股点选 → 本机 TV 桌面端第一个 pane 切到该标的
# ---------------------------------------------------------------------------

class TvShowRequest(BaseModel):
    symbol: str = Field(..., description="如 NASDAQ:AAPL")


# 切视觉左上角第一个可见 pane 到目标标的并读回校验（API 层即时生效）。
# widget 集合两条路径：TradingViewApi._chartWidgetCollection.getAll() 优先，
# 回退 _exposed_chartWidgetCollection._chartWidgetsDefs。视觉行优先排序用
# tv_control.js 的 COLLECT_SRC 口径：cw._mainDiv.getBoundingClientRect()
# 直接量每个 widget 的 DOM rect（回退 def width/height 匹配容器），
# 过滤不可见 pane（布局切换后残留 widget rect=0x0），按 (y, x) 行优先排序。
# 交易所前缀归一化（NASDAQ:QQQ→BATS:QQQ）只比 ticker。
_TV_SHOW_JS = r"""
(async () => {
  const prim = v => (typeof v === 'function') ? v() : v;
  const readSym = cw => {
    try { const s = cw.symbol();
      return String((s && typeof s.value === 'function') ? s.value() : s);
    } catch (e) {}
    try { return String(cw.model().mainSeries().symbol()); } catch (e) {}
    return 'ERR';
  };
  const rectOf = cw => {
    try {
      if (cw._mainDiv && cw._mainDiv.getBoundingClientRect) {
        const b = cw._mainDiv.getBoundingClientRect();
        return { x: b.x, y: b.y, w: b.width, h: b.height };
      }
    } catch (e) {}
    return null;
  };
  // 收集 widget（TradingViewApi 优先，_exposed 回退）
  let cws = [];
  const api = window.TradingViewApi && window.TradingViewApi._chartWidgetCollection;
  if (api && typeof api.getAll === 'function') cws = api.getAll() || [];
  if (!cws.length) {
    const col = window._exposed_chartWidgetCollection;
    if (col && col._chartWidgetsDefs) {
      cws = col._chartWidgetsDefs.map(d => d.chartWidget);
    }
  }
  if (!cws.length) return JSON.stringify({fatal: 'no chart widget collection'});
  // 过滤不可见 pane（rect 缺失或 0x0），按 (y, x) 视觉行优先排序
  const panes = cws.map(cw => ({ cw, rect: rectOf(cw) }))
    .filter(p => p.rect && p.rect.w > 50 && p.rect.h > 50)
    .sort((a, b) => (a.rect.y - b.rect.y) || (a.rect.x - b.rect.x));
  if (!panes.length) {
    // 单图/无法取 rect：退回集合顺序第一个
    panes.push({ cw: cws[0], rect: null });
  }
  const first = panes[0].cw;
  const before = readSym(first);
  const target = %SYMBOL%;
  if (typeof first.setSymbol === 'function') first.setSymbol(target);
  else if (first.model && first.model().mainSeries) {
    first.model().mainSeries().setSymbol(target);
  } else return JSON.stringify({fatal: 'widget 无 setSymbol', before});
  await new Promise(r => setTimeout(r, 300));
  const after = readSym(first);
  const ticker = String(target).split(':').pop().toUpperCase();
  const got = String(after).split(':').pop().toUpperCase();
  return JSON.stringify({before, after, ok: got === ticker, ticker,
                         panes: panes.length});
})()
"""


async def _tv_activate_window() -> None:
    """把 TV 窗口拉到前台（用户点选选股后希望立刻看到图表）。
    两步 best-effort：① CDP /json/activate 把图表标签页提到浏览器内部最前；
    ② 找到 9222 监听进程所属 .app，open -a 把整个应用窗口置前。
    全部失败静默（不影响切标的本身）。"""
    try:
        from controller import tv_cdp as _cdp_mod
        endpoint = _cdp_mod.config.CDP_HTTP_ENDPOINT  # 如 http://localhost:9222

        def _activate() -> None:
            import urllib.request
            with urllib.request.urlopen(f"{endpoint}/json", timeout=3) as r:
                targets = json.loads(r.read())
            page = next((t for t in targets
                         if t.get("type") == "page"
                         and "tradingview.com/chart" in t.get("url", "")), None)
            if page:
                with urllib.request.urlopen(
                        f"{endpoint}/json/activate/{page['id']}", timeout=3) as r:
                    r.read()

        await asyncio.to_thread(_activate)
        # 置前整个应用：9222 监听进程 pid → System Events frontmost。
        # 注意不能用 open -a：TV 调试 Chrome 与日常 Chrome 是同名 .app 的
        # 两个实例，open -a 无法区分，会激活错对象导致窗口只闪一下就回落。
        proc = await asyncio.create_subprocess_exec(
            "lsof", "-nP", "-iTCP:9222", "-sTCP:LISTEN", "-t",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        out, _ = await proc.communicate()
        pids = out.decode().split()
        if pids:
            try:
                await _osascript(
                    'tell application "System Events" to set frontmost of '
                    f'(first process whose unix id is {int(pids[0])}) to true')
                return
            except PermissionError:
                await notifier.notify(
                    "a11y_permission", "warning", "TV 窗口置前需要辅助功能权限",
                    "系统设置 → 隐私与安全性 → 辅助功能，授权运行控制台的终端 App")
            except Exception as e:
                logger.info("pid 精确置前失败，降级 open -a: %s", e)
        # 兜底：.app 路径 open -a（同名多实例时不精确，仅作退路）
        proc = await asyncio.create_subprocess_exec(
            "bash", "-c",
            "ps -o comm= -p $(lsof -nP -iTCP:9222 -sTCP:LISTEN -t | head -1) 2>/dev/null",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        out, _ = await proc.communicate()
        comm = out.decode().strip()
        if ".app/" in comm:
            app = comm.split(".app/")[0] + ".app"
            await asyncio.create_subprocess_exec(
                "open", "-a", app,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
    except Exception as e:
        logger.info("TV 窗口置前失败（静默）: %s", e)


async def _osascript(script: str, *args: str) -> str:
    proc = await asyncio.create_subprocess_exec(
        "osascript", "-e", script, *args,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    out, err = await proc.communicate()
    if proc.returncode != 0:
        msg = err.decode(errors="replace").strip()
        if "-1719" in msg or "辅助功能" in msg or "assistive" in msg.lower():
            raise PermissionError(msg)
        raise RuntimeError(msg)
    return out.decode().strip()


_TV_CHART_URL = "https://cn.tradingview.com/chart/I2KwX1s0/"  # auto_trade 保存布局
# 独立调试 profile（已登录 TV）。必须显式指定：① Chrome ≥136 起默认
# user-data-dir 下 --remote-debugging-port 被忽略（安全策略），端口永远起不来；
# ② 独立 profile 与日常 Chrome 互不干扰（各自单实例），且免登录。
_TV_CHROME_PROFILE = str(Path.home() / ".tv-chrome-profile")
_CONSOLE_URL = os.getenv("CONSOLE_URL", "http://127.0.0.1:8000/")
_CONSOLE_AUTO_OPEN_BROWSER = os.getenv("CONSOLE_AUTO_OPEN_BROWSER", "1") != "0"


async def _cdp_alive() -> bool:
    """9222 调试端口是否有可控浏览器（快速探测）。"""
    from controller import tv_cdp as _cdp_mod
    endpoint = _cdp_mod.config.CDP_HTTP_ENDPOINT

    def _probe() -> bool:
        import urllib.request
        try:
            with urllib.request.urlopen(f"{endpoint}/json/version",
                                        timeout=2) as r:
                return r.status == 200
        except Exception:
            return False

    return await asyncio.to_thread(_probe)


async def _tv_ensure_browser() -> Optional[str]:
    """确保可控浏览器存在。返回 None = 就绪；否则返回失败原因。
    三种情况：① 9222 不通 → 启动 Chrome（带调试端口 + TV 图表页）；
    ② 端口活但图表页被关了（macOS 关窗不退进程，target 列表为空）
    → /json/new 新开一个图表标签页；③ 图表页在 → 复用。最后都等 widget 就绪。"""
    from controller import tv_cdp as _cdp_mod
    from controller.tv_cdp import TradingViewCDPController
    endpoint = _cdp_mod.config.CDP_HTTP_ENDPOINT

    def _chart_page_up() -> bool:
        import urllib.request
        try:
            with urllib.request.urlopen(f"{endpoint}/json", timeout=2) as r:
                return any(t.get("type") == "page"
                           and "tradingview.com/chart" in t.get("url", "")
                           for t in json.loads(r.read()))
        except Exception:
            return False

    def _open_chart_tab() -> None:
        import urllib.request
        req = urllib.request.Request(
            f"{endpoint}/json/new?{_TV_CHART_URL}", method="PUT")
        with urllib.request.urlopen(req, timeout=5) as r:
            r.read()

    if await _cdp_alive():
        if not await asyncio.to_thread(_chart_page_up):
            logger.info("CDP 在线但无图表页，新开 TradingView 标签页 ...")
            try:
                await asyncio.to_thread(_open_chart_tab)
            except Exception as e:
                return f"新开图表标签页失败: {e}"
    else:
        logger.info("CDP 离线，启动浏览器打开 TradingView ...")
        await asyncio.create_subprocess_exec(
            "open", "-na", "Google Chrome", "--args",
            "--remote-debugging-port=9222",
            f"--user-data-dir={_TV_CHROME_PROFILE}", "--no-first-run",
            _TV_CHART_URL,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        # 等调试端口起来（Chrome 冷启动）
        for _ in range(15):
            await asyncio.sleep(2)
            if await _cdp_alive():
                break
        else:
            return ("调试 Chrome 未以可控模式启动（专用 profile "
                    f"{_TV_CHROME_PROFILE} 下 9222 端口 30s 未就绪；"
                    "该 profile 与日常 Chrome 独立，无需退出日常 Chrome——"
                    "请确认 profile 目录存在后再点一次）")
    # 等图表页 target 出现
    for _ in range(15):
        if await asyncio.to_thread(_chart_page_up):
            break
        await asyncio.sleep(2)
    else:
        return "浏览器已启动但图表页未加载出来"
    # 等 chart widget 就绪（TV 页面重，指标/布局初始化慢）
    ready_js = ("!!(((window.TradingViewApi||{})._chartWidgetCollection"
                "||{getAll:()=>[]}).getAll().length"
                "|| (((window._exposed_chartWidgetCollection||{})"
                "._chartWidgetsDefs)||[]).length)")
    try:
        for _ in range(30):
            async with TradingViewCDPController() as tv:
                if await asyncio.wait_for(tv.evaluate(ready_js), timeout=5):
                    logger.info("浏览器已就绪（TV 图表页 + widget 加载完成）")
                    return None
            await asyncio.sleep(2)
        return "图表页已打开但 widget 未就绪（页面可能未登录或加载过慢）"
    except Exception as e:
        return f"浏览器就绪检查异常: {e}"


def _cdp_page_id(url_prefix: str) -> Optional[str]:
    from controller import tv_cdp as _cdp_mod
    import urllib.request
    endpoint = _cdp_mod.config.CDP_HTTP_ENDPOINT
    try:
        with urllib.request.urlopen(f"{endpoint}/json", timeout=2) as r:
            targets = json.loads(r.read())
        page = next((t for t in targets if t.get("type") == "page"
                     and t.get("url", "").startswith(url_prefix)), None)
        return page.get("id") if page else None
    except Exception:
        return None


async def _cdp_open_tab(url: str) -> None:
    from controller import tv_cdp as _cdp_mod
    import urllib.request
    endpoint = _cdp_mod.config.CDP_HTTP_ENDPOINT

    def _open() -> None:
        req = urllib.request.Request(f"{endpoint}/json/new?{url}", method="PUT")
        with urllib.request.urlopen(req, timeout=5) as r:
            r.read()

    await asyncio.to_thread(_open)


async def _activate_console_tab() -> None:
    page_id = await asyncio.to_thread(_cdp_page_id, _CONSOLE_URL)
    if not page_id:
        return
    from controller import tv_cdp as _cdp_mod
    import urllib.request
    endpoint = _cdp_mod.config.CDP_HTTP_ENDPOINT

    def _activate() -> None:
        with urllib.request.urlopen(f"{endpoint}/json/activate/{page_id}", timeout=3) as r:
            r.read()

    await asyncio.to_thread(_activate)


async def _workspace_ensure_browser() -> Optional[str]:
    """同一调试 Chrome 内准备控制台和 TV 两个独立标签页。"""
    if not await _cdp_alive():
        logger.info("启动交易工作区浏览器（控制台 + TradingView）...")
        proc = await asyncio.create_subprocess_exec(
            "open", "-na", "Google Chrome", "--args",
            "--remote-debugging-port=9222",
            f"--user-data-dir={_TV_CHROME_PROFILE}", "--no-first-run",
            _TV_CHART_URL, _CONSOLE_URL,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        await proc.wait()
        for _ in range(15):
            await asyncio.sleep(2)
            if await _cdp_alive():
                break
        else:
            return "调试 Chrome 的 9222 端口 30s 未就绪"
    elif not await asyncio.to_thread(_cdp_page_id, _CONSOLE_URL):
        await _cdp_open_tab(_CONSOLE_URL)
    not_ready = await _tv_ensure_browser()
    if not_ready:
        return not_ready
    if not await asyncio.to_thread(_cdp_page_id, _CONSOLE_URL):
        await _cdp_open_tab(_CONSOLE_URL)
    try:
        await _activate_console_tab()
    except Exception as e:
        logger.info("控制台标签页置前失败（静默）: %s", e)
    return None


async def _startup_prepare() -> None:
    """打开工作区并等待全链路首次就绪；就绪锁定后不再因断线退回加载页。"""
    await asyncio.sleep(0.5)   # 让 uvicorn 完成 startup 并开始接收加载页请求
    if _CONSOLE_AUTO_OPEN_BROWSER:
        while not _startup_state["browser_ready"]:
            try:
                error = await _workspace_ensure_browser()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.exception("启动浏览器工作区异常")
                error = str(e)
            _startup_state["browser_error"] = error or ""
            if not error:
                _startup_state["browser_ready"] = True
                break
            await asyncio.sleep(5)
    else:
        _startup_state["browser_ready"] = True
    while not _startup_state["ready"]:
        if _startup_status()["all_ready"]:
            _startup_state["ready"] = True
            logger.info("控制台全链路就绪，加载页自动进入主页面")
            return
        await asyncio.sleep(1)


async def _tv_show_symbol(symbol: str) -> dict:
    """切第一个 pane 到该标的 + 读回校验 + 窗口置前。
    前置：CDP 不在线先启动浏览器并定位到 TradingView（_tv_ensure_browser）。"""
    from controller.tv_cdp import TradingViewCDPController
    not_ready = await _tv_ensure_browser()
    if not_ready:
        logger.warning("TV 不可用: %s", not_ready)
        return {"ok": False, "reason": not_ready}
    # 冷启动竞态：新开页的 widget 集合先于图表布局恢复就绪，setSymbol 会被
    # 布局恢复覆盖（实测首开页切 AMD 读回 NFLX，页面热身后即正常）→ 重试
    report = {}
    for attempt in range(3):
        try:
            async with TradingViewCDPController() as tv:
                report = json.loads(await asyncio.wait_for(
                    tv.evaluate(_TV_SHOW_JS.replace("%SYMBOL%", json.dumps(symbol))),
                    timeout=10))
        except Exception as e:
            logger.info("TV 切标的不可用（静默降级）: %s", e)
            return {"ok": False, "reason": str(e)}
        if report.get("fatal") or report.get("ok"):
            break
        if attempt < 2:
            await asyncio.sleep(2)
    if report.get("fatal"):
        logger.info("TV 切标的失败: %s", report["fatal"])
        return {"ok": False, "reason": report["fatal"]}
    if report.get("ok"):
        logger.info("TV 第一个 pane 已切到 %s（%s → %s）",
                    symbol, report.get("before"), report.get("after"))
        await _tv_activate_window()
        return {"ok": True, "shown": report.get("after")}
    logger.warning("TV 切标的读回不一致: 目标 %s 读回 %s",
                   symbol, report.get("after"))
    return {"ok": False, "reason": f"读回不一致: {report.get('after')}",
            "shown": report.get("after")}


@app.post("/api/tv/show")
async def api_tv_show(req: TvShowRequest,
                      trader: str = Depends(require_trader)) -> dict:
    """选股点选联动：本机 TV 桌面端视觉左上角第一个 pane 切到该标的。
    幂等快速（多客户端同时点各自切同一台 TV，单机单屏本质）；失败静默。"""
    symbol = req.symbol.strip()
    if not symbol:
        raise HTTPException(400, "symbol 不能为空")
    return await _tv_show_symbol(symbol)


# ---------------------------------------------------------------------------
# 客户端看盘（v2026-10-27）：「每人一格」+ 截图回传
# master 的 TV 屏幕是唯一能渲染私有指标（trend_state/FVG/smart_stop）的画面。
# master 固定 pane 0（现状不变）；每个已审批客户端按首次使用分配 pane 1..N，
# 点击标的 → 切自己那格 → 等数据就绪 → 按该格 rect 裁剪截图 → base64 回传，
# 前端弹窗看图（10s 自动刷新）。pane 分完 → 明确报「画面位已满」。
# ---------------------------------------------------------------------------

# pane 收集公共片段（与 _TV_SHOW_JS 同口径：双路径取 widget、rect 过滤
# 不可见、(y,x) 行优先视觉排序）
_TV_PANES_COLLECT_JS = r"""
  const readSym = cw => {
    try { const s = cw.symbol();
      return String((s && typeof s.value === 'function') ? s.value() : s);
    } catch (e) {}
    try { return String(cw.model().mainSeries().symbol()); } catch (e) {}
    return '';
  };
  const rectOf = cw => {
    try {
      if (cw._mainDiv && cw._mainDiv.getBoundingClientRect) {
        const b = cw._mainDiv.getBoundingClientRect();
        return { x: b.x, y: b.y, w: b.width, h: b.height };
      }
    } catch (e) {}
    return null;
  };
  let cws = [];
  const api = window.TradingViewApi && window.TradingViewApi._chartWidgetCollection;
  if (api && typeof api.getAll === 'function') cws = api.getAll() || [];
  if (!cws.length) {
    const col = window._exposed_chartWidgetCollection;
    if (col && col._chartWidgetsDefs) {
      cws = col._chartWidgetsDefs.map(d => d.chartWidget);
    }
  }
  const panes = cws.map(cw => ({ cw, rect: rectOf(cw) }))
    .filter(p => p.rect && p.rect.w > 50 && p.rect.h > 50)
    .sort((a, b) => (a.rect.y - b.rect.y) || (a.rect.x - b.rect.x));
"""

_TV_PANES_JS = "\n(() => {" + _TV_PANES_COLLECT_JS + r"""
  return JSON.stringify(panes.map(p => ({ symbol: readSym(p.cw),
                                          rect: p.rect })));
})()
"""

# 切指定 pane 到目标标的：已是该标的则跳过切换（客户端刷新截图的 fast-path，
# setSymbol 会触发数据重载，能不动就不动）
_TV_PANE_SHOW_JS = "\n(async () => {" + _TV_PANES_COLLECT_JS + r"""
  const idx = %PANE%;
  if (!panes.length) return JSON.stringify({ fatal: 'no visible pane' });
  if (idx >= panes.length) return JSON.stringify({ fatal: 'pane 不足',
                                                   panes: panes.length });
  const target = %SYMBOL%;
  const ticker = String(target).split(':').pop().toUpperCase();
  const cw = panes[idx].cw;
  const before = readSym(cw);
  const needSwitch = String(before).split(':').pop().toUpperCase() !== ticker;
  if (needSwitch) {
    if (typeof cw.setSymbol === 'function') cw.setSymbol(target);
    else if (cw.model && cw.model().mainSeries) {
      cw.model().mainSeries().setSymbol(target);
    } else return JSON.stringify({ fatal: 'widget 无 setSymbol', before });
    await new Promise(r => setTimeout(r, 300));
  }
  const after = readSym(cw);
  const got = String(after).split(':').pop().toUpperCase();
  return JSON.stringify({ before, after, ok: got === ticker,
                          switched: needSwitch, rect: panes[idx].rect,
                          panes: panes.length });
})()
"""

# 数据就绪探针：pane symbol 已是目标 且 K 线 bars 已加载（切标的后 TV
# 拉数据要 5~20s，轮询到就绪再截图，避免截到旧图/loading）
_TV_PANE_READY_JS = "\n(() => {" + _TV_PANES_COLLECT_JS + r"""
  const idx = %PANE%;
  const ticker = %TICKER%;
  if (idx >= panes.length) return 'false';
  const cw = panes[idx].cw;
  try {
    const s = cw.symbol();
    const cur = String((s && typeof s.value === 'function') ? s.value() : s)
      .split(':').pop().toUpperCase();
    if (cur !== ticker) return 'false';
    const bars = cw.model().mainSeries().bars();
    return String(!!(bars && bars.size() > 5));
  } catch (e) { return 'false'; }
})()
"""

_TV_PANES_PATH = (Path(__file__).resolve().parent.parent
                  / "logs" / "tv_panes.json")
_TV_PANE_STALE_S = 2 * 3600   # 两小时没用的客户端画面位可回收再分配
_tv_pane_locks: Dict[int, asyncio.Lock] = {}
_tv_view_info: dict = {"panes": 0, "slots": 0, "checked_at": 0.0}


def _tv_load_pane_map() -> dict:
    """trader → {pane, last_used} 映射；缺文件/损坏返回 {}。"""
    try:
        payload = json.loads(_TV_PANES_PATH.read_text(encoding="utf-8"))
        if isinstance(payload, dict):
            return payload
    except FileNotFoundError:
        pass
    except (OSError, ValueError) as e:
        logger.warning("tv_panes.json 加载失败: %s", e)
    return {}


def _tv_save_pane_map(m: dict) -> None:
    try:
        _TV_PANES_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = _TV_PANES_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(m, ensure_ascii=False, indent=1),
                       encoding="utf-8")
        os.replace(tmp, _TV_PANES_PATH)
    except OSError:
        logger.exception("tv_panes.json 落盘失败")


def _tv_assign_pane(trader: str, pane_count: int) -> Optional[int]:
    """给客户端分配画面位：已有映射且仍有效 → 复用；否则取最小空位
    （pane 0 是 master 的）；全满时先回收两小时未用的旧映射，仍无位 → None。"""
    now = time.time()
    m = _tv_load_pane_map()
    row = m.get(trader)
    if isinstance(row, dict) and 0 < int(row.get("pane", 0)) < pane_count:
        row["last_used"] = now
        _tv_save_pane_map(m)
        return int(row["pane"])
    # 失效映射清掉（布局缩小后 pane 越界的、自己的旧映射）
    m = {k: v for k, v in m.items()
         if isinstance(v, dict) and 0 < int(v.get("pane", 0)) < pane_count}
    used = {int(v["pane"]) for v in m.values()}
    for idx in range(1, pane_count):
        if idx not in used:
            m[trader] = {"pane": idx, "last_used": now}
            _tv_save_pane_map(m)
            return idx
    # 回收最久未用的（超过 _TV_PANE_STALE_S）
    stale = sorted(m.items(), key=lambda kv: kv[1].get("last_used", 0))
    if stale and now - stale[0][1].get("last_used", 0) > _TV_PANE_STALE_S:
        old_trader, old = stale[0]
        idx = int(old["pane"])
        del m[old_trader]
        m[trader] = {"pane": idx, "last_used": now}
        _tv_save_pane_map(m)
        logger.info("TV 画面位回收：%s 的 pane %d 转给 %s", old_trader, idx,
                    trader)
        return idx
    _tv_save_pane_map(m)
    return None


async def _tv_list_panes() -> Optional[list]:
    """读当前可见 pane 列表 [{symbol, rect}]（视觉行优先）；失败返回 None。"""
    from controller.tv_cdp import TradingViewCDPController
    try:
        async with TradingViewCDPController() as tv:
            return json.loads(await asyncio.wait_for(
                tv.evaluate(_TV_PANES_JS), timeout=10))
    except Exception as e:
        logger.info("TV pane 列表读取失败（静默）: %s", e)
        return None


async def _tv_view_for_trader(symbol: str, trader: str) -> dict:
    """客户端看盘主流程：确保浏览器 → 分配画面位 → 切该 pane → 等数据就绪
    → 按 pane rect 裁剪截图回传。每 pane 一把锁防并发切换同一格。"""
    from controller.tv_cdp import TradingViewCDPController
    not_ready = await _tv_ensure_browser()
    if not_ready:
        return {"ok": False, "reason": not_ready}
    panes = await _tv_list_panes()
    if not panes:
        return {"ok": False, "reason": "TV 无可用画面（图表未加载或宫格为空）"}
    idx = _tv_assign_pane(trader, len(panes))
    if idx is None:
        return {"ok": False, "code": "pane_full",
                "reason": f"画面位已满（TV 当前 {len(panes)} 格，master 占 1 格，"
                          "客户端画面位已全部分完）"}
    lock = _tv_pane_locks.setdefault(idx, asyncio.Lock())
    async with lock:
        report = {}
        try:
            async with TradingViewCDPController() as tv:
                show_js = (_TV_PANE_SHOW_JS
                           .replace("%PANE%", str(idx))
                           .replace("%SYMBOL%", json.dumps(symbol)))
                # 冷启动竞态（同 _tv_show_symbol）：布局恢复会覆盖 setSymbol，重试
                for attempt in range(3):
                    report = json.loads(await asyncio.wait_for(
                        tv.evaluate(show_js), timeout=10))
                    if report.get("fatal") or report.get("ok"):
                        break
                    if attempt < 2:
                        await asyncio.sleep(2)
                if report.get("fatal"):
                    return {"ok": False, "reason": report["fatal"]}
                if not report.get("ok"):
                    return {"ok": False,
                            "reason": f"读回不一致: {report.get('after')}"}
                if report.get("switched"):
                    ticker = symbol.split(":")[-1].upper()
                    ready_js = (_TV_PANE_READY_JS
                                .replace("%PANE%", str(idx))
                                .replace("%TICKER%", json.dumps(ticker)))
                    for _ in range(12):      # 最多等 12s 数据就绪
                        ok = await asyncio.wait_for(
                            tv.evaluate(ready_js), timeout=5)
                        if str(ok) == "true":
                            break
                        await asyncio.sleep(1)
                    await asyncio.sleep(1.2)   # 渲染稳定一帧再截
                rect = report["rect"]
                shot = await asyncio.wait_for(tv.screenshot(
                    quality=70,
                    clip={"x": rect["x"], "y": rect["y"],
                          "width": rect["w"], "height": rect["h"]}),
                    timeout=15)
        except Exception as e:
            logger.info("TV 客户端看盘失败（%s pane %d）: %s", trader, idx, e)
            return {"ok": False, "reason": str(e)}
    logger.info("TV 看盘：%s → pane %d 切到 %s（截图 %d KB）",
                trader, idx, report.get("after"), len(shot) // 1024)
    return {"ok": True, "pane": idx, "shown": report.get("after"),
            "image": base64.b64encode(shot).decode("ascii")}


@app.post("/api/tv/view")
async def api_tv_view(req: TvShowRequest,
                      trader: str = Depends(require_trader)) -> dict:
    """看盘统一入口：master → 只切 pane 0 不截图（面前就是屏幕）；
    已审批客户端 → 专属 pane 切标的 + 截图回传。"""
    symbol = req.symbol.strip()
    if not symbol:
        raise HTTPException(400, "symbol 不能为空")
    if trader == "master":
        return {**await _tv_show_symbol(symbol), "master": True}
    return await _tv_view_for_trader(symbol, trader)


@app.on_event("startup")
async def _tv_view_startup_check():
    """启动校验 TV 画面位（布局由人工保证，这里只盘点并在快照里展示）。"""
    async def _check():
        global _tv_view_info
        if not await _cdp_alive():
            _tv_view_info = {"panes": 0, "slots": 0,
                             "checked_at": time.time(), "note": "CDP 离线"}
            return
        panes = await _tv_list_panes()
        n = len(panes or [])
        _tv_view_info = {"panes": n, "slots": max(0, n - 1),
                         "checked_at": time.time()}
        logger.info("TV 画面位校验：%d 格（master 1 + 客户端位 %d）",
                    n, max(0, n - 1))
    asyncio.create_task(_check())


@app.post("/api/settings")
async def api_update_settings(req: SettingsRequest,
                              trader: str = Depends(require_trader)) -> dict:
    """修改全局设置并落盘 settings.json（重启后保持）。
    只更新显式传入的字段（None = 不动），至少传一个。"""
    changed = {}
    if req.stop_atr_mult is not None:
        if not (_STOP_ATR_MULT_MIN <= req.stop_atr_mult <= _STOP_ATR_MULT_MAX):
            raise HTTPException(400, f"stop_atr_mult 超出允许范围"
                                     f"（{_STOP_ATR_MULT_MIN}~{_STOP_ATR_MULT_MAX}）")
        _settings["stop_atr_mult"] = round(float(req.stop_atr_mult), 2)
        changed["stop_atr_mult"] = _settings["stop_atr_mult"]
    for key in ("screen_premarket_filter", "fvg_premarket_filter"):
        val = getattr(req, key)
        if val is not None:
            _settings[key] = bool(val)
            changed[key] = _settings[key]
    if req.imessage_recipient is not None:
        v = req.imessage_recipient.strip()
        if v:
            _settings["imessage_recipient"] = v
        else:
            _settings.pop("imessage_recipient", None)   # 空串 = 清除（跳过通道）
        changed["imessage_recipient"] = v
    if not changed:
        raise HTTPException(400, "未传入任何设置字段")
    _save_settings()
    record_event("settings_updated", trader=trader, **changed)
    logger.info("全局设置已更新 %s（by %s）", changed, trader)
    return {"ok": True, **_settings_payload()}


@app.get("/api/status")
async def api_status() -> dict:
    return (await build_snapshot())["status"]


@app.get("/api/logs")
async def api_logs(limit: int = 200) -> list:
    """返回最近 N 条日志（内存环形缓冲区）。"""
    logs = list(_log_buffer)
    return logs[-limit:]


@app.get("/api/market")
async def api_market() -> dict:
    """TV 实时数据快照（5M K线 + trend_state）+ FVG 监控槽位（前端首屏用）。"""
    health = collector.data_health()
    return {"symbols": collector.latest(),
            "age": health["content_age"],
            "transport_age": health["transport_age"],
            "bar_age": health["bar_age"],
            "stale": health["stale"],
            "stale_reason": health["reason"],
            "running": collector.running,
            "fvg_monitor": _fvg_monitor_snapshot()}


@app.get("/api/screen")
async def api_screen() -> dict:
    """横截面选股（双向）：trend_state 多空两列，按 EMA 发散度排序。"""
    return compute_screen()


@app.get("/api/shadow")
async def api_shadow() -> dict:
    """当日影子回测：统计 + 全部回踩事件（实时逐 bar 推演）。"""
    return {"stats": shadow_tracker.stats(),
            "events": shadow_tracker.events(200)}


@app.get("/api/shadow/report")
async def api_shadow_report(day: str = "") -> dict:
    """每日影子复盘报告：缺省=当天（ET），任意历史日可重算（实时计算）。"""
    import re as _re
    from zoneinfo import ZoneInfo
    import datetime as _dt
    if day and not _re.match(r"^\d{4}-\d{2}-\d{2}$", day):
        raise HTTPException(400, "day 格式应为 YYYY-MM-DD")
    if not day:
        day = _dt.datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
    return shadow_report.generate_report(day)


@app.get("/api/precheck")
async def api_precheck() -> dict:
    """盘前检查单：手动随时跑（纯读状态，无副作用；非交易日如实报红）。"""
    return await run_precheck()


@app.get("/api/precheck/last")
async def api_precheck_last() -> dict:
    """最近一次落盘的盘前检查单（logs/precheck-*.json 最新一份）。"""
    files = sorted(_PRECHECK_DIR.glob("precheck-*.json"))
    if not files:
        raise HTTPException(404, "暂无盘前检查记录")
    try:
        return json.loads(files[-1].read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise HTTPException(500, "最近一份盘前检查读取失败")


class AutoStartRequest(BaseModel):
    strategy: str = "ema9_pyramid"
    symbols: List[str] = Field(default_factory=list,
                               description="空 = 全 WATCHLIST（受实例上限约束）")
    params: dict = Field(default_factory=dict, description="initial_risk_usd=当前单笔资金")


@app.post("/api/auto/start")
async def api_auto_start(req: AutoStartRequest,
                         trader: str = Depends(require_trader)) -> dict:
    """EMA9 实时触线全自动建仓；首仓 R，后续 2R/3、R/3，最多三次。"""
    global _auto_enabled, _auto_strategy_name, _auto_last_bar
    if req.strategy != "ema9_pyramid":
        raise HTTPException(400, "自动策略仅支持 ema9_pyramid")
    symbols = [s.strip().upper() for s in req.symbols if s.strip()]
    if not symbols:
        symbols = list(config.WATCHLIST)
    fixed = []
    for s in symbols:
        if ":" not in s:
            match = [w for w in config.WATCHLIST if w.split(":")[-1] == s]
            fixed.append(match[0] if match else s)
        else:
            fixed.append(s)
    try:
        initial_risk = float(req.params.get("initial_risk_usd")
                             or config.RISK_PER_TRADE_USD)
    except (TypeError, ValueError):
        raise HTTPException(400, "首仓风险预算格式无效")
    if initial_risk <= 0:
        raise HTTPException(400, "首仓风险预算必须大于 0")
    market = collector.latest() or {}
    missing = [s for s in fixed
               if not (((market.get(s) or {}).get("e9") or {}).get("ema_9"))]
    if missing:
        raise HTTPException(409, "EMA9 v7 指标未就绪: " + ",".join(missing))
    no_direction = [s for s in fixed
                    if ((market.get(s) or {}).get("e9") or {}).get("direction") not in (1, -1)]
    if no_direction:
        raise HTTPException(409, "启动时方向不明确，无法锁定策略方向: " + ",".join(no_direction))
    directions = {s: int(((market.get(s) or {}).get("e9") or {}).get("direction"))
                  for s in fixed}
    started = auto_engine.start(fixed, initial_risk, directions)
    _auto_enabled = True
    _auto_strategy_name = req.strategy
    params = {"initial_risk_usd": initial_risk, "start_directions": directions,
              "risk_ratios": [1.0, 2.0 / 3.0, 1.0 / 3.0], "max_entries": 3}
    record_event("auto_start", strategy=req.strategy, symbols=started, params=params)
    logger.info("EMA9 金字塔启动: %s（首仓风险 %.2f）", started, initial_risk)
    return {"ok": True, "started": started, "params": params,
            "skipped": [s for s in fixed if s not in started]}


@app.post("/api/auto/stop")
async def api_auto_stop(close_positions: bool = False,
                        trader: str = Depends(require_trader)) -> dict:
    """停止全自动策略：不再开新仓；存量实例默认继续管理（可选全部平仓）。"""
    global _auto_enabled
    _auto_enabled = False
    auto_engine.enabled = False
    stopped = []
    market = collector.latest() or {}
    for inst in auto_engine.instances():
        if inst["state"] == "DONE":
            continue
        auto_engine.stop_symbol(inst["symbol"], close_position=close_positions)
        if close_positions:
            ref = ((market.get(inst["symbol"]) or {}).get("bar") or {}).get("close") or 0
            await auto_engine._stop_close(
                auto_engine._instances[inst["symbol"]], ref, "manual_stop")
        stopped.append(inst["symbol"])
    record_event("auto_stop", symbols=stopped, close_positions=close_positions)
    return {"ok": True, "stopped": stopped, "close_positions": close_positions}


def _auto_instances() -> list:
    rows = auto_engine.instances()
    equity = _equity_cache.get("equity") or 0
    for row in rows:
        row["max_position_pct"] = (round(row["max_notional"] / equity * 100, 2)
                                   if equity > 0 else None)
    return rows


@app.get("/api/auto/status")
async def api_auto_status() -> dict:
    """金字塔策略状态：首仓预算、建仓次数、平均成本、解锁与最大仓位。"""
    return {"enabled": _auto_enabled, "strategy": _auto_strategy_name,
            "max_instances": config.AUTO_MAX_INSTANCES,
            "instances": _auto_instances(), "proposals": []}


class AutoAdoptRequest(BaseModel):
    symbol: str
    lot_id: str
    initial_risk_usd: float = 0


@app.post("/api/auto/adopt")
async def api_auto_adopt(req: AutoAdoptRequest,
                         trader: str = Depends(require_trader)) -> dict:
    """把已成交手动仓作为金字塔首仓接管。"""
    global _auto_enabled, _auto_strategy_name
    symbol = req.symbol.strip().upper()
    if ":" not in symbol:
        match = [s for s in config.WATCHLIST if s.split(":")[-1] == symbol]
        if match:
            symbol = match[0]
    lot = next((r for r in order_mgr.list_lots(active_only=True)
                if r["lot_id"] == req.lot_id and r["symbol"] == symbol), None)
    if lot is None:
        raise HTTPException(404, "未找到该股票的 ACTIVE 手动仓")
    existing = auto_engine._instances.get(symbol)
    risk = (req.initial_risk_usd
            or (existing.initial_risk_usd if existing else 0)
            or config.RISK_PER_TRADE_USD)
    e9 = ((collector.latest() or {}).get(symbol) or {}).get("e9") or {}
    try:
        inst = await auto_engine.adopt_seed(symbol, lot, risk, e9, enabled=True)
    except ValueError as e:
        raise HTTPException(409, str(e))
    _auto_enabled = True
    _auto_strategy_name = "ema9_pyramid"
    record_event("auto_manual_adopted", symbol=symbol, lot_id=req.lot_id,
                 initial_risk_usd=risk)
    return {"ok": True, "instance": inst.view()}


@app.post("/api/auto/instances/{symbol}/stop")
async def api_auto_instance_stop(symbol: str, close_positions: bool = False,
                                 trader: str = Depends(require_trader)) -> dict:
    """单票停止加仓，或仅整组平仓该股票。"""
    symbol = symbol.strip().upper()
    if ":" not in symbol:
        match = [s for s in config.WATCHLIST if s.split(":")[-1] == symbol]
        if match:
            symbol = match[0]
    inst = auto_engine._instances.get(symbol)
    if inst is None:
        raise HTTPException(404, "策略实例不存在")
    auto_engine.stop_symbol(symbol, close_position=close_positions)
    if close_positions:
        ref = (((collector.latest() or {}).get(symbol) or {})
               .get("bar") or {}).get("close") or 0
        await auto_engine._stop_close(inst, ref, "manual_stop")
    record_event("auto_instance_stop", symbol=symbol,
                 close_positions=close_positions)
    return {"ok": True, "symbol": symbol, "close_positions": close_positions}


@app.post("/api/auto/proposals/{proposal_id}/approve")
async def api_auto_proposal_approve(
        proposal_id: str,
        trader: str = Depends(require_trader)) -> dict:
    raise HTTPException(410, "EMA9 金字塔为全自动策略，无人工候选")


class AutoRejectRequest(BaseModel):
    reason: str = ""


@app.post("/api/auto/proposals/{proposal_id}/reject")
async def api_auto_proposal_reject(
        proposal_id: str,
        req: Optional[AutoRejectRequest] = None,
        trader: str = Depends(require_trader)) -> dict:
    raise HTTPException(410, "EMA9 金字塔为全自动策略，无人工候选")


# ---------------------------------------------------------------------------
# 回测工作台（会话式：加载 → 单步 / 全跑 → 订单全生命周期）
# ---------------------------------------------------------------------------

from engine.backtest_session import BacktestSession

_bt_session: Optional[BacktestSession] = None


class BtLoadRequest(BaseModel):
    symbol: str = Field(..., description="如 NVDA / NASDAQ:NVDA")
    day: str = ""                     # YYYY-MM-DD，默认上个交易日
    strategy: str = "ema9_pyramid"
    params: dict = Field(default_factory=dict)
    bars: int = 500


@app.post("/api/bt/load")
async def api_bt_load(req: BtLoadRequest) -> dict:
    """加载回测数据并初始化会话（拉取 5M bar + trend_state，约 5-10s）。"""
    global _bt_session
    from datetime import datetime as _dt
    from backtest_trend import ET, fetch_history, prev_trading_day
    symbol = req.symbol.strip().upper()
    if ":" not in symbol:
        match = [s for s in config.WATCHLIST if s.split(":")[-1] == symbol]
        if match:
            symbol = match[0]
    day = req.day or prev_trading_day(_dt.now(ET)).date().isoformat()
    try:
        rows = await fetch_history(symbol, req.bars, day)
    except Exception as e:
        raise HTTPException(400, f"历史数据拉取失败: {e}")
    try:
        session = BacktestSession(rows, symbol, day, req.strategy, req.params)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if session.total_bars == 0:
        raise HTTPException(400, f"{symbol} 在 {day} 无常规时段数据")
    _bt_session = session
    logger.info("回测会话加载 %s %s [%s]：%d 根 bar", symbol, day,
                req.strategy, session.total_bars)
    return _bt_session.state()


@app.post("/api/bt/step")
async def api_bt_step(n: int = 1) -> dict:
    """单步：前进 n 根 bar（默认 1 根）。"""
    if _bt_session is None:
        raise HTTPException(400, "请先加载回测数据（/api/bt/load）")
    return await _bt_session.step(n)


@app.post("/api/bt/run_all")
async def api_bt_run_all() -> dict:
    """一次性跑完当日剩余全部 bar。"""
    if _bt_session is None:
        raise HTTPException(400, "请先加载回测数据（/api/bt/load）")
    return await _bt_session.run_all()


@app.get("/api/bt/state")
async def api_bt_state() -> dict:
    """当前回测会话状态（进度/当前 bar/实例/订单生命周期/汇总）。"""
    if _bt_session is None:
        raise HTTPException(400, "无活跃回测会话")
    return _bt_session.state()


@app.get("/api/backtest")
async def api_backtest(symbol: str, day: str = "", bars: int = 500) -> dict:
    """历史回测：把指定交易日的 5M bar 逐根喂给回测引擎，
    返回每一次入场信号的统计与 N 倍盈亏比分布。"""
    from datetime import datetime as _dt
    from backtest_trend import ET, fetch_history, prev_trading_day, run_backtest
    symbol = symbol.strip().upper()
    if ":" not in symbol:   # 补交易所前缀
        match = [s for s in config.WATCHLIST if s.split(":")[-1] == symbol]
        if match:
            symbol = match[0]
    if not day:
        day = prev_trading_day(_dt.now(ET)).date().isoformat()
    try:
        rows = await fetch_history(symbol, bars)
    except Exception as e:
        raise HTTPException(400, f"历史数据拉取失败: {e}")
    return {"symbol": symbol, **run_backtest(rows, day)}


@app.get("/api/performance")
async def api_performance() -> dict:
    """复盘统计：从历史归档（logs/trades-*.json）读取平仓记录。"""
    import glob
    import json as _json
    logs_dir = Path(__file__).resolve().parent.parent / "logs"
    records = []
    for f in sorted(glob.glob(str(logs_dir / "trades-*.json"))):
        try:
            records.extend(_json.loads(open(f, encoding="utf-8").read()))
        except Exception:
            continue
    closed = [r for r in records if r.get("state") == "CLOSED"]

    def _stats(rows) -> dict:
        if not rows:
            return {"count": 0, "win_rate": 0, "avg_win": 0, "avg_loss": 0,
                    "profit_factor": None, "total_pnl": 0,
                    "avg_mae": 0, "avg_mfe": 0, "avg_target_rr": 0,
                    "avg_actual_rr": 0, "expectancy": 0,
                    "breakeven_rate": 0, "avg_breakeven_min": 0,
                    "avg_stop_min": 0, "avg_hold_min": 0}
        wins = [r for r in rows if r.get("realized_pnl", 0) > 0]
        losses = [r for r in rows if r.get("realized_pnl", 0) <= 0]
        total = sum(r.get("realized_pnl", 0) for r in rows)
        avg_win = sum(r["realized_pnl"] for r in wins) / len(wins) if wins else 0.0
        avg_loss = sum(r["realized_pnl"] for r in losses) / len(losses) if losses else 0.0
        pf = round(abs(avg_win / avg_loss), 2) if avg_loss != 0 else None
        avg_mae = sum(r.get("mae", 0) for r in rows) / len(rows)
        avg_mfe = sum(r.get("mfe", 0) for r in rows) / len(rows)
        avg_target_rr = sum(r.get("target_rr", 0) for r in rows) / len(rows)
        avg_actual_rr = sum(r.get("actual_rr", 0) for r in rows) / len(rows)
        # 时间维度复盘：保本达成率 / 平均保本时长 / 平均止损时长 / 平均持仓时长
        be_rows = [r for r in rows if r.get("breakeven_reached")]
        be_rate = len(be_rows) / len(rows) * 100 if rows else 0.0
        avg_be_sec = (sum(r.get("breakeven_seconds", 0) for r in be_rows)
                      / len(be_rows)) if be_rows else 0.0
        stop_rows = [r for r in rows if r.get("stop_seconds", 0) > 0]
        avg_stop_sec = (sum(r.get("stop_seconds", 0) for r in stop_rows)
                        / len(stop_rows)) if stop_rows else 0.0
        avg_exit_sec = (sum(r.get("exit_seconds", 0) for r in rows)
                        / len(rows)) if rows else 0.0
        return {
            "count": len(rows),
            "win_rate": round(len(wins) / len(rows) * 100, 1),
            "avg_win": round(avg_win, 2),
            "avg_loss": round(avg_loss, 2),
            "profit_factor": pf,
            "total_pnl": round(total, 2),
            "expectancy": round(total / len(rows), 2),       # 每笔期望盈亏
            "avg_mae": round(avg_mae, 2),                    # 平均最大浮亏
            "avg_mfe": round(avg_mfe, 2),                    # 平均最大浮盈
            "avg_target_rr": round(avg_target_rr, 2),        # 平均目标 RR
            "avg_actual_rr": round(avg_actual_rr, 2),        # 平均真实 RR
            "breakeven_rate": round(be_rate, 1),             # 保本达成率 %
            "avg_breakeven_min": round(avg_be_sec / 60, 1),  # 平均保本时长（分钟）
            "avg_stop_min": round(avg_stop_sec / 60, 1),     # 平均止损时长（分钟）
            "avg_hold_min": round(avg_exit_sec / 60, 1),     # 平均持仓时长（分钟）
        }

    def _group(key) -> dict:
        out = {}
        for r in closed:
            k = r.get(key) or "未知"
            out.setdefault(k, []).append(r)
        return {k: _stats(v) for k, v in out.items()}

    return {
        "count": len(closed),
        "overall": _stats(closed),
        "by_type": _group("limit_type"),
        "by_side": _group("side"),
        "by_symbol": _group("symbol"),
        "records": closed,
    }


@app.get("/api/returns/compare")
async def api_returns_compare(start: str = "", end: str = "") -> dict:
    """按日期范围对比账户已实现日收益与 QQQ 收盘收益。"""
    from datetime import datetime as _dt
    from engine import returns_comparison as rc

    try:
        start_day, end_day = rc.parse_range(start, end)
    except ValueError as e:
        raise HTTPException(400, str(e))
    logs_dir = Path(__file__).resolve().parent.parent / "logs"
    daily_pnl, anchors = await asyncio.to_thread(rc.load_account_daily, logs_dir)

    status = await get_status_cached()
    now_et = _dt.now(rc.ET)
    equity = status.get("equity")
    today_pnl = status.get("daily_pnl")
    if (gateway._daily_pnl_session_active(now_et)
            and isinstance(equity, (int, float))
            and isinstance(today_pnl, (int, float))):
        sod_equity = equity - today_pnl
        if sod_equity > 0:
            today = now_et.date().isoformat()
            anchors[today] = sod_equity
            await asyncio.to_thread(rc.save_equity_anchor, logs_dir, today, sod_equity)

    qqq_closes, fetched_at = await asyncio.to_thread(rc.load_qqq_daily, logs_dir)
    needs_fetch = (not qqq_closes or time.time() - fetched_at > 6 * 3600
                   or min(qqq_closes) > start_day.isoformat())
    warnings = []
    if needs_fetch:
        try:
            bars = await rc.fetch_qqq_daily(
                Path(__file__).resolve().parent.parent,
                (end_day - start_day).days + 30)
            await asyncio.to_thread(rc.save_qqq_daily, logs_dir, bars)
            for row in bars:
                day = _dt.fromtimestamp(row["time"], rc.ET).date().isoformat()
                qqq_closes[day] = float(row["close"])
        except Exception as e:
            logger.warning("QQQ 日线刷新失败，使用本地缓存: %s", e)
            warnings.append(f"QQQ 日线刷新失败，当前展示本地缓存：{e}")

    rows, summary = rc.build_rows(start_day, end_day, daily_pnl, anchors, qqq_closes)
    if any(row["account_estimated"] for row in rows):
        warnings.append("部分日起始净值由最近净值锚点和平仓盈亏推算，未计入出入金及未归档交易")
    if not qqq_closes:
        warnings.append("暂无 QQQ 行情数据")
    return {
        "start": start_day.isoformat(),
        "end": end_day.isoformat(),
        "symbol": rc.QQQ_SYMBOL,
        "account_basis": "realized_pnl",
        "rows": rows,
        "summary": summary,
        "warnings": warnings,
    }


@app.get("/api/events")
async def api_events(event_type: Optional[str] = None, limit: int = 500) -> list:
    """查询历史事件（JSONL 持久日志，可回溯分析）。"""
    import glob
    import json as _json
    logs_dir = Path(__file__).resolve().parent.parent / "logs"
    records = []
    for f in sorted(glob.glob(str(logs_dir / "events-*.jsonl")), reverse=True):
        try:
            with open(f, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    r = _json.loads(line)
                    if event_type is None or r.get("type") == event_type:
                        records.append(r)
        except Exception:
            continue
    records.sort(key=lambda x: x.get("ts", 0), reverse=True)
    return records[:limit]


@app.get("/api/lots")
async def api_lots(active_only: bool = False, symbol: Optional[str] = None) -> List[dict]:
    return order_mgr.list_lots(active_only=active_only, symbol=symbol)


@app.get("/api/positions")
async def api_positions() -> List[dict]:
    return order_mgr.aggregate()


@app.post("/api/lots/limit-stop")
async def api_place_limit_stop(req: LimitStopOrderRequest,
                               trader: str = Depends(require_trader)) -> dict:
    """LMT 父单 + STP 子单下单；midpoint/retest 由服务端按已收盘 bar 定价。
    方向必须与 ts_c.direction（SuperTrend 方向）同向（1 多 / -1 空），震荡可开；
    force=执意开仓放行（需原因）。"""
    if req.side not in ("long", "short"):
        raise HTTPException(400, f"方向非法: {req.side}")
    if req.limit_type not in ("ema", "channel", "midpoint", "retest", "chase", "market"):
        raise HTTPException(400, f"不支持的限价类型: {req.limit_type}")
    if not config.ALLOW_SHORT and req.side == "short":
        raise HTTPException(400, "当前仅允许做多（空头已屏蔽）")
    # trend_state 过滤拦截（含 ATR 缺失检查；force=二次确认放行）
    _check_trend_filter(req.symbol, req.side, req.force, req.reason)
    limit_px, stop_px = req.limit_px, req.stop_px
    reprice_bar_time = 0.0
    breakout_px = 0.0
    if req.limit_type == "midpoint":
        data = (collector.latest() or {}).get(req.symbol, {})
        try:
            limit_px, stop_px, breakout_px, bar, _ = _midpoint_prices(
                data, req.side)
        except ValueError as e:
            raise HTTPException(400, str(e))
        reprice_bar_time = bar["time"]
    elif req.limit_type == "retest":
        # 服务端按 bar_c2 定价（不信浏览器价）：多 limit=bar_c2.high /
        # stop=bar_c2.low，空头镜像；bar_c2 缺失直接拒绝，不静默算错。
        data = (collector.latest() or {}).get(req.symbol, {})
        try:
            limit_px, stop_px, bar = _retest_prices(data, req.side)
        except ValueError as e:
            raise HTTPException(400, str(e))
        reprice_bar_time = bar["time"]
    elif req.limit_type == "chase":
        # FVG 追价：服务端按最新 FVG 事件边沿 + 实时价定价（不信浏览器价）；
        # 偏离边沿过远 / 无 FVG 事件 / ATR 缺失直接拒绝。reprice_bar_time 与
        # breakout_px 保持 0（一次定价，不参与逐 bar 改价与突破腿）。
        data = (collector.latest() or {}).get(req.symbol, {})
        try:
            limit_px, stop_px = _fvg_chase_prices(data, req.side, req.symbol)
        except ValueError as e:
            raise HTTPException(400, str(e))
    elif req.limit_type == "market":
        # 市价单：服务端按实时价 ×(1±0.5%) 定价（不信浏览器价），
        # 止损 = 现价 ∓ N×ATR（N = 全局止损倍数）；一次定价，不改价不突破腿。
        data = (collector.latest() or {}).get(req.symbol, {})
        try:
            limit_px, stop_px = _market_prices(data, req.side)
        except ValueError as e:
            raise HTTPException(400, str(e))
    qty = req.qty
    risk_ref_px = 0.0
    if req.limit_type in ("market", "chase"):
        # marketable 单（market/chase）：服务端以损定量拥有股数——
        # 溢价限价只做券商成交保护，风险/股数口径用预期成交价（现价）。
        #（BA 实锤：0.5% 溢价 ≈ 止损距离，按限价计风险被风控 10% 容差误杀）
        data2 = (collector.latest() or {}).get(req.symbol, {})
        risk_ref_px = (data2.get("bar") or {}).get("close") or 0
        dist = abs(risk_ref_px - stop_px) if risk_ref_px > 0 else 0
        if dist <= 0:
            raise HTTPException(400, "风险距离无效（现价/止损缺失），拒绝下单")
        equity = _equity_cache.get("equity") or 0
        risk_budget = req.risk_usd or config.RISK_PER_TRADE_USD
        q_risk = int(risk_budget / dist)
        q_pct = (int(equity * req.max_position_pct / 100 / limit_px)
                 if equity > 0 and req.max_position_pct > 0 else q_risk)
        qty = max(0, min(q_risk, q_pct, config.RISK_MAX_ORDER_QTY))
    if qty == 0:
        equity = await gateway.net_liquidation()
        qty = risk_gate.size_position(limit_px, stop_px, equity)
    if qty <= 0:
        raise HTTPException(400, "无法确定有效股数（请显式填写 qty）")

    lot, err = await order_mgr.place_limit_stop_lot(
        symbol=req.symbol, side=req.side, qty=qty,
        limit_px=limit_px, stop_px=stop_px,
        limit_type=req.limit_type, reason=req.reason,
        risk_usd=req.risk_usd,
        signal_snapshot=_snapshot_for(symbol=req.symbol),
        reprice_bar_time=reprice_bar_time,
        breakout_px=breakout_px,
        max_position_pct=req.max_position_pct,
        atr_mult=_stop_atr_mult(),   # 下单时全局止损倍数（lot 存续期内口径一致）
        trader=trader,
        risk_ref_px=risk_ref_px)
    if lot is None:
        raise HTTPException(400, err)
    record_event("order_placed", lot_id=lot.lot_id, symbol=req.symbol,
                 side=req.side, qty=qty, limit_px=limit_px,
                 stop_px=stop_px, limit_type=req.limit_type,
                 breakout_px=breakout_px, trader=trader,
                 risk_usd=req.risk_usd, force=req.force, reason=req.reason)
    return {"ok": True, "lot": lot.to_dict()}


@app.post("/api/lots/{lot_id}/trail")
async def api_update_trail(lot_id: str, req: UpdateTrailRequest,
                           trader: str = Depends(require_trader)) -> dict:
    ok, err = await order_mgr.update_trail(lot_id, req.stop_px)
    if not ok:
        raise HTTPException(400, err)
    return {"ok": True}


@app.post("/api/lots/{lot_id}/cancel")
async def api_cancel_lot(lot_id: str,
                         trader: str = Depends(require_trader)) -> dict:
    ok, err = await order_mgr.cancel_lot(lot_id)
    record_event("order_cancelled" if ok else "order_cancel_failed",
                 lot_id=lot_id, error=(err if not ok else ""))
    if not ok:
        raise HTTPException(400, err)
    return {"ok": True}


@app.post("/api/lots/{lot_id}/close")
async def api_close_lot(lot_id: str,
                        trader: str = Depends(require_trader)) -> dict:
    """提交平仓并进入 CLOSING；实际成交由 close watcher 记录。"""
    ok, err = await order_mgr.close_lot(lot_id)
    record_event("order_close_requested" if ok else "order_close_failed",
                 lot_id=lot_id, error=(err if not ok else ""))
    if not ok:
        raise HTTPException(400, err)
    return {"ok": True}


@app.post("/api/lots/{lot_id}/breakeven")
async def api_breakeven_lot(lot_id: str,
                            trader: str = Depends(require_trader)) -> dict:
    """手动保本：止损移到实际成本价（与自动 2R 保本同一逻辑）。"""
    ok, err = await order_mgr.breakeven(lot_id)
    record_event("manual_breakeven" if ok else "manual_breakeven_failed",
                 lot_id=lot_id, error=(err if not ok else ""))
    if not ok:
        raise HTTPException(400, err)
    return {"ok": True}


@app.post("/api/lots/{lot_id}/close-half")
async def api_close_half(lot_id: str, trader: str = Depends(require_trader)) -> dict:
    """平仓一半：收缩 STOP 保护单到剩余量 + marketable 平一半；lot 保持 ACTIVE。"""
    ok, err = await order_mgr.close_half(lot_id)
    record_event("order_close_half" if ok else "order_close_half_failed",
                 lot_id=lot_id, error=(err if not ok else ""))
    if not ok:
        raise HTTPException(400, err)
    return {"ok": True}


@app.post("/api/positions/{symbol}/close")
async def api_close_position(symbol: str,
                             trader: str = Depends(require_trader)) -> dict:
    """按 symbol 处理本工具 lot；外部持仓仅由外部清理循环处理。"""
    ticker = symbol.split(":")[-1]
    lots = order_mgr.list_lots(active_only=True, symbol=symbol)
    if not lots:
        raise HTTPException(400, f"{ticker} 没有本工具管理的订单或持仓")
    results = []
    errors = []
    for lot in lots:
        if lot["state"] == "PENDING":
            ok, err = await order_mgr.cancel_lot(lot["lot_id"])
            action = "cancel"
        elif lot["state"] == "ACTIVE":
            snap = (collector.latest() or {}).get(symbol, {})
            ref_px = (snap.get("bar") or {}).get("close")
            ok, err = await order_mgr.close_lot(
                lot["lot_id"], ref_px=ref_px, exit_reason="manual")
            action = "close"
        else:
            continue
        results.append({"lot_id": lot["lot_id"], "action": action, "ok": ok})
        if not ok:
            errors.append(f"{lot['lot_id']}: {err}")
    if errors:
        raise HTTPException(400, "; ".join(errors))
    _scan_alerts.pop(ticker, None)
    qty = sum(l.get("filled_qty", 0) for l in lots if l["state"] == "ACTIVE")
    side = lots[0]["side"] if lots else ""
    record_event("position_close_requested", symbol=symbol, results=results)
    return {"ok": True, "symbol": symbol, "side": side, "qty": qty,
            "results": results}


@app.post("/api/refresh")
async def api_refresh() -> dict:
    _refresh_marks_from_tv()   # mark 价一律用 TV（不用 TWS 行情）
    return await build_snapshot()


# ---------------------------------------------------------------------------
# WebSocket
# ---------------------------------------------------------------------------

@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket) -> None:
    await ws.accept()
    _ws_clients.add(ws)
    try:
        await ws.send_json(await build_snapshot())
        while True:
            # 只收不发（客户端心跳/未来指令可在此处理）
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        _ws_clients.discard(ws)


# ---------------------------------------------------------------------------
# 静态前端
# ---------------------------------------------------------------------------

@app.get("/api/startup", include_in_schema=False)
async def api_startup() -> dict:
    return _startup_status()


@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    name = "index.html" if _startup_state["ready"] else "loading.html"
    return FileResponse(STATIC_DIR / name, headers={"Cache-Control": "no-store"})


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.middleware("http")
async def _no_cache_static(request, call_next):
    """静态文件禁缓存；系统关闭期间拒绝一切新的写请求。"""
    if (_system_stopping and request.method not in ("GET", "HEAD", "OPTIONS")
            and request.url.path != "/api/system/shutdown"):
        return JSONResponse({"detail": "系统正在关闭，已停止交易控制"}, status_code=503)
    response = await call_next(request)
    if request.url.path.startswith("/static"):
        response.headers["Cache-Control"] = "no-cache"
    return response


# ---------------------------------------------------------------------------
# 盘前新闻摘要（engine/news_digest.py：RSS 采集 + LLM 主题归纳 + 自选股热点）
# 每天 07:30 ET 自动生成一次；手动刷新冷却 10 分钟。独立 startup 注册，
# 不侵入既有函数体。
# ---------------------------------------------------------------------------
import datetime as _dt
from zoneinfo import ZoneInfo as _ZoneInfo

_ZI_ET = _ZoneInfo("America/New_York")

from engine.live_news import LiveNewsService
_live_news = LiveNewsService()

_news_task: Optional[asyncio.Task] = None
_news_cache: dict = {}              # ET 日期串(YYYYMMDD) -> payload（含落盘文件懒加载）
_news_last_manual: float = 0.0      # 手动刷新冷却起点
NEWS_REFRESH_COOLDOWN_S = 600       # 手动刷新冷却 10 分钟
NEWS_AUTO_HM = 7 * 60 + 30          # 自动生成时刻 07:30 ET


def _news_tickers() -> List[str]:
    """watchlist 去重 ticker（剔除期货/CFD 前缀——个股新闻查股票）。"""
    return sorted({s.split(":")[-1] for s in config.WATCHLIST
                   if not s.startswith(("BLACKBULL:", "CAPITALCOM:"))})


def _news_today() -> Optional[dict]:
    """今日（ET）摘要：内存缓存优先，未命中懒加载落盘文件并缓存。"""
    day = _dt.datetime.now(_ZI_ET).strftime("%Y%m%d")
    if day not in _news_cache:
        _news_cache[day] = news_digest.load_digest(day)
    return _news_cache[day]


def _news_status() -> dict:
    """盘前热点 + 新浪实时快讯标签状态（实时部分只读内存缓存）。"""
    live = _live_news.status()
    p = _news_today()
    if not p:
        return {"generated_at": None, "theme_count": 0, "hot_count": 0,
                "hot": {}, **live}
    summary = p.get("summary") or {}
    hot = {k: (v.get("gist") or "") if isinstance(v, dict) else ""
           for k, v in (summary.get("hot_symbols") or {}).items()}
    themes = summary.get("themes") or []
    return {"generated_at": p.get("generated_at"),
            "generated_at_et": p.get("generated_at_et"),
            "theme_count": len(themes), "hot_count": len(hot),
            "hot": hot, "degraded": summary == {} and not p.get("summary"),
            **live}


async def _news_generate(reason: str) -> Optional[dict]:
    """在线程池跑完整采集+摘要（阻塞 IO 不进事件循环），落盘并更新缓存。"""
    tickers = _news_tickers()
    try:
        payload = await asyncio.to_thread(news_digest.generate, tickers)
        await asyncio.to_thread(news_digest.save_digest, payload)
    except Exception as e:
        logger.exception("盘前新闻生成失败（%s）: %s", reason, e)
        record_event("news_digest_failed", reason=reason, error=str(e))
        return None
    day = _dt.datetime.fromtimestamp(payload["generated_at"],
                                     _ZI_ET).strftime("%Y%m%d")
    _news_cache[day] = payload
    summary = payload.get("summary") or {}
    meta = payload.get("summary_meta") or {}
    logger.warning("盘前新闻已生成（%s）：主题 %d 热点 %d，LLM=%s，耗时 %ss",
                   reason, len(summary.get("themes") or []),
                   len(summary.get("hot_symbols") or {}),
                   meta.get("model") or "降级", payload.get("elapsed_s"))
    record_event("news_digest", reason=reason,
                 generated_at=payload["generated_at"],
                 theme_count=len(summary.get("themes") or []),
                 hot_count=len(summary.get("hot_symbols") or {}),
                 hot=sorted((summary.get("hot_symbols") or {}).keys()),
                 model=meta.get("model"), degraded=not payload.get("summary"),
                 elapsed_s=payload.get("elapsed_s"))
    return payload


async def _news_loop() -> None:
    """盘前新闻自动生成（每 60s 检查）：07:30 ET 后当天无摘要 → 生成一次。"""
    while True:
        await asyncio.sleep(60.0)
        try:
            now = _dt.datetime.now(_ZI_ET)
            if now.hour * 60 + now.minute < NEWS_AUTO_HM:
                continue
            if _news_today() is not None:
                continue
            await _news_generate("auto_0730")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.exception("盘前新闻循环异常: %s", e)


@app.on_event("startup")
async def _news_startup() -> None:
    global _news_task
    _news_task = asyncio.create_task(_news_loop())
    await _live_news.start()


@app.on_event("shutdown")
async def _news_shutdown() -> None:
    if _news_task:
        _news_task.cancel()
    await _live_news.stop()


@app.get("/api/news/live")
async def api_live_news(ticker: str, minutes: int = 15) -> dict:
    """新浪 7×24 个股快讯，默认最近 15 分钟。"""
    if not ticker.strip():
        raise HTTPException(400, "ticker 不能为空")
    return _live_news.news(ticker, minutes=minutes)


@app.get("/api/news")
async def api_news() -> dict:
    """今日盘前新闻摘要；无数据返回空态。"""
    payload = _news_today()
    if not payload:
        return {"empty": True}
    return payload


@app.post("/api/news/refresh")
async def api_news_refresh(trader: str = Depends(require_trader)) -> dict:
    """手动刷新（冷却 10 分钟，全客户端共享）。"""
    global _news_last_manual
    wait = NEWS_REFRESH_COOLDOWN_S - (time.time() - _news_last_manual)
    if wait > 0:
        raise HTTPException(429, f"刷新冷却中，{int(wait)}s 后再试")
    _news_last_manual = time.time()
    payload = await _news_generate(f"manual_by_{trader}")
    if payload is None:
        raise HTTPException(500, "生成失败（详见日志）")
    return {"ok": True, "generated_at": payload["generated_at"],
            "generated_at_et": payload["generated_at_et"]}


# ---------------------------------------------------------------------------
# 盘前报告工作流（logs/reports/premarket-*.md 历史列表 + 异步生成）
# 生成是长任务（采集 ~2 分钟 + LLM ~16s）：asyncio task 后台跑，前端轮询
# /api/reports/status 做进度态。只追加，不侵入既有函数体。
# ---------------------------------------------------------------------------

_REPORTS_DIR = Path(__file__).resolve().parent.parent / "logs" / "reports"
_REPORT_DATE_RE = re.compile(r"^\d{8}$")
REPORT_GEN_COOLDOWN_S = 600          # 同一报告 10 分钟内不重复生成
_report_task: Optional[asyncio.Task] = None
_report_state = {"running": False, "started_at": 0.0,
                 "last_result": None, "last_error": None}
_report_last_done: float = 0.0       # 最近一次生成成功时刻（冷却用）


def _report_meta_from_text(text: str) -> dict:
    """从报告文件头解析元信息（条目数/模型/degraded/耗时/tokens）。"""
    meta: dict = {"degraded": "⚠️ LLM 降级" in text}
    m = re.search(r"条目：大盘 (\d+) 条 / 自选股 (\d+) 票 (\d+) 条", text)
    if m:
        meta["market_items"], meta["ticker_count"], meta["ticker_items"] = \
            int(m.group(1)), int(m.group(2)), int(m.group(3))
    m = re.search(r"模型：(\S+) · 耗时 ([\d.]+)s · tokens (\S+)", text)
    if m:
        meta["model"], meta["latency_s"], meta["tokens"] = \
            m.group(1), float(m.group(2)), m.group(3)
    return meta


def _report_file(date: str) -> Optional[Path]:
    """date（YYYYMMDD，正则硬校验防路径穿越）→ 报告文件路径或 None。"""
    if not _REPORT_DATE_RE.match(date):
        return None
    path = (_REPORTS_DIR / f"premarket-{date}.md").resolve()
    if _REPORTS_DIR.resolve() not in path.parents:
        return None
    return path if path.exists() else None


@app.get("/api/reports")
async def api_reports_list() -> dict:
    """历史报告列表（日期倒序，含文件头元信息）。"""
    rows = []
    if _REPORTS_DIR.exists():
        for p in _REPORTS_DIR.glob("premarket-*.md"):
            date = p.stem.replace("premarket-", "")
            if not _REPORT_DATE_RE.match(date):
                continue
            try:
                head = p.read_text(encoding="utf-8")[:2000]
            except OSError:
                continue
            rows.append({"date": date, "mtime": p.stat().st_mtime,
                         "meta": _report_meta_from_text(head)})
    rows.sort(key=lambda r: -r["mtime"])
    rows.sort(key=lambda r: r["date"], reverse=True)
    return {"reports": rows}


@app.get("/api/reports/status")
async def api_reports_status() -> dict:
    """生成进度：running/started_at/elapsed_s/last_result/last_error。"""
    st = dict(_report_state)
    st["elapsed_s"] = (round(time.time() - st["started_at"], 1)
                       if st["running"] else None)
    return st


@app.get("/api/reports/{date}")
async def api_reports_get(date: str) -> dict:
    """该日报告 markdown 全文（date 格式校验，防路径穿越）。"""
    if not _REPORT_DATE_RE.match(date):
        raise HTTPException(400, "date 必须是 YYYYMMDD")
    path = _report_file(date)
    if path is None:
        raise HTTPException(404, f"无 {date} 的报告")
    text = path.read_text(encoding="utf-8")
    return {"date": date, "markdown": text,
            "meta": _report_meta_from_text(text[:2000])}


async def _report_generate_run(day: str, trader: str) -> None:
    """后台生成任务：采集+LLM 全部 to_thread，完成/失败更新 _report_state。"""
    global _report_last_done
    try:
        tickers = _news_tickers()
        ticker_set = set(tickers)

        def _work():
            market = news_digest.collect_market(ticker_set=ticker_set)
            ticker_news = news_digest.collect_tickers(tickers)
            # 视频源（enabled 的）：字幕抓取 + LLM 摘要（缓存命中不重复调）
            from engine import youtube_digest
            window = (time.time() - 18 * 3600, time.time())
            videos = youtube_digest.process_videos(
                _report_enabled_urls(), window=window, ticker_set=ticker_set)
            md, meta = news_digest.generate_report(day, market, ticker_news,
                                                   videos=videos)
            path = news_digest.save_report(day, md, market, ticker_news, meta,
                                           videos=videos)
            return path, meta, market, ticker_news, videos

        path, meta, market, ticker_news, videos = await asyncio.to_thread(_work)
        _report_apply_video_status(videos)
        _report_last_done = time.time()
        result = {"date": day.replace("-", ""),
                  "path": str(path), "degraded": bool(meta.get("degraded")),
                  "model": meta.get("model"),
                  "tokens": (meta.get("usage") or {}).get("total_tokens"),
                  "latency_s": meta.get("latency_s"),
                  "market_items": len(market),
                  "ticker_count": len(ticker_news),
                  "finished_at": time.time()}
        _report_state["last_result"] = result
        _report_state["last_error"] = None
        logger.warning("盘前报告已生成（by %s）: %s（%s）", trader, path,
                       "降级" if result["degraded"] else result["model"])
        record_event("news_report_generated", trader=trader, **result)
    except Exception as e:
        logger.exception("盘前报告生成失败")
        _report_state["last_error"] = str(e)
        record_event("news_report_failed", trader=trader, error=str(e))
    finally:
        _report_state["running"] = False


@app.post("/api/reports/generate")
async def api_reports_generate(trader: str = Depends(require_trader)) -> dict:
    """生成今日报告（异步长任务）：立即返回 running=true；在跑 → 409；
    10 分钟冷却内 → 429。"""
    global _report_task
    if _report_state["running"]:
        raise HTTPException(409, "报告生成中，请等待完成")
    wait = REPORT_GEN_COOLDOWN_S - (time.time() - _report_last_done)
    if wait > 0:
        raise HTTPException(429, f"生成冷却中，{int(wait)}s 后再试")
    day = _dt.datetime.now(_ZI_ET).strftime("%Y-%m-%d")
    _report_state["running"] = True
    _report_state["started_at"] = time.time()
    _report_state["last_error"] = None
    _report_task = asyncio.create_task(_report_generate_run(day, trader))
    return {"ok": True, "running": True, "day": day}


# ---------------------------------------------------------------------------
# 报告视频源管理（logs/report_sources.json）：
# 用户贴 YouTube 链接，生成报告时把字幕摘要并入（第六章节「盘前视频观点」）。
# 只追加，不侵入既有函数体。
# ---------------------------------------------------------------------------

_REPORT_SOURCES_PATH = (Path(__file__).resolve().parent.parent
                        / "logs" / "report_sources.json")


def _load_report_sources() -> list:
    """加载视频源列表；缺文件/损坏返回 []。"""
    try:
        payload = json.loads(_REPORT_SOURCES_PATH.read_text(encoding="utf-8"))
        if isinstance(payload, list):
            return payload
    except FileNotFoundError:
        pass
    except (OSError, ValueError) as e:
        logger.warning("report_sources.json 加载失败: %s", e)
    return []


def _save_report_sources(sources: list) -> None:
    """原子写落盘（tmp + os.replace）。"""
    try:
        _REPORT_SOURCES_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = _REPORT_SOURCES_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(sources, ensure_ascii=False, indent=1),
                       encoding="utf-8")
        os.replace(tmp, _REPORT_SOURCES_PATH)
    except OSError:
        logger.exception("report_sources.json 落盘失败")


def _report_enabled_urls() -> list:
    return [s["url"] for s in _load_report_sources() if s.get("enabled")]


def _report_apply_video_status(videos: list) -> None:
    """生成报告后把各视频的处理状态回写进源列表（失败原因前端可见）。"""
    if not videos:
        return
    sources = _load_report_sources()
    by_id = {v.get("video_id"): v for v in videos if v.get("video_id")}
    dirty = False
    for s in sources:
        v = by_id.get(s.get("video_id"))
        if v is None:
            continue
        s["last_status"] = v.get("status")
        s["last_error"] = v.get("error") or ""
        if v.get("title"):
            s["title"] = v["title"]
        if v.get("channel"):
            s["channel"] = v["channel"]
        dirty = True
    if dirty:
        _save_report_sources(sources)


class ReportSourceAddRequest(BaseModel):
    url: str = Field(..., description="YouTube 链接（watch?v=/youtu.be//shorts/）")


@app.get("/api/report/sources")
async def api_report_sources() -> dict:
    """视频源列表。"""
    return {"sources": _load_report_sources()}


async def _source_fill_meta(video_id: str) -> None:
    """后台补视频元数据（title/channel）；失败原因写进 last_error。"""
    from engine import youtube_digest
    sources = _load_report_sources()
    row = next((s for s in sources if s.get("video_id") == video_id), None)
    if row is None:
        return
    try:
        meta = await asyncio.to_thread(youtube_digest.fetch_metadata, row["url"])
        row["title"] = meta.get("title") or ""
        row["channel"] = meta.get("channel") or ""
        row["last_status"] = "meta_ok"
        row["last_error"] = ""
    except Exception as e:
        row["last_status"] = "meta_failed"
        row["last_error"] = str(e)
        logger.warning("视频源元数据抓取失败 %s: %s", video_id, e)
    _save_report_sources(sources)


@app.post("/api/report/sources")
async def api_report_source_add(req: ReportSourceAddRequest,
                                trader: str = Depends(require_trader)) -> dict:
    """添加视频源（幂等：同 video_id 返回已有行）；异步补元数据。"""
    from engine import youtube_digest
    url = req.url.strip()
    video_id = youtube_digest.parse_video_id(url)
    if not video_id:
        raise HTTPException(400, "无法解析 YouTube video_id（支持 watch?v=/"
                                 "youtu.be//shorts/ 三种形态）")
    sources = _load_report_sources()
    for s in sources:
        if s.get("video_id") == video_id:
            return {"ok": True, "source": s, "duplicated": True}
    row = {"url": url, "video_id": video_id, "title": "", "channel": "",
           "added_at": time.time(), "enabled": True,
           "last_status": "pending", "last_error": ""}
    sources.append(row)
    _save_report_sources(sources)
    record_event("report_source_added", video_id=video_id, url=url,
                 trader=trader)
    asyncio.create_task(_source_fill_meta(video_id))   # 后台补元数据
    return {"ok": True, "source": row}


@app.post("/api/report/sources/{video_id}/toggle")
async def api_report_source_toggle(video_id: str,
                                   trader: str = Depends(require_trader)) -> dict:
    """启用/停用翻转。"""
    sources = _load_report_sources()
    row = next((s for s in sources if s.get("video_id") == video_id), None)
    if row is None:
        raise HTTPException(404, "视频源不存在")
    row["enabled"] = not row.get("enabled", True)
    _save_report_sources(sources)
    return {"ok": True, "enabled": row["enabled"]}


@app.post("/api/report/sources/{video_id}/delete")
async def api_report_source_delete(video_id: str,
                                   trader: str = Depends(require_trader)) -> dict:
    """删除视频源。"""
    sources = _load_report_sources()
    new = [s for s in sources if s.get("video_id") != video_id]
    if len(new) == len(sources):
        raise HTTPException(404, "视频源不存在")
    _save_report_sources(new)
    record_event("report_source_deleted", video_id=video_id, trader=trader)
    return {"ok": True}
