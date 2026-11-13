# -*- coding: utf-8 -*-
"""
IBKR 网关适配层 (IBKRGateway)
=============================

基于 ib_async（ib_insync 的官方后继）封装 TWS 连接与下单。
实战经验继承自同级项目 nautilus_ibkr_helloworld（详见 PROJECT_NOTES 8.1）：

- TWS 实盘端口 7496；client_id 与 nautilus 项目错开（默认 11）；
- FA 账户组：order.faGroup = 组名，faMethod 留空（带值被拒 10226/321）；
- Pine 自动入场使用 MKT DAY 父单 + STP GTC 保护子单；手动限价与历史
  marketable LIMIT 入口继续保留。延迟行情标的仍受交易黑名单约束；
- 保护止损 STOP_MARKET GTC + 止盈 LIMIT GTC，OCA 组联动（ocaType=1）；
- IBKR 的 order error 不一定事件化 → submit 后 2.5s 主动核对订单状态；
- dry-run 门控：enabled=False 时所有下单动作只记日志，返回伪造句柄，
  上层（PositionManager）流程不变，便于全链路无风险演练。
"""

import asyncio
import fcntl
import functools
import inspect
import itertools
import json
import logging
import os
import threading
import time
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional
from zoneinfo import ZoneInfo

import config
from ib_async import ExecutionFilter

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# IBKR order error 中需要立刻视为下单失败的错误码
_FATAL_ERROR_CODES = {
    10226,  # FA allocation method 无效
    10349,  # 延迟行情下拒绝 bare MARKET
    201,    # Order rejected
    202,    # Order cancelled
}


_IBKR_CONTROL_METHODS = (
    "place_bracket", "place_market_with_stop", "place_limit_with_stop",
    "place_midpoint_oca", "place_close", "place_stop_only", "modify_stop",
    "modify_stop_qty", "modify_limit_stop", "modify_limit_stop_qty",
    "modify_midpoint_oca", "convert_entry_to_market", "cancel_order",
)


def _guard_ibkr_control_methods(cls):
    """系统关闭后拒绝所有券商写操作，并跟踪进行中的调用供关闭流程等待。"""
    for name in _IBKR_CONTROL_METHODS:
        method = getattr(cls, name)
        if inspect.iscoroutinefunction(method):
            @functools.wraps(method)
            async def async_guard(self, *args, __method=method, **kwargs):
                self._begin_control()
                try:
                    return await __method(self, *args, **kwargs)
                finally:
                    self._active_controls -= 1
            setattr(cls, name, async_guard)
        else:
            @functools.wraps(method)
            def sync_guard(self, *args, __method=method, **kwargs):
                self._begin_control()
                try:
                    return __method(self, *args, **kwargs)
                finally:
                    self._active_controls -= 1
            setattr(cls, name, sync_guard)
    return cls


@_guard_ibkr_control_methods
class IBKRGateway:
    """
    用法::

        gw = IBKRGateway.from_config()
        await gw.connect()          # dry-run 模式下也建议连接（只读），失败不致命
        res = await gw.place_entry(unit)      # dict，含 ok/status/order_id/trade
    """

    supports_order_refs = True

    def __init__(self, host: str, port: int, client_id: int,
                 account_id: str, fa_group: str, fa_method: str = "",
                 enabled: bool = False) -> None:
        self.host = host
        self.port = port
        self.client_id = client_id
        self.account_id = account_id
        self.fa_group = fa_group
        self.fa_method = fa_method
        self.enabled = enabled
        self.on_entry_stage = None
        self.on_order_audit = None
        self.ib = None          # ib_async.IB，connect 后可用
        self._connected = False
        self.connected_at = 0.0
        self.ibkr_time = 0.0                # IBKR 服务器时间戳（心跳探测时更新）
        self._heartbeat_task = None          # 心跳监控任务
        self.auto_reconnect = True           # 断线后自动重连
        self._reconnect_task = None          # 重连任务
        self.disconnect_callback = None      # 断线回调（上层订阅）
        self._errors: List[dict] = []
        self._fake_ids = itertools.count(900000)   # dry-run 伪造订单号
        self._tool_order_id_next: Optional[int] = None
        self._tool_order_id_lock = threading.Lock()
        self._control_allowed = True
        self._active_controls = 0

    def _begin_control(self) -> None:
        if not getattr(self, "_control_allowed", True):
            raise RuntimeError("系统正在关闭，已禁止 IBKR 交易控制")
        self._active_controls = getattr(self, "_active_controls", 0) + 1

    async def disable_control(self) -> None:
        """永久关闭本进程的交易门，等进行中的写调用结束后断开 API。"""
        self._control_allowed = False
        self.auto_reconnect = False
        while getattr(self, "_active_controls", 0):
            await asyncio.sleep(0.05)
        await self.disconnect()

    @classmethod
    def from_config(cls) -> "IBKRGateway":
        return cls(
            host=config.IBKR_HOST, port=config.IBKR_PORT,
            client_id=config.IBKR_CLIENT_ID,
            account_id=config.IBKR_ACCOUNT_ID,
            fa_group=config.IBKR_FA_GROUP, fa_method=config.IBKR_FA_METHOD,
            enabled=config.EXECUTION_ENABLED,
        )

    # ------------------------------------------------------------------
    # 连接
    # ------------------------------------------------------------------

    async def connect(self, timeout: float = 15.0) -> bool:
        """连接 TWS。dry-run 模式下连接失败仅告警（不阻塞全流程演练）。"""
        from ib_async import IB
        # 重连时取消旧的心跳任务
        if self._heartbeat_task and not self._heartbeat_task.done():
            self._heartbeat_task.cancel()
            self._heartbeat_task = None
        self.ib = IB()
        self.ib.errorEvent += self._on_error
        self.ib.disconnectedEvent += self._on_disconnect
        if self.on_order_audit is not None:
            for event, action in (
                (self.ib.newOrderEvent, "submitted"),
                (self.ib.orderModifyEvent, "modified"),
                (self.ib.cancelOrderEvent, "cancel_requested"),
                (self.ib.orderStatusEvent, "status"),
                (self.ib.execDetailsEvent, "fill"),
            ):
                event.connect(functools.partial(self._audit_trade, action), keep_ref=True)
        try:
            await self.ib.connectAsync(
                self.host, self.port, clientId=self.client_id,
                timeout=timeout, account=self.account_id)
            self.connected_at = time.time()
            self._connected = True
            accounts = self.ib.managedAccounts()
            logger.info("IBKR 已连接: %s:%d clientId=%d 账户=%s (执行=%s)",
                        self.host, self.port, self.client_id, accounts,
                        "实盘" if self.enabled else "dry-run")
            # 启动心跳监控：每 2s 探测，5s 内检测断线（含 TWS 假死）
            self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
            return True
        except Exception as e:
            self._connected = False
            logger.warning("IBKR 连接失败 (%s): %s",
                           type(e).__name__, e)
            return False

    async def disconnect(self) -> None:
        self.auto_reconnect = False
        if self._reconnect_task and not self._reconnect_task.done():
            self._reconnect_task.cancel()
            self._reconnect_task = None
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
            self._heartbeat_task = None
        if self.ib and self._connected:
            try:
                self.ib.disconnect()
            except Exception:
                pass
        self._connected = False

    def _on_disconnect(self) -> None:
        """ib_async disconnectedEvent：socket 断开即时触发（毫秒级）。"""
        if self._connected:
            self._connected = False
            logger.warning("IBKR 连接断开（disconnectedEvent）")
            self._notify_disconnect()
        if self.auto_reconnect:
            self._start_reconnect()

    def _start_reconnect(self) -> None:
        """启动重连任务（幂等）。"""
        if self._reconnect_task and not self._reconnect_task.done():
            return
        self._reconnect_task = asyncio.create_task(self._reconnect_loop())

    async def _reconnect_loop(self) -> None:
        """断线后每 5s 退避重试，直到重连成功或 auto_reconnect 关闭。"""
        while not self._connected and self.auto_reconnect:
            await asyncio.sleep(5.0)
            if self._connected or not self.auto_reconnect:
                break
            logger.info("尝试重连 IBKR ...")
            try:
                ok = await self.connect()
                if ok:
                    logger.info("IBKR 重连成功")
                    break
            except Exception as e:
                logger.warning("重连尝试失败: %s", e)

    async def _heartbeat_loop(self) -> None:
        """每 2s 探测一次连接，TWS 假死（socket 未断但无响应）5s 内判定断线。"""
        while True:
            await asyncio.sleep(2.0)
            if not self._connected:
                continue
            try:
                # reqCurrentTime 应用层探测：能检测 socket 未断但 TWS 卡死的情况
                t = await asyncio.wait_for(self.ib.reqCurrentTimeAsync(), timeout=3.0)
                self.ibkr_time = t.timestamp() if t else 0.0   # 记录 IBKR 服务器时钟
            except Exception:
                if self._connected:
                    self._connected = False
                    logger.warning("IBKR 心跳超时，判定断线")
                    self._notify_disconnect()

    def _notify_disconnect(self) -> None:
        if self.disconnect_callback:
            try:
                self.disconnect_callback()
            except Exception:
                logger.exception("断线回调异常")

    def _audit_trade(self, action, trade, fill=None) -> None:
        if self.on_order_audit is None:
            return
        try:
            order = trade.order
            status = trade.orderStatus
            data = {
                "action": action, "client_id": order.clientId,
                "order_id": order.orderId, "perm_id": order.permId,
                "parent_id": order.parentId, "order_ref": order.orderRef,
                "symbol": trade.contract.symbol, "side": order.action,
                "order_type": order.orderType, "qty": order.totalQuantity,
                "limit_px": order.lmtPrice, "aux_px": order.auxPrice,
                "oca_group": order.ocaGroup, "tif": order.tif,
                "status": status.status, "filled_qty": status.filled,
                "remaining_qty": status.remaining, "avg_fill": status.avgFillPrice,
            }
            if order.orderRef.startswith("autotrade:"):
                data["lot_id"] = order.orderRef.split(":", 2)[1]
            if fill is not None:
                fill_execution = fill.execution
                data.update(
                    exec_id=fill_execution.execId, fill_time=fill_execution.time.isoformat(),
                    fill_qty=fill_execution.shares, fill_px=fill_execution.price,
                )
            self.on_order_audit("broker_order", **data)
        except Exception:
            logger.exception("券商订单审计回调失败")

    def _on_error(self, reqId, errorCode, errorString, contract=None) -> None:
        rec = {"reqId": reqId, "code": errorCode, "msg": errorString,
               "ts": time.time()}
        self._errors.append(rec)
        # 心跳/连接类提示不算错误
        if errorCode in (2100, 2102, 2104, 2106, 2107, 2158):
            logger.info("IBKR 提示 %s: %s", errorCode, errorString)
        else:
            logger.warning("IBKR error %s [reqId=%s]: %s",
                           errorCode, reqId, errorString)

    def _recent_fatal_error(self, since: float, order_ids=None) -> Optional[dict]:
        for e in reversed(self._errors):
            if (e["ts"] >= since and e["code"] in _FATAL_ERROR_CODES
                    and (order_ids is None or e["reqId"] in order_ids)):
                return e
        return None

    @staticmethod
    def is_tool_order_id(order_id) -> bool:
        """是否为本工具集中分配的 88 号段订单号。"""
        return (isinstance(order_id, int)
                and int(config.TOOL_ORDER_ID_BASE) <= order_id
                <= int(config.TOOL_ORDER_ID_MAX))

    def _next_tool_order_id(self) -> int:
        """跨进程原子分配并持久化下一个工具订单号。"""
        base = int(config.TOOL_ORDER_ID_BASE)
        maximum = int(config.TOOL_ORDER_ID_MAX)
        if base > maximum:
            raise RuntimeError("工具订单号段配置无效")
        state_path = Path(config.TOOL_ORDER_ID_STATE_PATH).expanduser()
        if not state_path.is_absolute():
            state_path = PROJECT_ROOT / state_path
        state_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = state_path.with_name(state_path.name + ".lock")
        with self._tool_order_id_lock:
            with lock_path.open("a+", encoding="utf-8") as lock_file:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
                try:
                    persisted_next = base
                    try:
                        with state_path.open("r", encoding="utf-8") as f:
                            state = json.load(f)
                        persisted_next = int(state["next_order_id"])
                    except FileNotFoundError:
                        pass
                    except (OSError, ValueError, TypeError, KeyError) as e:
                        raise RuntimeError(
                            f"工具订单号状态文件损坏: {state_path}") from e

                    local_next = self._tool_order_id_next
                    if local_next is None:
                        if self.ib is None or getattr(self.ib, "client", None) is None:
                            raise RuntimeError("IBKR 未连接，无法初始化工具订单号")
                        local_next = max(base, int(self.ib.client.getReqId()))
                    order_id = max(base, local_next, persisted_next)
                    if order_id > maximum:
                        raise RuntimeError(
                            f"工具订单号段已耗尽: {base}-{maximum}")

                    next_id = order_id + 1
                    tmp_path = state_path.with_name(
                        f".{state_path.name}.{os.getpid()}.{time.time_ns()}.tmp")
                    try:
                        with tmp_path.open("w", encoding="utf-8") as f:
                            json.dump({"next_order_id": next_id}, f)
                            f.write("\n")
                            f.flush()
                            os.fsync(f.fileno())
                        os.replace(tmp_path, state_path)
                    finally:
                        try:
                            tmp_path.unlink()
                        except FileNotFoundError:
                            pass
                    self._tool_order_id_next = next_id
                    return order_id
                finally:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    @staticmethod
    def _set_stop_price(order, stop_px: float) -> None:
        if order.orderType == "STP":
            order.auxPrice = stop_px
            order.trailStopPrice = 0.0
        else:
            order.trailStopPrice = stop_px
            order.auxPrice = 0.0

    def _modification_error(self, trades, since: float) -> Optional[str]:
        statuses = [(t, t.orderStatus.status) for t in trades]
        fatal = self._recent_fatal_error(since)
        if fatal:
            return fatal["msg"]
        for trade, status in statuses:
            if status not in ("PreSubmitted", "Submitted"):
                return (f"orderId={trade.order.orderId} 状态 {status}，"
                        "IBKR 未确认接受修改")
        return None

    # ------------------------------------------------------------------
    # 下单
    # ------------------------------------------------------------------

    def _apply_fa(self, order) -> None:
        """FA 账户组分配字段；faMethod 留空用组默认（实测 10226 坑）。"""
        if self.fa_group:
            order.faGroup = self.fa_group
            if self.fa_method:
                order.faMethod = self.fa_method

    async def _qualify(self, symbol: str):
        from ib_async import Stock
        ticker = symbol.split(":")[-1]
        contract = Stock(ticker, "SMART", "USD")
        await self.ib.qualifyContractsAsync(contract)
        return contract

    async def place_bracket(self, unit) -> dict:
        """
        ⚠️ 仅供旧 main.py 提案卡链路（execution/position.py）使用；
        控制台链路（OrderManager）已统一走 place_limit_with_stop（STP）。
        括号单同时下发：父单 = marketable LIMIT DAY 入场；
        子单 = TRAIL GTC 跟踪止损（初始触发价 = unit.stop_px（1M ST 线值），
        跟踪幅度 = unit.trail_amount（2×ATR）），parentId 挂到父单，
        父 transmit=False + 子 transmit=True 构成传输链，入场成交后
        TRAIL 立即生效——止损止盈一体，broker 端托管，引擎宕机也有效。
        返回 {ok, entry_order_id, trail_order_id, status, trade?, error?}。
        """
        premium = config.ENTRY_LIMIT_PREMIUM_PCT / 100.0
        sign = 1 if unit.side == "long" else -1
        lmt = round(unit.entry_px * (1 + premium * sign), 2)
        action = "BUY" if unit.side == "long" else "SELL"
        opp = "SELL" if unit.side == "long" else "BUY"

        if self.enabled and not self._connected:
            return {"ok": False, "error": "IBKR 未连接，入场未提交"}
        if not self.enabled:
            fake_entry = next(self._fake_ids)
            fake_trail = next(self._fake_ids)
            logger.info("[DRY-RUN] 括号单 %s: %s x%d @LMT %.2f (ref %.2f) + "
                        "TRAIL %s 初始触发 %.2f 跟踪幅度 %.2f | FA=%s",
                        unit.symbol, action, unit.qty, lmt, unit.entry_px,
                        opp, unit.stop_px, unit.trail_amount, self.fa_group)
            return {"ok": True, "dry_run": True, "entry_order_id": fake_entry,
                    "trail_order_id": fake_trail, "status": "DryRun",
                    "limit_price": lmt}

        from ib_async import LimitOrder, Order
        contract = await self._qualify(unit.symbol)
        # 显式分配 orderId，子单 parentId 才能正确关联父单
        parent = LimitOrder(action, unit.qty, lmt, tif="DAY",
                            orderId=self._next_tool_order_id(), transmit=False)
        self._apply_fa(parent)
        trail = Order()
        trail.orderType = "TRAIL"
        trail.action = opp
        trail.totalQuantity = unit.qty
        trail.tif = "GTC"
        trail.auxPrice = round(unit.trail_amount, 2)        # 跟踪幅度（美元，须对齐最小 tick，否则 error 110）
        trail.trailStopPrice = round(unit.stop_px, 2)       # 初始触发价
        trail.orderId = self._next_tool_order_id()
        trail.parentId = parent.orderId
        trail.transmit = True                   # 链尾传输，整组提交
        self._apply_fa(trail)
        t0 = time.time()
        entry_trade = self.ib.placeOrder(contract, parent)
        trail_trade = self.ib.placeOrder(contract, trail)
        # IBKR 的 order error 不一定事件化：submit 后 2.5s 主动核对状态
        await asyncio.sleep(2.5)
        status = entry_trade.orderStatus.status
        trail_status = trail_trade.orderStatus.status
        fatal = self._recent_fatal_error(t0)
        bad = ("Cancelled", "Inactive", "ApiCancelled")
        if fatal or status in bad or trail_status in bad:
            err = (fatal["msg"] if fatal else
                   f"入场 {status} / TRAIL {trail_status}")
            logger.error("括号单失败 %s: %s", unit.symbol, err)
            return {"ok": False, "entry_order_id": parent.orderId,
                    "status": status, "error": err}
        logger.info("括号单已提交 %s %s x%d @%.2f | TRAIL 触发 %.2f 幅度 %.2f "
                    "| entryId=%d(%s) trailId=%d(%s)",
                    unit.symbol, action, unit.qty, lmt,
                    unit.stop_px, unit.trail_amount,
                    parent.orderId, status, trail.orderId, trail_status)
        return {"ok": True, "entry_order_id": parent.orderId,
                "trail_order_id": trail.orderId, "status": status,
                "limit_price": lmt, "trade": entry_trade,
                "trail_trade": trail_trade}

    async def place_market_with_stop(self, symbol: str, side: str, qty: int,
                                     stop_px: float, order_ref: str = "") -> dict:
        """市价单（父，MKT DAY）+ 固定 STOP 止损（子，STP GTC，parentId 关联）。

        严格市价成交（v2026-10-24 用户定稿：只做实时行情标的、严格市价成交）。
        前置条件：本账户对该标的有实时行情订阅——延迟行情标的会被
        error 10349 拒（已在 TRADE_BLOCKLIST 层屏蔽）。parentId 语义与
        place_limit_with_stop 相同：父单未成交子单挂起，父单被撤子单随撤。
        """
        action = "BUY" if side == "long" else "SELL"
        opp = "SELL" if side == "long" else "BUY"

        if self.enabled and not self._connected:
            return {"ok": False, "error": "IBKR 未连接，入场未提交"}
        if not self.enabled:
            fake_entry = next(self._fake_ids)
            fake_stop = next(self._fake_ids)
            logger.info("[DRY-RUN] 市价+STOP 子单 %s: %s x%d @MKT | STOP %s %.2f | FA=%s",
                        symbol, action, qty, opp, stop_px, self.fa_group)
            return {"ok": True, "dry_run": True, "entry_order_id": fake_entry,
                    "stop_order_id": fake_stop, "status": "DryRun"}

        from ib_async import MarketOrder, Order
        def audit(stage, **details):
            if self.on_entry_stage is None:
                return
            try:
                self.on_entry_stage(
                    stage=stage, symbol=symbol, side=side, qty=qty,
                    lot_id=order_ref.removeprefix("autotrade:") if order_ref else None,
                    **details,
                )
            except Exception:
                logger.exception("券商发单审计回调失败 %s", order_ref)

        qualify_started_at = time.time()
        contract = await self._qualify(symbol)
        qualified_at = time.time()
        parent = MarketOrder(action, qty, tif="DAY",
                             orderId=self._next_tool_order_id(), transmit=False)
        parent.orderRef = order_ref + ":entry" if order_ref else ""
        self._apply_fa(parent)
        stop = Order()
        stop.orderType = "STP"
        stop.action = opp
        stop.totalQuantity = qty
        stop.tif = "GTC"
        stop.auxPrice = round(stop_px, 2)
        stop.orderId = self._next_tool_order_id()
        stop.parentId = parent.orderId
        stop.transmit = True
        stop.orderRef = order_ref + ":stop" if order_ref else ""
        self._apply_fa(stop)
        t0 = time.time()
        entry_trade = self.ib.placeOrder(contract, parent)
        stop_send_at = time.time()
        stop_trade = self.ib.placeOrder(contract, stop)
        audit(
            "sent_to_tws", order_id=parent.orderId, stop_order_id=stop.orderId,
            qualify_started_at=qualify_started_at, qualified_at=qualified_at,
            entry_send_at=t0, stop_send_at=stop_send_at,
        )
        await asyncio.sleep(2.5)
        status = entry_trade.orderStatus.status
        stop_status = stop_trade.orderStatus.status
        fatal = self._recent_fatal_error(t0)
        bad = ("Cancelled", "Inactive", "ApiCancelled")
        if fatal or status in bad or stop_status in bad:
            err = (fatal["msg"] if fatal else f"入场 {status} / STOP {stop_status}")
            logger.error("市价+STOP 单失败 %s: %s", symbol, err)
            return {"ok": False, "entry_order_id": parent.orderId,
                    "stop_order_id": stop.orderId,
                    "status": status, "stop_status": stop_status,
                    "trade": entry_trade, "stop_trade": stop_trade,
                    "error": err}
        logger.info("市价+STOP 已提交 %s %s x%d | STOP %.2f | orderId=%d/%d(%s/%s)",
                    symbol, action, qty, stop_px, parent.orderId, stop.orderId,
                    status, stop_status)
        return {"ok": True, "entry_order_id": parent.orderId,
                "stop_order_id": stop.orderId, "status": status,
                "stop_status": stop_status,
                "trade": entry_trade, "stop_trade": stop_trade}

    async def place_limit_with_stop(self, symbol: str, side: str, qty: int,
                                    limit_px: float, stop_px: float, order_ref: str = "") -> dict:
        """
        限价单（父，LMT DAY）+ 固定 STOP 止损（子，STP GTC，parentId 关联）。
        parentId 关联语义：父单未成交时子单挂起；父单被撤/平仓时子单随之
        自动取消（这就是"平仓时看到子订单自动取消"的机制）。
        返回 {ok, entry_order_id, stop_order_id, status, ...}。
        """
        action = "BUY" if side == "long" else "SELL"
        opp = "SELL" if side == "long" else "BUY"

        if self.enabled and not self._connected:
            return {"ok": False, "error": "IBKR 未连接，限价入场未提交"}
        if not self.enabled:
            fake_entry = next(self._fake_ids)
            fake_stop = next(self._fake_ids)
            logger.info("[DRY-RUN] 限价+STOP 子单 %s: %s x%d @LMT %.2f | STOP %s %.2f | FA=%s",
                        symbol, action, qty, limit_px, opp, stop_px, self.fa_group)
            return {"ok": True, "dry_run": True, "entry_order_id": fake_entry,
                    "stop_order_id": fake_stop, "status": "DryRun",
                    "limit_price": limit_px}

        from ib_async import LimitOrder, Order
        contract = await self._qualify(symbol)
        # 显式分配 orderId，子单 parentId 才能正确关联父单
        parent = LimitOrder(action, qty, round(limit_px, 2), tif="DAY",
                            orderId=self._next_tool_order_id(), transmit=False)
        parent.orderRef = order_ref + ":entry" if order_ref else ""
        self._apply_fa(parent)
        stop = Order()
        stop.orderType = "STP"
        stop.action = opp
        stop.totalQuantity = qty
        stop.tif = "GTC"
        stop.auxPrice = round(stop_px, 2)      # STOP 触发价 = ST 线值
        stop.orderId = self._next_tool_order_id()
        stop.parentId = parent.orderId
        stop.transmit = True
        stop.orderRef = order_ref + ":stop" if order_ref else ""
        self._apply_fa(stop)
        t0 = time.time()
        entry_trade = self.ib.placeOrder(contract, parent)
        stop_trade = self.ib.placeOrder(contract, stop)
        await asyncio.sleep(2.5)
        status = entry_trade.orderStatus.status
        stop_status = stop_trade.orderStatus.status
        fatal = self._recent_fatal_error(t0)
        bad = ("Cancelled", "Inactive", "ApiCancelled")
        if fatal or status in bad or stop_status in bad:
            err = (fatal["msg"] if fatal else f"入场 {status} / STOP {stop_status}")
            logger.error("限价+STOP 单失败 %s: %s", symbol, err)
            return {"ok": False, "entry_order_id": parent.orderId,
                    "stop_order_id": stop.orderId,
                    "status": status, "stop_status": stop_status,
                    "trade": entry_trade, "stop_trade": stop_trade,
                    "error": err}
        logger.info("限价+STOP 已提交 %s %s x%d @%.2f | STOP 触发 %.2f "
                    "| entryId=%d(%s) stopId=%d(%s)",
                    symbol, action, qty, limit_px, stop_px,
                    parent.orderId, status, stop.orderId, stop_status)
        return {"ok": True, "entry_order_id": parent.orderId,
                "stop_order_id": stop.orderId, "status": status,
                "limit_price": limit_px, "trade": entry_trade,
                "stop_trade": stop_trade}

    async def place_midpoint_oca(self, symbol: str, side: str, qty: int,
                                 limit_px: float, breakout_px: float,
                                 stop_px: float, oca_group: str, order_ref: str = "") -> dict:
        """中点 OCA 双腿入场：LMT 中点父单 + STP 突破入场腿，一成全撤。

        结构：LMT 父单（transmit=False，OCA 组）+ STP 止损子单（parentId
        关联 LMT，不入 OCA 组）+ STP 突破腿（transmit=True，OCA 组，
        ocaType=1 成交即撤其余）。中点腿成交则止损子单原生生效；突破腿
        成交时 OCA 撤销 LMT 腿并连带撤销其子止损，需由上层 watcher 补挂
        独立止损。返回 {ok, entry_order_id, stop_order_id,
        breakout_order_id, ...}。
        """
        action = "BUY" if side == "long" else "SELL"
        opp = "SELL" if side == "long" else "BUY"

        if self.enabled and not self._connected:
            return {"ok": False, "error": "IBKR 未连接，入场未提交"}
        if not self.enabled:
            fake_entry = next(self._fake_ids)
            fake_stop = next(self._fake_ids)
            fake_brk = next(self._fake_ids)
            logger.info("[DRY-RUN] 中点OCA %s: %s x%d @LMT %.2f | 突破STP %.2f "
                        "| STOP %s %.2f | OCA=%s",
                        symbol, action, qty, limit_px, breakout_px,
                        opp, stop_px, oca_group)
            return {"ok": True, "dry_run": True, "entry_order_id": fake_entry,
                    "stop_order_id": fake_stop, "breakout_order_id": fake_brk,
                    "status": "DryRun", "limit_price": limit_px}

        from ib_async import LimitOrder, Order
        contract = await self._qualify(symbol)
        parent = LimitOrder(action, qty, round(limit_px, 2), tif="DAY",
                            orderId=self._next_tool_order_id(), transmit=False)
        parent.ocaGroup = oca_group
        parent.ocaType = 1                      # 成交即撤销组内其余订单
        parent.orderRef = order_ref + ":entry" if order_ref else ""
        self._apply_fa(parent)
        stop = Order()
        stop.orderType = "STP"
        stop.action = opp
        stop.totalQuantity = qty
        stop.tif = "GTC"
        stop.auxPrice = round(stop_px, 2)      # STOP 触发价 = ST 线值
        stop.orderId = self._next_tool_order_id()
        stop.parentId = parent.orderId          # 止损只挂中点腿，不入 OCA 组
        stop.transmit = True
        stop.orderRef = order_ref + ":stop" if order_ref else ""
        self._apply_fa(stop)
        brk = Order()
        brk.orderType = "STP"                   # stop-market 突破入场
        brk.action = action
        brk.totalQuantity = qty
        brk.tif = "DAY"
        brk.auxPrice = round(breakout_px, 2)
        brk.orderId = self._next_tool_order_id()
        brk.ocaGroup = oca_group
        brk.ocaType = 1
        brk.transmit = True
        brk.orderRef = order_ref + ":breakout" if order_ref else ""
        self._apply_fa(brk)
        t0 = time.time()
        entry_trade = self.ib.placeOrder(contract, parent)
        stop_trade = self.ib.placeOrder(contract, stop)
        brk_trade = self.ib.placeOrder(contract, brk)
        await asyncio.sleep(2.5)
        status = entry_trade.orderStatus.status
        stop_status = stop_trade.orderStatus.status
        brk_status = brk_trade.orderStatus.status
        fatal = self._recent_fatal_error(t0)
        bad = ("Cancelled", "Inactive", "ApiCancelled")
        if fatal or status in bad or stop_status in bad or brk_status in bad:
            err = (fatal["msg"] if fatal else
                   f"入场 {status} / STOP {stop_status} / 突破 {brk_status}")
            logger.error("中点OCA 单失败 %s: %s", symbol, err)
            return {"ok": False, "entry_order_id": parent.orderId,
                    "stop_order_id": stop.orderId,
                    "breakout_order_id": brk.orderId,
                    "status": status, "stop_status": stop_status,
                    "breakout_status": brk_status,
                    "trade": entry_trade, "stop_trade": stop_trade,
                    "breakout_trade": brk_trade, "error": err}
        logger.info("中点OCA 已提交 %s %s x%d @LMT %.2f | 突破 %.2f | "
                    "STOP %.2f | entryId=%d(%s) stopId=%d(%s) brkId=%d(%s)",
                    symbol, action, qty, limit_px, breakout_px, stop_px,
                    parent.orderId, status, stop.orderId, stop_status,
                    brk.orderId, brk_status)
        return {"ok": True, "entry_order_id": parent.orderId,
                "stop_order_id": stop.orderId,
                "breakout_order_id": brk.orderId, "status": status,
                "limit_price": limit_px, "trade": entry_trade,
                "stop_trade": stop_trade, "breakout_trade": brk_trade}

    async def place_close(self, symbol: str, side: str, qty: int,
                          ref_px: float, order_ref: str = "") -> dict:
        """
        平仓：反向 marketable LIMIT（DAY）。
        side 是持仓方向：long 持仓 → SELL 平仓（限价略低于市价），
        short 持仓 → BUY 平仓（限价略高于市价），确保尽快成交。
        返回 {ok, order_id, status, limit_price, trade?}。
        """
        premium = config.ENTRY_LIMIT_PREMIUM_PCT / 100.0
        action = "SELL" if side == "long" else "BUY"      # 反向
        sign = -1 if side == "long" else 1                # 平多卖略低 / 平空买略高
        lmt = round(ref_px * (1 + premium * sign), 2)

        if self.enabled and not self._connected:
            return {"ok": False, "error": "IBKR 未连接，平仓未提交"}
        if not self.enabled:
            fake = next(self._fake_ids)
            logger.info("[DRY-RUN] 平仓 %s: %s x%d @LMT %.2f",
                        symbol, action, qty, lmt)
            return {"ok": True, "dry_run": True, "order_id": fake,
                    "status": "DryRun", "limit_price": lmt}

        from ib_async import LimitOrder
        contract = await self._qualify(symbol)
        order = LimitOrder(action, qty, lmt, tif="DAY",
                           orderId=self._next_tool_order_id())
        if order_ref:
            order.orderRef = order_ref
        self._apply_fa(order)
        t0 = time.time()
        trade = self.ib.placeOrder(contract, order)
        await asyncio.sleep(2.5)
        status = trade.orderStatus.status
        fatal = self._recent_fatal_error(t0, {order.orderId})
        bad = ("Cancelled", "Inactive", "ApiCancelled")
        if fatal or status in bad:
            err = fatal["msg"] if fatal else f"平仓 {status}"
            logger.error("平仓失败 %s orderId=%s status=%s fatalReqId=%s: %s",
                         symbol, order.orderId, status, fatal["reqId"] if fatal else None, err)
            return {"ok": False, "order_id": order.orderId,
                    "status": status, "error": err, "trade": trade,
                    "permId": getattr(trade.order, "permId", None)}
        logger.info("平仓已提交 %s %s x%d @%.2f | orderId=%d(%s)",
                    symbol, action, qty, lmt, order.orderId, status)
        return {"ok": True, "order_id": order.orderId, "status": status,
                "limit_price": lmt, "trade": trade}

    async def place_stop_only(self, symbol: str, side: str, qty: int,
                              stop_px: float) -> dict:
        """提交独立 STP GTC 保护单，供平仓失败后的保护单恢复使用。

        side 是持仓方向。返回 {ok, order_id, stop_order_id, status, trade?}；
        两种订单号字段相同，分别便于通用下单和保护单调用方使用。
        """
        action = "SELL" if side == "long" else "BUY"
        if not self.enabled:
            fake = next(self._fake_ids)
            logger.info("[DRY-RUN] 独立 STOP 保护单 %s: %s x%d @%.2f",
                        symbol, action, qty, stop_px)
            return {"ok": True, "dry_run": True, "order_id": fake,
                    "stop_order_id": fake, "status": "DryRun",
                    "stop_price": stop_px}
        if not self._connected:
            return {"ok": False, "error": "IBKR 未连接，无法提交保护单"}

        from ib_async import Order
        contract = await self._qualify(symbol)
        order = Order()
        order.orderType = "STP"
        order.action = action
        order.totalQuantity = qty
        order.tif = "GTC"
        order.auxPrice = round(stop_px, 2)
        order.orderId = self._next_tool_order_id()
        order.transmit = True
        self._apply_fa(order)
        t0 = time.time()
        trade = self.ib.placeOrder(contract, order)
        await asyncio.sleep(2.5)
        status = trade.orderStatus.status
        fatal = self._recent_fatal_error(t0, {order.orderId})
        bad = ("Cancelled", "Inactive", "ApiCancelled")
        if fatal or status in bad:
            err = fatal["msg"] if fatal else f"独立 STOP {status}"
            logger.error("独立 STOP 保护单失败 %s: %s", symbol, err)
            return {"ok": False, "order_id": order.orderId,
                    "stop_order_id": order.orderId, "status": status,
                    "error": err, "trade": trade}
        logger.info("独立 STOP 保护单已提交 %s %s x%d @%.2f | orderId=%d(%s)",
                    symbol, action, qty, stop_px, order.orderId, status)
        return {"ok": True, "order_id": order.orderId,
                "stop_order_id": order.orderId, "status": status,
                "stop_price": stop_px, "trade": trade}

    async def modify_stop(self, unit, new_stop_px: float) -> bool:
        """修改保护止损价；IBKR 确认接受后才更新 unit.stop_px。"""
        new_stop = round(new_stop_px, 2)
        if not self.enabled:
            logger.info("[DRY-RUN] 止损触发价修改 %s: -> %.2f",
                        unit.symbol, new_stop)
            unit.stop_px = new_stop
            return True
        if not self._connected:
            logger.warning("%s IBKR 断连，拒绝修改止损", unit.symbol)
            return False
        trade = getattr(unit, "trail_trade", None)
        if trade is None:
            logger.warning("%s 无保护单句柄，无法修改", unit.symbol)
            return False
        if trade.isDone():
            logger.warning("%s 保护单已为终态，拒绝修改", unit.symbol)
            return False
        old_stop = (trade.order.auxPrice if trade.order.orderType == "STP"
                    else trade.order.trailStopPrice)
        t0 = time.time()
        try:
            self._set_stop_price(trade.order, new_stop)
            self.ib.placeOrder(trade.contract, trade.order)  # 同 orderId 即修改
        except Exception as e:
            logger.warning("%s 止损修改异常: %s", unit.symbol, e)
            self._set_stop_price(trade.order, old_stop)
            try:
                if not trade.isDone():
                    self.ib.placeOrder(trade.contract, trade.order)
            except Exception as rollback_error:
                logger.error("%s 止损修改回滚失败，需立即人工核查: %s",
                             unit.symbol, rollback_error)
            return False
        await asyncio.sleep(2.5)
        error = self._modification_error([trade], t0)
        if error:
            logger.warning("%s 止损修改未获 IBKR 接受: %s", unit.symbol, error)
            fatal = self._recent_fatal_error(t0)
            if fatal and fatal.get("code") == 320 \
                    and hasattr(unit, "trail_modify_blocked_until"):
                unit.trail_modify_blocked_until = time.time() + 300
                logger.warning("%s IBKR error 320，止损修改冷却 5 分钟，避免连续打崩连接",
                               unit.symbol)
            self._set_stop_price(trade.order, old_stop)
            try:
                if not trade.isDone():
                    self.ib.placeOrder(trade.contract, trade.order)
            except Exception as rollback_error:
                logger.error("%s 止损修改回滚失败，需立即人工核查: %s",
                             unit.symbol, rollback_error)
            return False
        unit.stop_px = new_stop
        logger.info("止损触发价已修改 %s (%s) -> %.2f (orderId=%d)",
                    unit.symbol, trade.order.orderType, new_stop,
                    trade.order.orderId)
        return True

    async def modify_stop_qty(self, trade, new_qty: int) -> bool:
        """修改 STP 保护数量；trade 也可为带 trail_trade 的 unit/lot。

        供入场部分成交后收缩保护数量。IBKR 确认接受才返回 True。
        """
        if new_qty <= 0:
            logger.warning("STP 修改数量必须为正数: %s", new_qty)
            return False
        trade = getattr(trade, "trail_trade", trade)
        if not self.enabled:
            if trade is not None:
                trade.order.totalQuantity = new_qty
            logger.info("[DRY-RUN] STOP 数量修改 -> %s", new_qty)
            return True
        if not self._connected:
            logger.warning("IBKR 断连，拒绝修改 STOP 数量")
            return False
        if trade is None or trade.order.orderType != "STP":
            logger.warning("缺少 STP 单句柄，无法修改保护数量")
            return False
        if trade.isDone():
            logger.warning("STP 单已为终态，拒绝修改数量 (orderId=%s)",
                           trade.order.orderId)
            return False
        old_qty = trade.order.totalQuantity
        t0 = time.time()
        try:
            trade.order.totalQuantity = new_qty
            self.ib.placeOrder(trade.contract, trade.order)
        except Exception as e:
            logger.warning("STOP 数量修改异常: %s", e)
            trade.order.totalQuantity = old_qty
            try:
                if not trade.isDone():
                    self.ib.placeOrder(trade.contract, trade.order)
            except Exception as rollback_error:
                logger.error("STOP 数量回滚失败，需立即人工核查: %s",
                             rollback_error)
            return False
        await asyncio.sleep(2.5)
        error = self._modification_error([trade], t0)
        if error:
            logger.warning("STOP 数量修改未获 IBKR 接受: %s", error)
            trade.order.totalQuantity = old_qty
            try:
                if not trade.isDone():
                    self.ib.placeOrder(trade.contract, trade.order)
            except Exception as rollback_error:
                logger.error("STOP 数量回滚失败，需立即人工核查: %s",
                             rollback_error)
            return False
        logger.info("STOP 数量已修改 -> %s (orderId=%d)",
                    new_qty, trade.order.orderId)
        return True

    async def modify_limit_stop(self, unit, new_limit_px: float,
                                new_stop_px: float) -> bool:
        """PENDING 父子单改价；两腿均获 IBKR 接受后才更新 unit。"""
        new_limit = round(new_limit_px, 2)
        new_stop = round(new_stop_px, 2)
        if not self.enabled:
            logger.info("[DRY-RUN] 中点父子单改价 %s: LMT %.2f / STP %.2f",
                        unit.symbol, new_limit, new_stop)
            unit.entry_px = new_limit
            unit.stop_px = new_stop
            return True
        if not self._connected:
            logger.warning("%s IBKR 断连，拒绝中点改价", unit.symbol)
            return False
        entry = getattr(unit, "entry_trade", None)
        stop = getattr(unit, "trail_trade", None)
        if entry is None or stop is None:
            logger.warning("%s 缺少父子单句柄，无法中点改价", unit.symbol)
            return False
        if entry.isDone() or stop.isDone():
            logger.warning("%s 父子单已有终态，拒绝中点改价", unit.symbol)
            return False
        old_limit = entry.order.lmtPrice
        old_stop = (stop.order.auxPrice if stop.order.orderType == "STP"
                    else stop.order.trailStopPrice)

        def rollback() -> None:
            self._set_stop_price(stop.order, old_stop)
            entry.order.lmtPrice = old_limit
            errors = []
            for trade in (stop, entry):
                try:
                    if not trade.isDone():
                        self.ib.placeOrder(trade.contract, trade.order)
                except Exception as e:
                    errors.append(str(e))
            if errors:
                raise RuntimeError("; ".join(errors))

        t0 = time.time()
        try:
            self._set_stop_price(stop.order, new_stop)
            self.ib.placeOrder(stop.contract, stop.order)
            entry.order.lmtPrice = new_limit
            self.ib.placeOrder(entry.contract, entry.order)
        except Exception as e:
            logger.warning("%s 中点父子单改价异常: %s", unit.symbol, e)
            try:
                rollback()
            except Exception as rollback_error:
                logger.error("%s 中点改价回滚失败，需立即人工核查: %s",
                             unit.symbol, rollback_error)
            return False
        await asyncio.sleep(2.5)
        error = self._modification_error([stop, entry], t0)
        if error:
            logger.warning("%s 中点改价未获 IBKR 接受: %s", unit.symbol, error)
            try:
                rollback()
            except Exception as rollback_error:
                logger.error("%s 中点改价回滚失败，需立即人工核查: %s",
                             unit.symbol, rollback_error)
            return False
        unit.entry_px = new_limit
        unit.stop_px = new_stop
        logger.info("中点父子单已改价 %s: LMT %.2f / STP %.2f",
                    unit.symbol, new_limit, new_stop)
        return True

    async def modify_limit_stop_qty(self, unit, new_limit_px: float,
                                    new_stop_px: float, new_qty: int) -> bool:
        """PENDING 父子单改价 + 改股数：LMT 父单与 STP 子单的 totalQuantity
        随价格同步修改（同 orderId modify 语义），供回踩单逐 bar 以损定量。

        两腿均获 IBKR 接受后才更新 unit；任一失败回滚全部（含原股数）。
        股数不变时应走 modify_limit_stop 纯价格路径。
        """
        new_limit = round(new_limit_px, 2)
        new_stop = round(new_stop_px, 2)
        new_qty = int(new_qty)
        if new_qty <= 0:
            logger.warning("%s 回踩改价股数必须为正: %s", unit.symbol, new_qty)
            return False
        if not self.enabled:
            logger.info("[DRY-RUN] 回踩父子单改价改量 %s: LMT %.2f / STP %.2f x%d",
                        unit.symbol, new_limit, new_stop, new_qty)
            unit.entry_px = new_limit
            unit.stop_px = new_stop
            return True
        if not self._connected:
            logger.warning("%s IBKR 断连，拒绝回踩改价改量", unit.symbol)
            return False
        entry = getattr(unit, "entry_trade", None)
        stop = getattr(unit, "trail_trade", None)
        if entry is None or stop is None:
            logger.warning("%s 缺少父子单句柄，无法回踩改价改量", unit.symbol)
            return False
        if entry.isDone() or stop.isDone():
            logger.warning("%s 父子单已有终态，拒绝回踩改价改量", unit.symbol)
            return False
        old_limit = entry.order.lmtPrice
        old_stop = (stop.order.auxPrice if stop.order.orderType == "STP"
                    else stop.order.trailStopPrice)
        old_entry_qty = entry.order.totalQuantity
        old_stop_qty = stop.order.totalQuantity

        def rollback() -> None:
            self._set_stop_price(stop.order, old_stop)
            stop.order.totalQuantity = old_stop_qty
            entry.order.lmtPrice = old_limit
            entry.order.totalQuantity = old_entry_qty
            errors = []
            for trade in (stop, entry):
                try:
                    if not trade.isDone():
                        self.ib.placeOrder(trade.contract, trade.order)
                except Exception as e:
                    errors.append(str(e))
            if errors:
                raise RuntimeError("; ".join(errors))

        t0 = time.time()
        try:
            self._set_stop_price(stop.order, new_stop)
            stop.order.totalQuantity = new_qty
            self.ib.placeOrder(stop.contract, stop.order)
            entry.order.lmtPrice = new_limit
            entry.order.totalQuantity = new_qty
            self.ib.placeOrder(entry.contract, entry.order)
        except Exception as e:
            logger.warning("%s 回踩父子单改价改量异常: %s", unit.symbol, e)
            try:
                rollback()
            except Exception as rollback_error:
                logger.error("%s 回踩改价改量回滚失败，需立即人工核查: %s",
                             unit.symbol, rollback_error)
            return False
        await asyncio.sleep(2.5)
        error = self._modification_error([stop, entry], t0)
        if error:
            logger.warning("%s 回踩改价改量未获 IBKR 接受: %s", unit.symbol, error)
            try:
                rollback()
            except Exception as rollback_error:
                logger.error("%s 回踩改价改量回滚失败，需立即人工核查: %s",
                             unit.symbol, rollback_error)
            return False
        unit.entry_px = new_limit
        unit.stop_px = new_stop
        logger.info("回踩父子单已改价改量 %s: LMT %.2f / STP %.2f x%d",
                    unit.symbol, new_limit, new_stop, new_qty)
        return True

    async def modify_midpoint_oca(self, unit, new_limit_px: float,
                                  new_breakout_px: float,
                                  new_stop_px: float) -> bool:
        """中点 OCA 三腿改价：LMT 中点 + STP 突破触发 + STP 止损子单。

        三腿均获 IBKR 接受后才更新 unit；任一失败回滚全部。
        """
        new_limit = round(new_limit_px, 2)
        new_brk = round(new_breakout_px, 2)
        new_stop = round(new_stop_px, 2)
        if not self.enabled:
            logger.info("[DRY-RUN] 中点OCA改价 %s: LMT %.2f / 突破 %.2f / STP %.2f",
                        unit.symbol, new_limit, new_brk, new_stop)
            unit.entry_px = new_limit
            unit.stop_px = new_stop
            return True
        if not self._connected:
            logger.warning("%s IBKR 断连，拒绝中点OCA改价", unit.symbol)
            return False
        entry = getattr(unit, "entry_trade", None)
        stop = getattr(unit, "trail_trade", None)
        brk = getattr(unit, "breakout_trade", None)
        if entry is None or stop is None or brk is None:
            logger.warning("%s 缺少OCA三腿句柄，无法中点改价", unit.symbol)
            return False
        if entry.isDone() or stop.isDone() or brk.isDone():
            logger.warning("%s OCA三腿已有终态，拒绝中点改价", unit.symbol)
            return False
        old_limit = entry.order.lmtPrice
        old_brk = brk.order.auxPrice
        old_stop = (stop.order.auxPrice if stop.order.orderType == "STP"
                    else stop.order.trailStopPrice)

        def rollback() -> None:
            self._set_stop_price(stop.order, old_stop)
            entry.order.lmtPrice = old_limit
            brk.order.auxPrice = old_brk
            errors = []
            for trade in (stop, brk, entry):
                try:
                    if not trade.isDone():
                        self.ib.placeOrder(trade.contract, trade.order)
                except Exception as e:
                    errors.append(str(e))
            if errors:
                raise RuntimeError("; ".join(errors))

        t0 = time.time()
        try:
            self._set_stop_price(stop.order, new_stop)
            self.ib.placeOrder(stop.contract, stop.order)
            brk.order.auxPrice = new_brk
            self.ib.placeOrder(brk.contract, brk.order)
            entry.order.lmtPrice = new_limit
            self.ib.placeOrder(entry.contract, entry.order)
        except Exception as e:
            logger.warning("%s 中点OCA改价异常: %s", unit.symbol, e)
            try:
                rollback()
            except Exception as rollback_error:
                logger.error("%s 中点OCA改价回滚失败，需立即人工核查: %s",
                             unit.symbol, rollback_error)
            return False
        await asyncio.sleep(2.5)
        error = self._modification_error([stop, brk, entry], t0)
        if error:
            logger.warning("%s 中点OCA改价未获 IBKR 接受: %s", unit.symbol, error)
            try:
                rollback()
            except Exception as rollback_error:
                logger.error("%s 中点OCA改价回滚失败，需立即人工核查: %s",
                             unit.symbol, rollback_error)
            return False
        unit.entry_px = new_limit
        unit.stop_px = new_stop
        logger.info("中点OCA已改价 %s: LMT %.2f / 突破 %.2f / STP %.2f",
                    unit.symbol, new_limit, new_brk, new_stop)
        return True

    async def convert_entry_to_market(self, unit) -> bool:
        """PENDING 入场父单由 LMT 原地改 MKT（同 orderId 修改，一次性升级）。

        STP 子单经 parentId 关联，无需变动。市价单通常立即成交，
        成交即成功；未成交且 IBKR 未接受修改时回滚为原 LMT 挂单。
        """
        if not self.enabled:
            logger.info("[DRY-RUN] 入场单升级市价 %s (orderId=%s)",
                        unit.symbol, getattr(unit, "entry_order_id", None))
            return True
        if not self._connected:
            logger.warning("%s IBKR 断连，拒绝市价升级", unit.symbol)
            return False
        entry = getattr(unit, "entry_trade", None)
        if entry is None:
            logger.warning("%s 无入场单句柄，无法市价升级", unit.symbol)
            return False
        if entry.isDone():
            logger.warning("%s 入场单已为终态，拒绝市价升级", unit.symbol)
            return False
        old_type = entry.order.orderType
        old_limit = entry.order.lmtPrice
        t0 = time.time()
        try:
            entry.order.orderType = "MKT"
            entry.order.lmtPrice = 0.0
            self.ib.placeOrder(entry.contract, entry.order)
        except Exception as e:
            logger.warning("%s 市价升级异常: %s", unit.symbol, e)
            entry.order.orderType = old_type
            entry.order.lmtPrice = old_limit
            try:
                if not entry.isDone():
                    self.ib.placeOrder(entry.contract, entry.order)
            except Exception as rollback_error:
                logger.error("%s 市价升级回滚失败，需立即人工核查: %s",
                             unit.symbol, rollback_error)
            return False
        await asyncio.sleep(2.5)
        if entry.isDone():
            filled = float(entry.orderStatus.filled or 0)
            if filled > 0:
                logger.info("入场单已升级市价并成交 %s x%.0f (orderId=%d)",
                            unit.symbol, filled, entry.order.orderId)
                return True
            logger.warning("%s 市价升级后入场单终态无成交 (status=%s)",
                           unit.symbol, entry.orderStatus.status)
            return False
        error = self._modification_error([entry], t0)
        if error:
            logger.warning("%s 市价升级未获 IBKR 接受: %s", unit.symbol, error)
            entry.order.orderType = old_type
            entry.order.lmtPrice = old_limit
            try:
                if not entry.isDone():
                    self.ib.placeOrder(entry.contract, entry.order)
            except Exception as rollback_error:
                logger.error("%s 市价升级回滚失败，需立即人工核查: %s",
                             unit.symbol, rollback_error)
            return False
        logger.info("入场单已升级市价 %s (%s -> MKT, orderId=%d)",
                    unit.symbol, old_type, entry.order.orderId)
        return True

    async def last_price(self, symbol: str, timeout: float = 5.0) -> Optional[float]:
        """snapshot 最新价（用于 PnL 计算）。未连接/取不到返回 None。"""
        if not self._connected:
            return None
        try:
            from ib_async import Stock
            ticker = symbol.split(":")[-1]
            contract = Stock(ticker, "SMART", "USD")
            await self.ib.qualifyContractsAsync(contract)
            t = self.ib.reqMktData(contract, "", True, False)  # snapshot=True
            deadline = time.time() + timeout
            while time.time() < deadline:
                if t.last and t.last > 0:
                    break
                if t.close and t.close > 0:
                    break
                await asyncio.sleep(0.1)
            price = t.last or t.close
            self.ib.cancelMktData(contract)
            return float(price) if price and price > 0 else None
        except Exception as e:
            logger.warning("snapshot 最新价失败 %s: %s", symbol, e)
            return None

    def cancel_order(self, trade) -> bool:
        """撤单个订单，实盘断线时返回失败。"""
        if not self.enabled:
            return True
        if not self._connected:
            return False
        try:
            if trade is not None and not trade.isDone():
                self.ib.cancelOrder(trade.order)
            return True
        except Exception as e:
            logger.warning("撤单失败: %s", e)
            return False

    # ------------------------------------------------------------------
    # 只读查询
    # ------------------------------------------------------------------

    async def trade_map(self) -> Optional[Dict[int, object]]:
        """当前可见订单表（orderId -> Trade），供按订单号恢复句柄。

        None 表示断连或同步失败、订单状态未知；{} 才表示查询成功且没有
        可见订单。调用方不得把 None 当作空表继续对账。
        """
        if not self._connected:
            return None
        out: Dict[int, object] = {}
        try:
            for t in self.ib.trades():
                out[t.order.orderId] = t
            trades = await asyncio.wait_for(
                self.ib.reqAllOpenOrdersAsync(), timeout=10)
            for t in trades:
                out.setdefault(t.order.orderId, t)
        except Exception as e:
            logger.warning("开放订单同步失败: %s", e)
            return None
        return out

    async def closing_fill_avg(self, symbol: str, side: str,
                               since_ts: float = 0.0,
                               legacy_order_ids=None, *, exact_order_ids=None) -> Optional[tuple]:
        """统计本工具平仓成交的 (加权均价, 总股数)，无匹配则 None。

        side 是持仓方向（long 的平仓是 SLD）。只统计 88 号段订单；
        legacy_order_ids 可显式传入旧平仓单 orderId 白名单。
        exact_order_ids 指定时严格限制这些订单号，其他88号段批次也排除；
        不指定时保留历史聚合接口。订单管理器必须使用精确归属查询。
        """
        if not self._connected:
            return None
        ticker = symbol.split(":")[-1].upper()
        want = "SLD" if side == "long" else "BOT"
        legacy_ids = set()
        for order_id in legacy_order_ids or ():
            try:
                legacy_ids.add(int(order_id))
            except (TypeError, ValueError):
                continue
        try:
            fills = await asyncio.wait_for(
                self.ib.reqExecutionsAsync(ExecutionFilter()), timeout=10)
        except Exception as e:
            logger.warning("成交回报查询失败: %s", e)
            return None
        exact_ids = set(exact_order_ids) if exact_order_ids is not None else None
        total_shares = 0.0
        total_amt = 0.0
        for f in fills:
            try:
                if f.contract.symbol.upper() != ticker:
                    continue
                ex = f.execution
                order_id = int(ex.orderId)
                if exact_ids is not None:
                    if order_id not in exact_ids:
                        continue
                elif not self.is_tool_order_id(order_id) and order_id not in legacy_ids:
                    continue
                if ex.side != want:
                    continue
                if since_ts and getattr(ex, "time", None) is not None:
                    try:
                        if ex.time.timestamp() < since_ts:
                            continue
                    except Exception:
                        pass
                total_shares += float(ex.shares)
                total_amt += float(ex.price) * float(ex.shares)
            except Exception:
                continue
        if total_shares <= 0:
            return None
        return round(total_amt / total_shares, 4), int(total_shares)

    async def submission_order_ids(self, symbol: str, references: list) -> Optional[dict]:
        """Recover exact persisted order references from today's executions, never by price."""
        if not self._connected:
            return None
        try:
            fills = await asyncio.wait_for(self.ib.reqExecutionsAsync(ExecutionFilter()), timeout=10)
        except Exception:
            logger.exception("受理意图成交引用查询失败")
            return None
        ticker = symbol.split(":")[-1].upper()
        result = {reference: set() for reference in references}
        for fill in fills:
            execution = fill.execution
            reference = getattr(execution, "orderRef", "")
            if fill.contract.symbol.upper() == ticker and reference in result:
                result[reference].add(int(execution.orderId))
        return result

    async def entry_fills_by_order(self, symbol: str,
                                   order_ids) -> Optional[Dict[int, tuple]]:
        """按订单号聚合本日成交（成交真相）：{orderId: (成交股数, 加权均价)}。

        OCA 双腿场景：突破腿成交与 LMT 腿被撤可在同一帧发生，缓存的
        orderStatus.filled 可能漏报；executions 是唯一可靠来源。
        None = 查询失败（调用方不得把 None 当作无成交）。
        """
        if not self._connected:
            return None
        ticker = symbol.split(":")[-1].upper()
        want_ids = set()
        for oid in order_ids or ():
            try:
                want_ids.add(int(oid))
            except (TypeError, ValueError):
                continue
        if not want_ids:
            return {}
        try:
            fills = await asyncio.wait_for(
                self.ib.reqExecutionsAsync(ExecutionFilter()), timeout=10)
        except Exception as e:
            logger.warning("成交回报查询失败: %s", e)
            return None
        out: Dict[int, list] = {}
        for f in fills:
            try:
                if f.contract.symbol.upper() != ticker:
                    continue
                ex = f.execution
                oid = int(ex.orderId)
                if oid not in want_ids:
                    continue
                shares, amt = out.get(oid, [0.0, 0.0])
                shares += float(ex.shares)
                amt += float(ex.price) * float(ex.shares)
                out[oid] = [shares, amt]
            except Exception:
                continue
        return {oid: (int(shares), round(amt / shares, 4))
                for oid, (shares, amt) in out.items() if shares > 0}

    async def fa_accounts(self, *, confirmed_only=False) -> List[str]:
        """
        FA 账户组（self.fa_group）的成员账户列表。运行时从 TWS 的
        FA Groups 配置动态查询（requestFA, type=1）；查询失败回退
        config.IBKR_FA_ACCOUNTS。资金/盈亏/持仓/对账均只针对这组。
        """
        if not self._connected:
            return [] if confirmed_only else list(config.IBKR_FA_ACCOUNTS)
        try:
            xml = await asyncio.wait_for(self.ib.requestFAAsync(1), timeout=8)
            root = ET.fromstring(xml)
            for grp in root.iter("Group"):
                if (grp.findtext("name") or "").strip() == self.fa_group:
                    accts = [a.text.strip() for a in grp.iter("acct")
                             if a.text and a.text.strip()]
                    if accts:
                        # 仅在成员变化时打日志，避免高频调用刷屏
                        if accts != getattr(self, "_fa_accounts_cache", None):
                            self._fa_accounts_cache = accts
                            logger.info("FA 组 %s 成员账户: %s",
                                        self.fa_group, accts)
                        return accts
        except Exception as e:
            logger.warning("动态查询 FA 组 %s 失败: %s",
                           self.fa_group, e)
        return [] if confirmed_only else list(config.IBKR_FA_ACCOUNTS)

    async def external_adoption_snapshot(self, tickers):
        """Read positions by account plus broker executions, without binding orders."""
        if not self._connected:
            return None
        ib = self.ib
        try:
            accounts = set(await self.fa_accounts(confirmed_only=True))
            if not accounts or not self._connected or self.ib is not ib:
                return None
            if not tickers:
                return [], [], [], accounts
            fills = await asyncio.wait_for(ib.reqExecutionsAsync(ExecutionFilter()), timeout=10)
            active_orders = await asyncio.wait_for(ib.reqAllOpenOrdersAsync(), timeout=10)
            positions = [p for p in ib.positions() if p.account in accounts
                         and p.contract.secType == "STK" and p.position
                         and p.contract.symbol.upper() in tickers]
            if self._connected and self.ib is ib:
                return positions, fills, active_orders, accounts
        except Exception:
            logger.exception("IBKR 外部仓位接管快照不可用")
        return None

    async def positions(self) -> Optional[Dict[str, float]]:
        """FA 组成员账户的股票持仓按 symbol 聚合。

        None 表示断连或查询失败、仓位未知；{} 才表示查询成功且确认空仓。
        调用方必须先判断 None，不得用 .get(..., 0) 把未知当作零仓。
        """
        if not self._connected:
            return None
        try:
            accts = set(await self.fa_accounts())
            agg: Dict[str, float] = {}
            for p in self.ib.positions():
                # 只聚合股票：组里还有期权（如 SNDK 261002C01900000），
                # 按 contract.symbol 混聚会把股/期头寸错误相加
                if p.contract.secType != "STK":
                    continue
                if p.account not in accts:
                    continue
                ticker = p.contract.symbol
                agg[ticker] = agg.get(ticker, 0.0) + float(p.position)
            return {k: v for k, v in agg.items() if v != 0}
        except Exception as e:
            logger.warning("持仓查询失败: %s", e)
            return None

    async def open_protective_qty(self) -> Optional[Dict[str, Dict[str, float]]]:
        """
        FA 组开放的保护性止损单（STP/TRAIL）按 ticker 聚合。
        返回 {ticker: {"sell": 股数, "buy": 股数}}；未连接/查询失败返回 None。
        供止损守卫核对「持仓是否有足够的止损单覆盖」。
        """
        if not self._connected:
            return None
        accts = set(await self.fa_accounts())
        try:
            trades = await asyncio.wait_for(
                self.ib.reqAllOpenOrdersAsync(), timeout=10)
        except Exception as e:
            logger.warning("开放订单查询失败: %s", e)
            return None
        out: Dict[str, Dict[str, float]] = {}
        for trade in trades:
            order = trade.order
            if order.orderType not in ("STP", "TRAIL"):
                continue
            # FA 组口径：成员账户订单 或 挂在组名下的订单
            acct = order.account or ""
            if (accts and acct not in accts and acct != self.fa_group
                    and getattr(order, "faGroup", "") != self.fa_group):
                continue
            ticker = trade.contract.symbol
            slot = out.setdefault(ticker, {"sell": 0.0, "buy": 0.0})
            key = "sell" if order.action == "SELL" else "buy"
            slot[key] += float(order.totalQuantity or 0)
        return out

    async def net_liquidation(self) -> Optional[float]:
        """FA 组（dt_test）成员账户 NetLiquidation 汇总（USD/BASE）。"""
        if not self._connected:
            return None
        try:
            accts = set(await self.fa_accounts())
            summary = await asyncio.wait_for(
                self.ib.accountSummaryAsync(""), timeout=10)
            total = 0.0
            seen = False
            for item in summary:
                if item.account not in accts:
                    continue
                if item.tag == "NetLiquidation" \
                        and item.currency in ("USD", "BASE"):
                    total += float(item.value)
                    seen = True
            return total if seen else None
        except Exception as e:
            logger.warning("净值查询失败: %s", e)
            return None

    async def prev_day_equity(self) -> Optional[float]:
        """FA 组（dt_test）成员账户 PreviousDayEquityWithLoanValue 汇总（前一日权益）。"""
        if not self._connected:
            return None
        try:
            accts = set(await self.fa_accounts())
            summary = await asyncio.wait_for(
                self.ib.accountSummaryAsync(""), timeout=10)
            total = 0.0
            seen = False
            for item in summary:
                if item.account not in accts:
                    continue
                if item.tag == "PreviousDayEquityWithLoanValue" \
                        and item.currency in ("USD", "BASE"):
                    total += float(item.value)
                    seen = True
            return total if seen else None
        except Exception as e:
            logger.warning("前一日权益查询失败: %s", e)
            return None

    @staticmethod
    def _daily_pnl_session_active(now: datetime) -> bool:
        """常规交易日 09:30 ET 后才展示当日盈亏，盘后保留当日结果。"""
        return now.weekday() < 5 and now.hour * 60 + now.minute >= 9 * 60 + 30

    async def daily_pnl(self) -> Optional[float]:
        """
        常规时段当日盈亏 = 当前净值 - 前一日权益（PreviousDayEquityWithLoanValue）。
        周末和 09:30 ET 前返回 0，避免把上一交易日差值显示成今天盈利；
        盘后保留当日结果。此口径含日内浮动，不作日亏损熔断基准。
        常规时段取不到净值返回 None。
        """
        if not self._daily_pnl_session_active(datetime.now(ZoneInfo("America/New_York"))):
            return 0.0
        cur = await self.net_liquidation()
        prev = await self.prev_day_equity()
        if cur is None or prev is None:
            logger.warning("日盈亏未知（净值/前一日权益取不到），日亏损熔断本次跳过")
            return None
        return round(cur - prev, 2)

    async def realized_pnl(self) -> Optional[float]:
        """
        今日已实现盈亏（RealizedPnL，All 汇总，USD 口径）。
        这是日亏损熔断的正确基准：只算今天真正平仓的盈亏，不含隔夜持仓浮动。
        """
        if not self._connected:
            return None
        try:
            summary = await asyncio.wait_for(
                self.ib.accountSummaryAsync(""), timeout=10)
            for item in summary:
                if (item.tag == "RealizedPnL"
                        and item.currency == "USD"
                        and item.account == "All"):
                    return float(item.value)
            return None
        except Exception as e:
            logger.warning("已实现盈亏查询失败: %s", e)
            return None
