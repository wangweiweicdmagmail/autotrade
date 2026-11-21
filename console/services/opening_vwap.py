"""Operator reservations for the regular-session opening VWAP signal."""

import asyncio
import time
from datetime import datetime, time as clock
from zoneinfo import ZoneInfo

from fastapi import HTTPException

import config
from console.shared import logger
from domain.contracts import finite_number

ET = ZoneInfo("America/New_York")
CONFIRM_WAIT = 4
SUBMIT_WINDOW = 12
PRICE_MAX_AGE = 10
STRATEGY_ID = "opening_vwap_1m"


class OpeningVwapService:
    def __init__(self, context):
        self.context = context
        self._opening = None
        self._orders = {}
        self._last_managed = {}
        self.task = None

    def _day(self, now):
        local = datetime.fromtimestamp(now, ET)
        opening = int(datetime.combine(local.date(), clock(9, 30), ET).timestamp())
        if self._opening != opening:
            self._opening = opening
            self._orders.clear()
        return opening

    def _market(self):
        return self.context.collector.vwap_opening

    def _price(self, feed, now):
        stamp = finite_number(feed.get("bar_time"))
        observed = finite_number(feed.get("observed_at"))
        price = finite_number(feed.get("price"))
        if (stamp is not None and stamp <= now < stamp + 60 and observed is not None
                and 0 <= now - observed <= PRICE_MAX_AGE and price is not None and price > 0):
            return price
        return None

    def _signals(self, now):
        opening = self._day(now)
        if datetime.fromtimestamp(now, ET).weekday() >= 5:
            return {}
        rows = {}
        for symbol, feed in self._market().items():
            bar_time = finite_number(feed.get("bar_time"))
            if feed.get("opening") != opening or bar_time is None:
                continue
            for signal in feed.get("signals", []):
                close_at = signal.get("close_at")
                if close_at not in range(opening + 180, opening + 301, 60):
                    continue
                if signal.get("bar_time") != close_at - 60 or signal.get("side") not in ("long", "short"):
                    continue
                if any(finite_number(signal.get(name)) is None
                       for name in ("entry_px", "stop_px", "body_pct")):
                    continue
                confirmed = signal.get("confirmed") is True and now >= close_at and bar_time >= close_at
                preview = (signal.get("confirmed") is False and now < close_at
                           and self._price(feed, now) is not None)
                if not (confirmed or preview):
                    continue
                key = f"{symbol}:{int(close_at)}"
                rows[key] = {**signal, "id": key, "symbol": symbol,
                             "status": "confirmed" if confirmed else "preview"}
        return rows

    def display(self, now):
        rows = self._signals(now)
        for key, order in self._orders.items():
            row = rows.get(key)
            if row is None or row["side"] != order["preview"]["side"]:
                row = order["preview"]
            rows[key] = {**row, "status": order["status"], "detail": order.get("detail"),
                         "can_cancel": order["status"] == "armed" and now < row["close_at"]}
        return sorted(rows.values(), key=lambda r: (r["close_at"], -r["body_pct"], r["symbol"]))

    def prepare(self, request, trader):
        now = time.time()
        rows = self._signals(now)
        existing = self._orders.get(request.signal_id)
        if request.cancel:
            if (not existing or existing["status"] != "armed" or existing["trader"] != trader
                    or now >= existing["preview"]["close_at"]):
                raise HTTPException(409, "当前预约无法撤销")
            del self._orders[request.signal_id]
            return {"status": "cancelled"}
        row = rows.get(request.signal_id)
        if existing or not row or row["status"] != "preview" or row["side"] != request.side:
            raise HTTPException(409, "预选信号已失效、已预约或已进入收盘确认")
        if self.context.collector.data_health()["stale"]:
            raise HTTPException(409, "行情陈旧，不能预约")
        if self.context.gateway.enabled and not self.context.gateway._connected:
            raise HTTPException(409, "实盘券商未连接，不能预约")
        self._orders[request.signal_id] = {"preview": row, "trader": trader, "status": "armed"}
        self.context.events.record_event("opening_vwap_armed", symbol=row["symbol"],
                                         side=row["side"], signal_id=row["id"], trader=trader)
        return {"status": "armed"}

    async def _submit(self, row, trader, now):
        if row["side"] == "short" and not config.ALLOW_SHORT:
            return "rejected", "当前仅允许做多"
        price = self._price(self._market().get(row["symbol"], {}), now)
        stop = finite_number(row.get("stop_px"))
        reference = finite_number(row.get("entry_px"))
        if (price is None or stop is None or reference is None or stop <= 0
                or not (stop < min(price, reference) if row["side"] == "long"
                        else max(price, reference) < stop)):
            return "rejected", "成交观察价缺失或已经越过初始止损"
        risk = config.RISK_PER_TRADE_USD
        qty = min(int(risk / abs(price - stop)), config.RISK_MAX_ORDER_QTY)
        cap = config.RISK_MAX_SYMBOL_POSITION_PCT
        equity = self.context.accounts._equity_cache.get("equity") or 0
        if equity > 0:
            qty = min(qty, int(equity * cap / 100 / price))
        if qty <= 0:
            return "rejected", "风险预算或仓位上限不足"
        lot, detail = await self.context.order_mgr.place_limit_stop_lot(
            symbol=row["symbol"], side=row["side"], qty=qty, limit_px=reference, stop_px=stop,
            limit_type="market", reason="开盘 VWAP 三连收盘确认", risk_usd=risk,
            risk_ref_px=price, max_position_pct=cap, trader=trader, manual_entry=True,
            entry_order_type="MKT", submission_deadline=row["close_at"] + SUBMIT_WINDOW,
            signal_snapshot={
                "strategy_id": STRATEGY_ID, "opening_vwap_opening": self._opening,
                "opening_vwap_signal_id": row["id"], "opening_vwap_evidence": row,
            },
        )
        if lot is not None:
            self.context.events.record_event("opening_vwap_submitted", lot_id=lot.lot_id,
                                             symbol=row["symbol"], signal_id=row["id"])
            return "submitted", detail
        if any((item.get("signal_snapshot") or {}).get("opening_vwap_signal_id") == row["id"]
               and (item["state"] in ("PENDING", "ACTIVE", "CLOSING") or item.get("entry_submission_pending"))
               for item in self.context.order_mgr.list_lots(symbol=row["symbol"])):
            return "unknown", detail
        return "rejected", detail

    async def advance(self, now):
        signals = self._signals(now)
        due = {}
        for key, order in self._orders.items():
            preview = order["preview"]
            close_at = preview["close_at"]
            if order["status"] == "armed" and now >= close_at:
                due.setdefault(close_at, []).append((key, order))
        for close_at, cohort in sorted(due.items()):
            market = self._market()
            if now < close_at + CONFIRM_WAIT and any(
                (market.get(order["preview"]["symbol"], {}).get("bar_time") or 0) < close_at
                for _, order in cohort
            ):
                continue
            ready = []
            for key, order in cohort:
                final = signals.get(key)
                if now >= close_at + SUBMIT_WINDOW:
                    order.update(status="expired", detail="超过收盘提交窗口")
                elif (not final or final["status"] != "confirmed"
                      or final["side"] != order["preview"]["side"]):
                    order.update(status="invalid", detail="收盘未确认预约方向和全部入场条件")
                else:
                    ready.append((final, order))
            for final, order in sorted(ready, key=lambda item: (-item[0]["body_pct"], item[0]["symbol"])):
                order["status"] = "submitting"
                try:
                    status, detail = await self._submit(final, order["trader"], time.time())
                except Exception:
                    logger.exception("开盘 VWAP 提交待核对 %s", final["id"])
                    order.update(status="unknown", detail="提交结果待对账")
                else:
                    order.update(status=status, detail=detail)

    async def manage(self, now):
        local = datetime.fromtimestamp(now, ET)
        if local.weekday() >= 5 or not clock(9, 30) <= local.time() < clock(16):
            return
        if self.context.gateway.enabled and not self.context.gateway._connected:
            return
        manager = self.context.order_mgr
        lots = manager.list_lots(active_only=True)
        live_ids = {lot["lot_id"] for lot in lots}
        self._last_managed = {key: value for key, value in self._last_managed.items() if key in live_ids}
        for lot in lots:
            meta = lot.get("signal_snapshot") or {}
            if meta.get("strategy_id") != STRATEGY_ID or lot.get("external_managed"):
                continue
            opening = finite_number(meta.get("opening_vwap_opening"))
            if opening is None:
                continue
            key = lot["lot_id"]
            if now - self._last_managed.get(key, 0) < 3:
                continue
            price = self._price(self._market().get(lot["symbol"], {}), now)
            close_at = finite_number((meta.get("opening_vwap_evidence") or {}).get("close_at"))
            cancel_at = close_at + SUBMIT_WINDOW if close_at is not None else opening + 900
            if now >= cancel_at and lot["state"] == "PENDING":
                action = manager.cancel_lot(key)
            elif now >= opening + 900 and lot["state"] == "ACTIVE":
                action = manager.close_lot(key, ref_px=price, exit_reason="opening_vwap_time_exit", market_exit=True)
            elif lot["state"] == "ACTIVE" and not lot.get("breakeven_reached"):
                entered = finite_number(lot.get("entry_broker_time")) or finite_number(lot.get("entry_time"))
                cost = finite_number(lot.get("avg_fill"))
                if not entered or not cost or now < entered + 180 or price is None:
                    continue
                crossed = price <= cost if lot["side"] == "long" else price >= cost
                if crossed:
                    action = manager.close_lot(key, ref_px=price, exit_reason="opening_vwap_breakeven_crossed",
                                               market_exit=True)
                else:
                    action = manager.breakeven(key)
            else:
                continue
            self._last_managed[key] = now
            ok, detail = await action
            self.context.events.record_event("opening_vwap_manage", lot_id=key, ok=ok, detail=detail)

    async def run(self):
        while True:
            await asyncio.sleep(1)
            try:
                await self.advance(time.time())
                await self.manage(time.time())
            except Exception:
                logger.exception("开盘 VWAP 预约与持仓处理异常")
