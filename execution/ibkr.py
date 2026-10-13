# -*- coding: utf-8 -*-
"""
IBKR 网关适配层 (IBKRGateway)
=============================

基于 ib_async（ib_insync 的官方后继）封装 TWS 连接与下单。
实战经验继承自同级项目 nautilus_ibkr_helloworld（详见 PROJECT_NOTES 8.1）：

- TWS 实盘端口 7496；client_id 与 nautilus 项目错开（默认 11）；
- FA 账户组：order.faGroup = 组名，faMethod 留空（带值被拒 10226/321）；
- 入场单用 marketable LIMIT（限价=参考价±0.5%，tif=DAY），
  不用 bare MARKET（延迟行情被拒 error 10349），也不用 GTC
  （未成交单不隔夜，引擎宕机 IBKR 收盘自动作废，双保险）；
- 保护止损 STOP_MARKET GTC + 止盈 LIMIT GTC，OCA 组联动（ocaType=1）；
- IBKR 的 order error 不一定事件化 → submit 后 2.5s 主动核对订单状态；
- dry-run 门控：enabled=False 时所有下单动作只记日志，返回伪造句柄，
  上层（PositionManager）流程不变，便于全链路无风险演练。
"""

import asyncio
import itertools
import json
import logging
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Dict, List, Optional

import config

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# IBKR order error 中需要立刻视为下单失败的错误码
_FATAL_ERROR_CODES = {
    10226,  # FA allocation method 无效
    10349,  # 延迟行情下拒绝 bare MARKET
    201,    # Order rejected
    202,    # Order cancelled
}


class IBKRGateway:
    """
    用法::

        gw = IBKRGateway.from_config()
        await gw.connect()          # dry-run 模式下也建议连接（只读），失败不致命
        res = await gw.place_entry(unit)      # dict，含 ok/status/order_id/trade
    """

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
        self.ib = None          # ib_async.IB，connect 后可用
        self._connected = False
        self.ibkr_time = 0.0                # IBKR 服务器时间戳（心跳探测时更新）
        self._heartbeat_task = None          # 心跳监控任务
        self.auto_reconnect = True           # 断线后自动重连
        self._reconnect_task = None          # 重连任务
        self.disconnect_callback = None      # 断线回调（上层订阅）
        self._errors: List[dict] = []
        self._fake_ids = itertools.count(900000)   # dry-run 伪造订单号

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
        try:
            await self.ib.connectAsync(
                self.host, self.port, clientId=self.client_id,
                timeout=timeout, account=self.account_id)
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

    def _recent_fatal_error(self, since: float) -> Optional[dict]:
        for e in reversed(self._errors):
            if e["ts"] >= since and e["code"] in _FATAL_ERROR_CODES:
                return e
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

        if not (self.enabled and self._connected):
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
                            orderId=self.ib.client.getReqId(), transmit=False)
        self._apply_fa(parent)
        trail = Order()
        trail.orderType = "TRAIL"
        trail.action = opp
        trail.totalQuantity = unit.qty
        trail.tif = "GTC"
        trail.auxPrice = round(unit.trail_amount, 4)        # 跟踪幅度（美元）
        trail.trailStopPrice = round(unit.stop_px, 2)       # 初始触发价
        trail.orderId = self.ib.client.getReqId()
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

    async def place_limit_with_stop(self, symbol: str, side: str, qty: int,
                                    limit_px: float, stop_px: float) -> dict:
        """
        限价单（父，LMT DAY）+ 固定 STOP 止损（子，STP GTC，parentId 关联）。
        parentId 关联语义：父单未成交时子单挂起；父单被撤/平仓时子单随之
        自动取消（这就是"平仓时看到子订单自动取消"的机制）。
        返回 {ok, entry_order_id, stop_order_id, status, ...}。
        """
        action = "BUY" if side == "long" else "SELL"
        opp = "SELL" if side == "long" else "BUY"

        if not (self.enabled and self._connected):
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
                            orderId=self.ib.client.getReqId(), transmit=False)
        self._apply_fa(parent)
        stop = Order()
        stop.orderType = "STP"
        stop.action = opp
        stop.totalQuantity = qty
        stop.tif = "GTC"
        stop.auxPrice = round(stop_px, 2)      # STOP 触发价 = ST 线值
        stop.orderId = self.ib.client.getReqId()
        stop.parentId = parent.orderId
        stop.transmit = True
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
                    "status": status, "error": err}
        logger.info("限价+STOP 已提交 %s %s x%d @%.2f | STOP 触发 %.2f "
                    "| entryId=%d(%s) stopId=%d(%s)",
                    symbol, action, qty, limit_px, stop_px,
                    parent.orderId, status, stop.orderId, stop_status)
        return {"ok": True, "entry_order_id": parent.orderId,
                "stop_order_id": stop.orderId, "status": status,
                "limit_price": limit_px, "trade": entry_trade,
                "stop_trade": stop_trade}

    async def place_close(self, symbol: str, side: str, qty: int,
                          ref_px: float) -> dict:
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

        if not (self.enabled and self._connected):
            fake = next(self._fake_ids)
            logger.info("[DRY-RUN] 平仓 %s: %s x%d @LMT %.2f",
                        symbol, action, qty, lmt)
            return {"ok": True, "dry_run": True, "order_id": fake,
                    "status": "DryRun", "limit_price": lmt}

        from ib_async import LimitOrder
        contract = await self._qualify(symbol)
        order = LimitOrder(action, qty, lmt, tif="DAY")
        self._apply_fa(order)
        trade = self.ib.placeOrder(contract, order)
        t0 = time.time()
        await asyncio.sleep(2.5)
        status = trade.orderStatus.status
        fatal = self._recent_fatal_error(t0)
        bad = ("Cancelled", "Inactive", "ApiCancelled")
        if fatal or status in bad:
            err = fatal["msg"] if fatal else f"平仓 {status}"
            logger.error("平仓失败 %s: %s", symbol, err)
            return {"ok": False, "order_id": order.orderId,
                    "status": status, "error": err}
        logger.info("平仓已提交 %s %s x%d @%.2f | orderId=%d(%s)",
                    symbol, action, qty, lmt, order.orderId, status)
        return {"ok": True, "order_id": order.orderId, "status": status,
                "limit_price": lmt, "trade": trade}

    async def modify_stop(self, unit, new_stop_px: float) -> bool:
        """修改 TRAIL 触发价（trailStopPrice）。"""
        if not (self.enabled and self._connected):
            logger.info("[DRY-RUN] TRAIL 触发价修改 %s: -> %.2f",
                        unit.symbol, new_stop_px)
            unit.stop_px = new_stop_px
            return True
        trade = getattr(unit, "trail_trade", None)
        if trade is None:
            logger.warning("%s 无 TRAIL 单句柄，无法修改", unit.symbol)
            return False
        trade.order.trailStopPrice = round(new_stop_px, 2)
        self.ib.placeOrder(trade.contract, trade.order)  # 同 orderId 即修改
        unit.stop_px = new_stop_px
        logger.info("TRAIL 触发价已修改 %s -> %.2f (orderId=%d)",
                    unit.symbol, new_stop_px, trade.order.orderId)
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
        """撤单个订单（dry-run / 未连接时返回 True 表示无需真实撤单）。"""
        if not (self.enabled and self._connected):
            return True
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

    async def fa_accounts(self) -> List[str]:
        """
        FA 账户组（self.fa_group）的成员账户列表。运行时从 TWS 的
        FA Groups 配置动态查询（requestFA, type=1）；查询失败回退
        config.IBKR_FA_ACCOUNTS。资金/盈亏/持仓/对账均只针对这组。
        """
        if not self._connected:
            return list(config.IBKR_FA_ACCOUNTS)
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
            logger.warning("动态查询 FA 组 %s 失败，回退 config: %s",
                           self.fa_group, e)
        return list(config.IBKR_FA_ACCOUNTS)

    async def positions(self) -> Dict[str, float]:
        """
        FA 组（dt_test）成员账户持仓按 symbol 聚合（仓位落在子账户，
        主账号净额可能为 0，必须跨账户聚合；且只统计本组成员）。
        """
        if not self._connected:
            return {}
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

    async def daily_pnl(self) -> Optional[float]:
        """
        当日盈亏 = 当前净值 - 前一日权益（IBKR PreviousDayEquityWithLoanValue）。
        注意：此口径含隔夜持仓的日内浮盈浮亏，适合「账户今日总盈亏」显示，
        不适合做日亏损熔断基准（隔夜持仓波动会误触发熔断）。
        取不到返回 None。
        """
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
