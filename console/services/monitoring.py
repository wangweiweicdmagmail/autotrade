"""monitoring application functions and owned state. No console.app dependency."""

from engine.execution_rules import fvg_reverse_action
from typing import Optional
from typing import Set
import asyncio
import config
import time
from console.shared import logger


class MonitoringService:
    def __init__(self, context):
        self.context = context

    async def _close_on_flip(
        self, ticker: str, sym: str, pos: float, close_px: Optional[float]
    ) -> bool:
        """趋势反向时只处理本工具 lot；外部持仓由独立清理循环处理。"""
        return False  # Compatibility entry point: ST alerts never authorize exits.

    def _legacy_tool_order_ids(self) -> set:
        ids = set()
        for lot in self.context.order_mgr.list_lots():
            values = list(lot.get("owned_order_ids") or [])
            values.extend(
                (
                    lot.get(key)
                    for key in (
                        "entry_order_id",
                        "trail_order_id",
                        "close_order_id",
                        "close_permId",
                    )
                )
            )
            for value in values:
                if value is not None:
                    try:
                        ids.add(int(value))
                    except (TypeError, ValueError):
                        continue
        return ids

    def _external_symbol(self, ticker: str) -> Optional[str]:
        for symbol in self.context.collector.latest() or {}:
            if symbol.split(":")[-1].upper() == ticker.upper():
                return symbol
        return None

    def _external_account_ok(self, order, accounts: set) -> bool:
        account = getattr(order, "account", "") or ""
        fa_group = getattr(order, "faGroup", "") or ""
        if fa_group == self.context.gateway.fa_group:
            return True
        return bool(account and account in accounts)

    async def _wait_trade_terminal(self, trade, timeout: float = 8.0) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if trade.isDone():
                return True
            await asyncio.sleep(0.25)
        return trade.isDone()

    async def _external_cleanup_once(self) -> dict:
        """外部（号段外/非工具白名单）订单与持仓的处理。
        EXTERNAL_ORDER_CLEANUP_ENABLED=1：自动撤挂单 + 市价平残余持仓（原行为）；
        =0（默认）：只检测不动作——写 `_external_alerts` 快照 + 节流事件，
        前端弹框/语音提醒人工处理（IBKR 手动单绝不自动市价平仓）；
        快照不完整时禁止任何写操作。"""
        stats = {"cancelled": 0, "closed": 0, "pending": 0, "failed": 0, "skipped": 0}
        if not (self.context.gateway.enabled and self.context.gateway._connected):
            return stats
        if config.EXTERNAL_MANUAL_GUARD_ENABLED:
            self._external_alerts = []
            gateway = getattr(self.context, "external_guard_gateway", self.context.gateway)
            if gateway is None or not gateway._connected:
                return stats
            await self.context.order_mgr.enforce_external_entry_policy(
                allow_market=self.context.system._is_trading_hours()
            )
            return stats
        # Broker positions and the lot ledger advance independently. Never
        # turn their difference into an order for a ticker owned by the tool.
        managed_tickers = {
            lot["symbol"].split(":")[-1].upper()
            for lot in self.context.order_mgr.list_lots()
        }
        positions, trades = (
            await self.context.gateway.positions(),
            await self.context.gateway.trade_map(),
        )
        if positions is None or trades is None:
            logger.warning("外部清理跳过：IBKR 订单或持仓快照不完整")
            stats["failed"] += 1
            return stats
        accounts = set(await self.context.gateway.fa_accounts())
        legacy_ids = self.context.monitoring._legacy_tool_order_ids()
        if (not config.EXTERNAL_ORDER_CLEANUP_ENABLED
                or getattr(self.context.order_mgr, "external_adoption_enabled", False)):
            alerts = []
            for trade in list(trades.values()):
                if trade.isDone():
                    continue
                order = trade.order
                order_id = int(order.orderId)
                if (
                    self.context.gateway.is_tool_order_id(order_id)
                    or order_id in legacy_ids
                ):
                    continue
                if not self.context.monitoring._external_account_ok(order, accounts):
                    continue
                alerts.append(
                    {
                        "kind": "order",
                        "ticker": trade.contract.symbol,
                        "side": order.action,
                        "qty": float(order.totalQuantity or 0),
                        "order_type": order.orderType,
                        "ts": time.time(),
                    }
                )
            tool_qty = {
                g["symbol"]: g["net_qty"] for g in self.context.order_mgr.aggregate()
            }
            for ticker in sorted(set(positions) | set(tool_qty)):
                residual = positions.get(ticker, 0) - tool_qty.get(ticker, 0)
                if abs(residual) < 1:
                    continue
                alerts.append(
                    {
                        "kind": "position",
                        "ticker": ticker,
                        "side": "long" if residual > 0 else "short",
                        "qty": abs(int(residual)),
                        "ts": time.time(),
                    }
                )
            self.context.monitoring._external_alerts = alerts
            for a in alerts:
                key = (a["kind"], a["ticker"])
                if (
                    time.time()
                    - self.context.monitoring._external_alert_last.get(key, 0)
                    < 60
                ):
                    continue
                self.context.monitoring._external_alert_last[key] = time.time()
                self.context.events.record_event(
                    "external_position_alert",
                    **{k: v for k, v in a.items() if k != "ts"},
                )
            return stats
        for trade in list(trades.values()):
            if trade.isDone():
                continue
            order = trade.order
            order_id = int(order.orderId)
            if (
                self.context.gateway.is_tool_order_id(order_id)
                or order_id in legacy_ids
            ):
                continue
            if not self.context.monitoring._external_account_ok(order, accounts):
                stats["skipped"] += 1
                logger.warning("外部挂单账户无法确认，跳过 orderId=%s", order_id)
                continue
            if order_id == 0:
                stats["skipped"] += 1
                key = ("manual_order", trade.contract.symbol)
                if (
                    time.time()
                    - self.context.monitoring._external_alert_last.get(key, 0)
                    >= 300
                ):
                    self.context.monitoring._external_alert_last[key] = time.time()
                    logger.warning(
                        "TWS 手动单 %s（orderId=0）API 不可撤，请在 TWS 手动处理或等其终结",
                        trade.contract.symbol,
                    )
                continue
            ok = self.context.gateway.cancel_order(trade)
            if not ok:
                stats["failed"] += 1
                self.context.events.record_event(
                    "external_order_cancel_failed",
                    order_id=order_id,
                    error="cancel_order 调用失败",
                )
                continue
            await self.context.monitoring._wait_trade_terminal(trade, timeout=5.0)
            status = trade.orderStatus.status
            if trade.isDone() and status != "Filled":
                stats["cancelled"] += 1
                self.context.events.record_event(
                    "external_order_cancelled",
                    order_id=order_id,
                    symbol=trade.contract.symbol,
                    status=status,
                )
            else:
                stats["failed"] += 1
                self.context.events.record_event(
                    "external_order_cancel_failed",
                    order_id=order_id,
                    symbol=trade.contract.symbol,
                    status=status,
                )
        positions = await self.context.gateway.positions()
        if positions is None:
            logger.warning("外部持仓清理跳过：撤单后持仓快照不可用")
            stats["failed"] += 1
            return stats
        tool_qty = {
            g["symbol"]: g["net_qty"] for g in self.context.order_mgr.aggregate()
        }
        for ticker in sorted(set(positions) | set(tool_qty)):
            residual = positions.get(ticker, 0) - tool_qty.get(ticker, 0)
            if abs(residual) < 1:
                continue
            if ticker.upper() in managed_tickers or self.context.order_mgr.list_lots(symbol=ticker):
                stats["skipped"] += 1
                key = ("managed_residual", ticker)
                if time.time() - self.context.monitoring._external_alert_last.get(key, 0) >= 60:
                    self.context.monitoring._external_alert_last[key] = time.time()
                    self.context.events.record_event(
                        "external_position_alert", kind="position", ticker=ticker,
                        qty=abs(int(residual)), side="long" if residual > 0 else "short",
                        reason="本工具有该标的订单记录，净仓差额需对账",
                    )
                continue
            existing = self.context.monitoring._external_cleanup_inflight.get(ticker)
            if existing is not None and (not existing.isDone()):
                stats["pending"] += 1
                continue
            self.context.monitoring._external_cleanup_inflight.pop(ticker, None)
            registered = next(
                (
                    t
                    for t in trades.values()
                    if getattr(t.order, "orderRef", "") == "external_cleanup"
                    and t.contract.symbol == ticker
                    and (not t.isDone())
                ),
                None,
            )
            if registered is not None:
                self.context.monitoring._external_cleanup_inflight[ticker] = registered
                stats["pending"] += 1
                continue
            symbol = self.context.monitoring._external_symbol(ticker)
            if symbol is None:
                stats["failed"] += 1
                self.context.events.record_event(
                    "external_position_close_failed",
                    ticker=ticker,
                    qty=abs(int(residual)),
                    error="标的不在订阅列表",
                )
                continue
            side = "long" if residual > 0 else "short"
            qty = abs(int(residual))
            ref_px = self.context.accounts._tv_last_price(symbol)
            if ref_px is None:
                stats["failed"] += 1
                self.context.events.record_event(
                    "external_position_close_failed",
                    symbol=symbol,
                    qty=qty,
                    error="TV 现价不可用",
                )
                continue
            result = await self.context.gateway.place_close(
                symbol, side, qty, ref_px, order_ref="external_cleanup"
            )
            if not result.get("ok") or result.get("trade") is None:
                stats["failed"] += 1
                self.context.events.record_event(
                    "external_position_close_failed",
                    symbol=symbol,
                    qty=qty,
                    error=result.get("error") or "平仓提交失败",
                )
                continue
            trade = result["trade"]
            self.context.monitoring._external_cleanup_inflight[ticker] = trade
            done = await self.context.monitoring._wait_trade_terminal(trade)
            status = trade.orderStatus.status
            if done and status == "Filled":
                stats["closed"] += 1
                self.context.monitoring._external_cleanup_inflight.pop(ticker, None)
                self.context.events.record_event(
                    "external_position_closed",
                    symbol=symbol,
                    side=side,
                    qty=qty,
                    order_id=result.get("order_id"),
                    avg_fill=trade.orderStatus.avgFillPrice,
                )
            elif done:
                stats["failed"] += 1
                self.context.monitoring._external_cleanup_inflight.pop(ticker, None)
                self.context.events.record_event(
                    "external_position_close_failed",
                    symbol=symbol,
                    qty=qty,
                    status=status,
                )
            else:
                stats["pending"] += 1
                self.context.events.record_event(
                    "external_position_close_pending",
                    symbol=symbol,
                    qty=qty,
                    order_id=result.get("order_id"),
                    status=status,
                )
        return stats

    async def _external_cleanup_loop(self) -> None:
        gateway = getattr(self.context, "external_guard_gateway", None)
        if gateway is not None and gateway is not self.context.gateway:
            if not await gateway.connect():
                gateway._start_reconnect()
        try:
            while True:
                try:
                    await self.context.monitoring._external_cleanup_once()
                except Exception as e:
                    logger.exception("外部订单清理异常: %s", e)
                await asyncio.sleep(5.0 if gateway is not None else 30.0)
        finally:
            if gateway is not None and gateway is not self.context.gateway:
                await gateway.disconnect()

    async def _position_scan_loop(self) -> None:
        """持仓扫描（每分钟，双向）：持仓方向与 trend_state 确认值（ts_c，上一根
        已收盘 5M bar，不看盘中实时值）冲突时——多仓见 direction==-1、空仓见
        direction==1——默认仅提醒，由用户决定是否平仓。"""
        while True:
            await asyncio.sleep(60.0)
            try:
                if (
                    not self.context.system._is_trading_hours()
                    or self.context.collector.data_health()["stale"]
                ):
                    continue
                positions = await self.context.gateway.positions()
                if positions is None:
                    logger.warning("持仓扫描跳过：IBKR 持仓快照不可用")
                    continue
                market = self.context.collector.latest()
                for ticker, pos in positions.items():
                    if pos == 0:
                        continue
                    side = "long" if pos > 0 else "short"
                    sym = None
                    for s, data in market.items():
                        if s.split(":")[-1] == ticker:
                            sym = s
                            break
                    if sym is None:
                        continue
                    data = market[sym]
                    ts_c = data.get("ts_c") or {}
                    confirmed_bar = data.get("bar_c") or {}
                    live_bar = data.get("bar") or {}
                    if (not confirmed_bar.get("time") or ts_c.get("time") != confirmed_bar.get("time")
                            or confirmed_bar["time"] + 300 != live_bar.get("time")):
                        continue  # Never substitute live ST for a confirmed-ST alert.
                    direction = ts_c.get("direction")
                    flipped = (
                        pos > 0 and direction == -1 or (pos < 0 and direction == 1)
                    )
                    if not flipped:
                        self.context.monitoring._scan_alerts.pop(ticker, None)
                        continue
                    st_line = ts_c.get("supertrend")
                    bar = data.get("bar") or {}
                    close = bar.get("close")
                    self.context.monitoring._scan_alerts[ticker] = {
                        "symbol": sym,
                        "ticker": ticker,
                        "side": side,
                        "qty": abs(pos),
                        "st_line": st_line,
                        "close": close,
                        "auto_close_failed": False,
                        "confirmed_direction": direction,
                        "realtime_direction": (data.get("ts") or {}).get("direction"),
                        "confirmed_bar_time": ts_c.get("time"),
                        "scanned_at": time.time(),
                    }
                    logger.info(
                        "持仓扫描: %s %s %s 股趋势已反向(ST %.2f)，提醒平仓",
                        ticker,
                        side,
                        abs(pos),
                        st_line or 0,
                    )
                    self.context.events.record_event(
                        "position_scan_alert",
                        symbol=sym,
                        ticker=ticker,
                        side=side,
                        confirmed_direction=direction,
                        realtime_direction=(data.get("ts") or {}).get("direction"),
                        confirmed_bar_time=ts_c.get("time"),
                        qty=abs(pos),
                        st_line=st_line,
                        close=close,
                    )
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.exception("持仓扫描异常: %s", e)

    async def _stop_guard_loop(self) -> None:
        """止损单守卫（每分钟）：FA 组每个持仓必须有足够的 STP/TRAIL 保护单覆盖，
        裸仓（保护单股数 < 持仓股数）每分钟提醒一次，直到补齐。只告警不自动补单。"""
        while True:
            await asyncio.sleep(60.0)
            try:
                if not config.STOP_GUARD_ENABLED or not self.context.gateway._connected:
                    continue
                positions = await self.context.gateway.positions()
                if positions is None:
                    logger.warning("止损守卫跳过：IBKR 持仓快照不可用")
                    continue
                covers = await self.context.gateway.open_protective_qty()
                if covers is None:
                    logger.warning("止损守卫跳过：IBKR 开放订单快照不可用")
                    continue
                now = time.time()
                missing: Set[str] = set()
                for ticker, pos in positions.items():
                    side = "long" if pos > 0 else "short"
                    need = "sell" if pos > 0 else "buy"
                    covered = (covers.get(ticker) or {}).get(need, 0.0)
                    if covered >= abs(pos):
                        continue
                    missing.add(ticker)
                    prev = self.context.monitoring._stop_alerts.get(ticker)
                    self.context.monitoring._stop_alerts[ticker] = {
                        "ticker": ticker,
                        "side": side,
                        "qty": abs(pos),
                        "covered_qty": covered,
                        "since": prev["since"] if prev else now,
                        "checked_at": now,
                    }
                    logger.warning(
                        "⚠️ 止损单缺失: %s %s %s 股，%s 保护单仅 %s 股——裸仓，请立即补挂止损",
                        ticker,
                        side,
                        abs(pos),
                        need.upper(),
                        covered,
                    )
                    self.context.events.record_event(
                        "missing_stop_alert",
                        ticker=ticker,
                        side=side,
                        qty=abs(pos),
                        covered_qty=covered,
                    )
                for t in list(self.context.monitoring._stop_alerts):
                    if t not in missing:
                        self.context.monitoring._stop_alerts.pop(t, None)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.exception("止损单守卫异常: %s", e)

    def _lot_protection_alive(self, lot, trades: Optional[dict]) -> bool:
        """ACTIVE lot 的 STP 保护单是否在 IBKR 侧存活：内存句柄未终结，或
        快照里 trail_order_id 对应订单未终结（重启后句柄丢失靠快照认回）。
        trades=None（快照不完整）时调用方应整拍跳过，不得据此告警。"""
        t = getattr(lot, "trail_trade", None)
        if t is not None and (not t.isDone()):
            return True
        oid = getattr(lot, "trail_order_id", None)
        if oid and trades:
            tr = trades.get(oid)
            if tr is not None and (not tr.isDone()):
                return True
        return False

    async def _naked_position_check_once(self, fails: dict) -> None:
        """裸仓巡检单拍（每 10s 由循环调用）：
        ACTIVE lot 的 STP 保护单缺失 → critical 告警 + 自动补挂（价格
        lot.stop_px，走 OrderManager._restore_protection——含幻影守卫与快照
        完整性检查，重建后认回句柄）；连续 3 次补挂失败 → 升级市价平仓 +
        最高级别告警。只补止损，绝不影响正常仓位；dry-run/未连接整拍跳过。"""
        if not (self.context.gateway.enabled and self.context.gateway._connected):
            return
        actives = [
            l
            for l in self.context.order_mgr.list_lots(active_only=True)
            if l["state"] == "ACTIVE" and not l.get("external_managed") and (l.get("filled_qty") or 0) > 0
        ]
        if not actives:
            return
        trades = await self.context.gateway.trade_map()
        if trades is None:
            logger.warning("裸仓巡检跳过：IBKR 订单快照不完整")
            return
        for d in actives:
            lot = self.context.order_mgr.get(d["lot_id"])
            if lot is None or lot.state != "ACTIVE":
                continue
            key = f"naked:{lot.lot_id}"
            if self.context.monitoring._lot_protection_alive(lot, trades):
                fails.pop(lot.lot_id, None)
                await self.context.notifier.recover(key, "STP 保护单已恢复存活")
                continue
            await self.context.notifier.notify(
                key,
                "critical",
                "裸仓告警",
                f"{lot.symbol} ACTIVE 仓 {lot.filled_qty} 股 STP 保护单缺失，自动补挂中 @ {lot.stop_px}",
            )
            logger.warning(
                "裸仓巡检: %s STP 缺失，自动补挂 @%.2f", lot.symbol, lot.stop_px
            )
            ok = await self.context.order_mgr.restore_protection(lot.lot_id)
            if ok:
                fails.pop(lot.lot_id, None)
                await self.context.notifier.recover(
                    key, f"已自动补挂 STP @ {lot.stop_px}"
                )
                continue
            if lot.state != "ACTIVE":
                continue  # An exit won the lock; do not classify it as naked.
            fails[lot.lot_id] = fails.get(lot.lot_id, 0) + 1
            logger.error(
                "裸仓巡检: %s 补挂失败（连续 %d 次）", lot.lot_id, fails[lot.lot_id]
            )
            if fails[lot.lot_id] >= 3:
                ref_px = (
                    self.context.accounts._tv_last_price(lot.symbol)
                    or lot.last_px
                    or lot.avg_fill
                    or lot.entry_px
                )
                ok2, err = await self.context.order_mgr.close_lot(
                    lot.lot_id, ref_px=ref_px, exit_reason="naked_guard"
                )
                await self.context.notifier.notify(
                    key + ":force_closed",
                    "critical",
                    "裸仓强平",
                    f"{lot.symbol} 补挂 STP 连续 3 次失败，"
                    + (
                        "已提交市价平仓，请核查成交"
                        if ok2
                        else f"平仓也失败（{err}），请立即人工处理"
                    ),
                )
                fails.pop(lot.lot_id, None)

    async def _naked_position_loop(self) -> None:
        """裸仓巡检循环（每 10s，单拍见 _naked_position_check_once）。"""
        fails: dict = {}
        while True:
            await asyncio.sleep(10.0)
            try:
                await self.context.monitoring._naked_position_check_once(fails)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.exception("裸仓巡检异常: %s", e)

    async def _health_check_once(self, was_connected: Optional[bool]) -> bool:
        """健康检查单拍（TWS 断线 + 数据陈旧），返回本轮 connected 状态。

        - TWS 断线：connected 跳变 False → critical「TWS 已断线，请人工重启
          7496」；恢复 → 解除通知。首轮只建基线不告警（启动瞬间未连接不报）。
        - 数据陈旧：交易时段内传输中断、内容停止变化或 bar 超龄 → warning；
          恢复自动解除并通知；非交易时段/周末静默（挂着的话静默摘除，不发恢复通知）。"""
        connected = bool(self.context.gateway._connected)
        if was_connected is not None:
            if was_connected and (not connected):
                await self.context.notifier.notify(
                    "tws_down", "critical", "TWS 断线", "TWS 已断线，请人工重启 7496"
                )
            elif not was_connected and connected:
                await self.context.notifier.recover("tws_down", "TWS 已恢复连接")
        health = self.context.collector.data_health()
        if health["trading_hours"]:
            if health["stale"]:
                content_age = health["content_age"]
                age_txt = "∞" if content_age is None else f"{int(content_age)}s"
                await self.context.notifier.notify(
                    "data_stale",
                    "warning",
                    "行情数据陈旧",
                    f"TV 内容 {age_txt} 未变化（{health['reason']}），正在自动重建订阅",
                )
            else:
                await self.context.notifier.recover("data_stale", "行情数据已恢复更新")
        else:
            self.context.notifier.clear("data_stale")
        sh = self.context.collector.study_health()
        if sh["systemic"]:
            await self.context.notifier.notify(
                "tv_study_dead",
                "critical",
                "指标数据中断",
                f"60 秒内指标错误 {sh['study_errors_60s']} 条，ts_c 就绪 {sh['ts_ready']}/{sh['ts_total']}（持续 {int(sh['ts_dead_for'])} 秒）。请查看行情采集日志",
            )
        else:
            await self.context.notifier.recover("tv_study_dead", "指标数据已恢复")
        return connected

    async def _health_watch_loop(self) -> None:
        """健康巡检循环（每 5s，单拍见 _health_check_once）。"""
        was_connected: Optional[bool] = None
        while True:
            await asyncio.sleep(5.0)
            try:
                was_connected = await self.context.monitoring._health_check_once(
                    was_connected
                )
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.exception("健康巡检异常: %s", e)

    async def _slow_break_loop(self) -> None:
        """未快速突破提醒（每分钟）：ACTIVE 持仓开仓超过 SLOW_BREAK_SECONDS 仍未保本
        → 记录提醒，前端语音播报「未快速突破，是否减仓」。每个 lot 只提醒一次。"""
        while True:
            await asyncio.sleep(60.0)
            try:
                if not self.context.system._is_trading_hours():
                    continue
                now = time.time()
                active = self.context.order_mgr.list_lots(active_only=True)
                active_ids = set()
                for lot in active:
                    if not self._uses_legacy_guards(lot):
                        continue
                    lot_id = lot.get("lot_id")
                    if not lot_id:
                        continue
                    active_ids.add(lot_id)
                    if lot.get("breakeven_reached"):
                        continue
                    entry_time = lot.get("entry_time") or 0
                    if entry_time <= 0:
                        continue
                    elapsed = now - entry_time
                    if elapsed < config.SLOW_BREAK_SECONDS:
                        continue
                    if lot_id not in self.context.monitoring._slow_break_alerts:
                        self.context.monitoring._slow_break_alerts[lot_id] = {
                            "lot_id": lot_id,
                            "symbol": lot.get("symbol"),
                            "ticker": (lot.get("symbol") or "").split(":")[-1],
                            "qty": lot.get("filled_qty") or lot.get("qty"),
                            "elapsed_seconds": round(elapsed, 1),
                            "alerted_at": now,
                        }
                        logger.warning(
                            "未快速突破提醒: %s 开仓 %.0f 分钟未保本，提示是否减仓",
                            lot_id,
                            elapsed / 60,
                        )
                        self.context.events.record_event(
                            "slow_break_alert",
                            lot_id=lot_id,
                            symbol=lot.get("symbol"),
                            elapsed_seconds=round(elapsed, 1),
                        )
                for lid in list(self.context.monitoring._slow_break_alerts):
                    if lid not in active_ids:
                        self.context.monitoring._slow_break_alerts.pop(lid, None)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.exception("未快速突破提醒异常: %s", e)

    def _is_pyramid_lot(self, lot: dict) -> bool:
        meta = lot.get("signal_snapshot") or {}
        return (
            bool(meta.get("strategy_id"))
            or lot.get("limit_type") == "auto_ema9_pyramid"
            or bool(meta.get("pyramid_seed"))
        )

    @staticmethod
    def _uses_legacy_guards(lot: dict) -> bool:
        """旧 ST/FVG 叠加规则只适用于旧单和 EMA9 兼容配置。"""
        return (lot.get("signal_snapshot") or {}).get("strategy_id") in (
            None,
            "ema9_pyramid",
        )

    async def _auto_breakeven_loop(self) -> None:
        """普通订单按配置保本，突破单入场所在K线收盘后按信号收盘价保护并保留1R保本。"""
        scan_tick = 0
        last_error: dict = {}
        last_attempt: dict = {}
        while True:
            await asyncio.sleep(1.0)
            try:
                trading_hours = self.context.system._is_trading_hours()
                if (
                    not self.context.gateway._connected
                    or self.context.collector.data_health()["stale"]
                ):
                    continue
                scan_tick += 1
                active = self.context.order_mgr.list_lots(active_only=True)
                market = self.context.collector.latest() or {}
                if scan_tick % 30 == 0:
                    logger.info("自动保本循环扫描: %d 个活跃 lot", len(active))
                now = time.time()
                for lot in active:
                    lot_id = lot.get("lot_id")
                    if lot.get("external_managed") or self.context.monitoring._is_pyramid_lot(lot):
                        continue
                    if lot.get("state") != "ACTIVE":
                        continue
                    side = lot.get("side")
                    breakout = bool((lot.get("signal_snapshot") or {}).get("opening_breakout_id"))
                    if breakout and (not lot.get("avg_fill") or not lot.get("filled_qty")):
                        continue
                    if breakout:
                        guard_key = f"signal_close:{lot_id}"
                        if now - last_attempt.get(guard_key, 0) >= 5:
                            outcome = await self.context.order_mgr.protect_breakout_signal_close(
                                lot_id, market.get(lot.get("symbol"), {}), now=now,
                            )
                            action = outcome.get("action")
                            if action not in ("waiting", "already"):
                                last_attempt[guard_key] = now
                            if outcome["ok"] and action in ("stop_updated", "stop_kept", "close_submitted"):
                                self.context.events.record_event(
                                    "breakout_close_protection", lot_id=lot_id,
                                    symbol=lot.get("symbol"), side=side, **outcome,
                                )
                                logger.warning("突破五分钟收盘价保护 %s %s: %s", lot.get("symbol"), side, outcome)
                            elif not outcome["ok"] and now - last_error.get(guard_key, 0) >= 30:
                                last_error[guard_key] = now
                                logger.warning("突破五分钟收盘价保护失败 %s: %s", lot.get("symbol"), outcome)
                            if action == "close_submitted" or not outcome["ok"]:
                                continue
                    if breakout:
                        continue  # 突破仅按入场K线收盘保护，不启用1R自动保本。
                    if not trading_hours or lot.get("breakeven_reached"):
                        continue
                    avg_fill = lot.get("avg_fill") or lot.get("entry_px")
                    init_entry = lot.get("init_entry_px") or lot.get("entry_px")
                    init_stop = lot.get("init_stop_px") or lot.get("stop_px")
                    filled = lot.get("filled_qty") or lot.get("qty")
                    if (
                        not avg_fill
                        or not init_entry
                        or (not init_stop)
                        or (filled <= 0)
                    ):
                        continue
                    risk = abs(init_entry - init_stop)
                    if risk <= 0:
                        continue
                    _snap = market.get(lot.get("symbol"), {})
                    _bar = _snap.get("bar") or {}
                    price = _bar.get("high" if side == "long" else "low")
                    if not price or price <= 0:
                        continue
                    profit = price - avg_fill if side == "long" else avg_fill - price
                    rr = profit / risk
                    trigger = "profit_r"
                    if rr < config.AUTO_BREAKEVEN_R:
                        continue
                    if now - last_attempt.get(lot_id, 0) < 5.0:
                        continue
                    last_attempt[lot_id] = now
                    ok, err = await self.context.order_mgr.breakeven(lot_id)
                    if ok:
                        logger.warning(
                            "自动保本 %s %s 浮盈 %.2fR，止损移成本 %.2f",
                            lot.get("symbol"),
                            side,
                            rr,
                            avg_fill,
                        )
                        self.context.events.record_event(
                            "auto_breakeven",
                            lot_id=lot_id,
                            symbol=lot.get("symbol"),
                            side=side,
                            unrealized_r=round(rr, 2),
                            new_stop=round(avg_fill, 2),
                            trigger=trigger,
                        )
                        last_error.pop(lot_id, None)
                    elif now - last_error.get(lot_id, 0) >= 30:
                        last_error[lot_id] = now
                        logger.warning(
                            "自动保本失败 %s %s: %s", lot.get("symbol"), side, err
                        )
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.exception("自动保本循环异常: %s", e)

    _fvg_reverse_action = staticmethod(fvg_reverse_action)

    async def _fvg_reverse_guard_loop(self) -> None:
        """持仓反向 FVG 守卫（每秒，5M 收盘确认口径）：本工具 ACTIVE lot 的标的出现
        反向缺口确认（fvg_c.time == bar_c.time 对齐）时只提醒。
        保本或退出必须由用户明确操作；每 lot 每根确认 bar 只提醒一次。"""
        while True:
            await asyncio.sleep(1.0)
            try:
                if not (
                    config.FVG_REVERSE_GUARD_ENABLED
                    and self.context.system._is_trading_hours()
                    and self.context.gateway._connected
                    and (not self.context.collector.data_health()["stale"])
                ):
                    continue
                market = self.context.collector.latest() or {}
                active = [
                    l
                    for l in self.context.order_mgr.list_lots(active_only=True)
                    if l.get("state") == "ACTIVE"
                ]
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
                        continue
                    act_key = f"{lot_id}:{int(bt)}"
                    if act_key in self.context.monitoring._fvg_reverse_acted:
                        continue
                    price = self.context.accounts._tv_last_price(symbol) or bar_c.get(
                        "close"
                    )
                    avg_fill = lot.get("avg_fill") or lot.get("entry_px")
                    action = self.context.monitoring._fvg_reverse_action(
                        side, event, avg_fill, price
                    )
                    if action is None:
                        continue
                    self.context.monitoring._fvg_reverse_acted[act_key] = time.time()
                    ticker = symbol.split(":")[-1]
                    qty = lot.get("filled_qty") or lot.get("qty")
                    fvg_top = fvg_c.get("fvg_last_top")
                    fvg_bottom = fvg_c.get("fvg_last_bottom")
                    pnl = (
                        price - avg_fill if side == "long" else avg_fill - price
                    ) * qty
                    self.context.monitoring._fvg_reverse_alerts[lot_id] = {
                        "kind": "reverse_warning",
                        "requires_confirmation": True,
                        "recommended_action": action,
                        "lot_id": lot_id,
                        "symbol": symbol,
                        "ticker": ticker,
                        "side": side,
                        "qty": qty,
                        "price": price,
                        "pnl": round(pnl, 2),
                        "fvg_top": fvg_top,
                        "fvg_bottom": fvg_bottom,
                        "alerted_at": time.time(),
                    }
                    self.context.events.record_event(
                        "fvg_reverse_alert",
                        requires_confirmation=True, recommended_action=action,
                        lot_id=lot_id,
                        symbol=symbol,
                        ticker=ticker,
                        side=side,
                        qty=qty,
                        price=price,
                        pnl=round(pnl, 2),
                        fvg_top=fvg_top,
                        fvg_bottom=fvg_bottom,
                    )
                active_ids = {l.get("lot_id") for l in active}
                for lid in list(self.context.monitoring._fvg_reverse_alerts):
                    if lid not in active_ids:
                        self.context.monitoring._fvg_reverse_alerts.pop(lid, None)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.exception("反向FVG守卫异常: %s", e)

    async def _eod_force_close_loop(self) -> None:
        """15:45 美东时间强制平掉所有活跃仓位（日内不过夜）。"""
        from zoneinfo import ZoneInfo
        import datetime as _dt

        et = ZoneInfo("America/New_York")
        done_days: set = set()
        while True:
            await asyncio.sleep(30.0)
            try:
                now_et = _dt.datetime.now(et)
                if now_et.hour == 15 and now_et.minute >= 45 and (now_et.minute < 59):
                    day_key = now_et.strftime("%Y%m%d")
                    if day_key in done_days:
                        continue
                    active = self.context.order_mgr.list_lots(active_only=True)
                    logger.warning("EOD 强平触发：%d 个活跃仓位", len(active))
                    all_ok = True
                    for lot in active:
                        if lot["state"] == "PENDING":
                            ok, err = await self.context.order_mgr.cancel_lot(
                                lot["lot_id"]
                            )
                        elif lot["state"] == "ACTIVE":
                            ok, err = await self.context.order_mgr.close_lot(
                                lot["lot_id"]
                            )
                        else:
                            continue
                        if not ok:
                            all_ok = False
                        logger.info(
                            "EOD 处理 %s: %s",
                            lot["lot_id"],
                            "成功" if ok else f"失败({err})",
                        )
                    if all_ok:
                        done_days.add(day_key)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.exception("EOD 强平循环异常: %s", e)

    async def _reconcile_loop(self) -> None:
        """每 60s 订单对账：重挂丢失的 watcher、补登系统外平仓记录
        （重启 / 断线重连 / TWS 手动操作后的自愈）。"""
        while True:
            await asyncio.sleep(60.0)
            try:
                await self.context.order_mgr.reconcile()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.exception("订单对账循环异常: %s", e)
