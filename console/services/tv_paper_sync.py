"""Mirror confirmed execution lots into a dedicated TradingView Paper account."""

import asyncio
import json
import math
import os
import config
import re
from pathlib import Path

from console.shared import logger
from controller.tv_cdp import TradingViewCDPController


_BRIDGE = Path(__file__).resolve().parents[2] / "controller" / "tv_paper_bridge.js"
_WORKING = {3, 4, 6}
_TV_SYMBOL = re.compile(r"[A-Z0-9_.-]+:[A-Z0-9_.!-]+")


def configured_paper_account(data_dir):
    """Explicit environment overrides the local account binding (including blank)."""
    if not config.TV_PAPER_SYNC_ENABLED:
        return ""
    override = os.environ.get("TV_PAPER_SYNC_ACCOUNT")
    if override is not None:
        return override
    try:
        return (data_dir / "tv_paper_sync_account").read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return ""
    except (OSError, UnicodeError):
        logger.exception("TV 模拟账户绑定无法读取；同步保持关闭")
        return ""


class TvPaperSync:
    def __init__(self, context, account="", *, connect=TradingViewCDPController):
        self.context = context
        self.account = account.strip()
        self.connect = connect
        self.path = context.paths.data_dir / "tv_paper_sync.json"
        self.links = {}
        self.state = "等待连接" if self.account else "未启用"
        self.notices = []

    def status(self):
        return {"enabled": bool(self.account), "state": self.state,
                "linked": len(self.links), "notices": self.notices}

    def _load(self):
        if not self.path.exists():
            return
        data = json.loads(self.path.read_text())
        if data.get("version") != 1 or data.get("account") != self.account or not isinstance(data.get("links"), dict):
            raise ValueError("TV 模拟同步账本版本或账户不匹配")
        self.links = data["links"]

    def _save(self):
        tmp = self.path.with_suffix(".tmp")
        with open(tmp, "w", opener=lambda path, flags: os.open(path, flags, 0o600)) as stream:
            json.dump({"version": 1, "account": self.account, "links": self.links}, stream, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        tmp.replace(self.path)

    def _notice(self, symbol, message, **details):
        self.notices.append({"symbol": symbol, "message": message, **details})

    async def _call(self, tv, action, **args):
        payload = json.dumps([self.account, action, args], ensure_ascii=True, allow_nan=False)
        return await tv.evaluate(f"({_BRIDGE.read_text()})(...{payload})")

    async def _send(self, tv, link, action, before, target, **args):
        link["pending"] = {"action": action, "target": target, "before_ids": sorted(before),
                           "qty": args.get("qty"), "side": args["side"]}
        self._save()
        result = await self._call(tv, action, **args)
        if isinstance(result, dict) and result.get("orderId"):
            link["pending"]["order_id"] = str(result["orderId"])
            self._save()

    @staticmethod
    def _stop(lot):
        value = lot.get("stop_px")
        if (lot.get("protection_order_status") not in {"Submitted", "PreSubmitted"}
                or not lot.get("trail_order_id")
                or lot.get("protection_order_qty") != lot.get("filled_qty")
                or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0):
            return None
        return round(value, 2)

    async def reconcile(self, tv):
        self.notices = []
        gateway = self.context.gateway
        if not gateway.enabled or not gateway._connected:
            self.state = "等待实盘连接"
            return
        recovery = self.context.order_mgr.recovery_status()
        if any(issue.get("kind") == "external_adoption" for issue in recovery["issues"]):
            self.state = "等待 TWS 确认 FA 账户组及订单快照"
            return
        if (recovery.get("phase") != "checked" or recovery.get("error")
                or recovery.get("checked_at", 0) < gateway.connected_at
                or any(issue.get("kind") != "order" and not issue.get("lot_id")
                       for issue in recovery["issues"])):
            self.state = "等待实盘账本恢复"
            return
        connection = gateway.connected_at
        self.state = "运行中"
        by_symbol = {}
        all_lots = {lot["lot_id"]: lot for lot in self.context.order_mgr.list_lots()}
        blocked = {issue.get("lot_id") for issue in recovery["issues"]}
        differences = {row["symbol"].split(":")[-1] for row in recovery["position_differences"]}
        for lot in all_lots.values():
            if lot["state"] in {"PENDING", "ACTIVE", "CLOSING"} and lot["filled_qty"] > 0:
                by_symbol.setdefault(lot["symbol"], []).append(lot)
        symbols = by_symbol.keys() | self.links.keys()
        if not symbols:
            return
        snap = await self._call(tv, "snapshot", history=True)
        for symbol in sorted(symbols):
            if not gateway._connected or gateway.connected_at != connection:
                self.state = "等待实盘账本恢复"
                return
            current_recovery = self.context.order_mgr.recovery_status()
            if current_recovery != recovery:
                self.state = "等待实盘账本恢复"
                return
            lots = by_symbol.get(symbol, [])
            if len(lots) > 1:
                self._notice(symbol, "同标的有多个实盘批次，暂停自动同步")
                continue
            link = self.links.get(symbol)
            tracked = all_lots.get(link["lot_id"]) if link else None
            if symbol.split(":")[-1] in differences or any(
                    item and item["lot_id"] in blocked for item in [*lots, tracked]):
                self._notice(symbol, "等待实盘批次对账")
                continue
            lot = lots[0] if lots else None
            if tracked and tracked["state"] == "CLOSED":
                # Close the previous mirror before considering a new batch in this symbol.
                lot = None
            await self._reconcile_symbol(tv, symbol, lot, snap, tracked)

    async def _reconcile_symbol(self, tv, symbol, lot, snap, tracked):
        positions = [p for p in snap["positions"] if p["symbol"] == symbol and p.get("qty", 0) > 0]
        occupied = [p for p in snap["positions"] if p.get("qty", 0) > 0]
        occupied += [o for o in snap["orders"] if o.get("status") in _WORKING]
        if any(row.get("symbol", "").split(":")[-1] == symbol.split(":")[-1]
               and row["symbol"] != symbol for row in occupied):
            self._notice(symbol, "TV 有同代码但交易所不同的仓位或挂单，暂停同步")
            return
        if len(positions) > 1:
            self._notice(symbol, "TV 有多个同标的仓位")
            return
        pos = positions[0] if positions else {}
        qty = pos.get("qty", 0)
        rows = {str(o["id"]): o for o in snap["history"] + snap["orders"] if o["symbol"] == symbol}
        working = [o for o in snap["orders"] if o["symbol"] == symbol and o["status"] in _WORKING]
        stops = [o for o in working if o.get("type") == 3 and o.get("parentType") == 2
                 and o.get("parentId") == pos.get("id")]
        stop = stops[0] if len(stops) == 1 else {}
        paper_stop = stop.get("stopPrice")
        link = self.links.get(symbol)
        if link and (pending := link.get("pending")):
            if pending["action"] == "stop":
                confirmed = (paper_stop == pending["target"] == pos.get("stopLoss")
                             and qty == link["qty"] == stop.get("qty")
                             and pos.get("id") == link["position_id"] and pos.get("side") == link["side"])
            else:
                matches = [o for oid, o in rows.items()
                           if oid not in pending["before_ids"]
                           and (not pending.get("order_id") or oid == pending["order_id"])
                           and o.get("type") == 2 and o.get("status") == 2
                           and o.get("side") == pending["side"] * (1 if pending["action"] == "open" else -1)
                           and o.get("qty") == pending["qty"]]
                confirmed = (len(matches) == 1 and qty == pending["target"]
                             and (qty == 0 or pos.get("side") == link["side"])
                             and (pending["action"] == "open" or qty == 0 or pos.get("id") == link["position_id"]))
            if not confirmed:
                self._notice(symbol, "上次 TV 操作结果待核对，暂停自动重发")
                return
            link.pop("pending")
            link["qty"] = qty
            link["position_id"] = pos.get("id")
            if pending["action"] == "open":
                link["entry_order_id"] = str(matches[0]["id"])
            if pending["action"] == "stop":
                link["live_stop"] = pending["target"]
            self._save()
        if link and not lot and not qty and link["qty"] == 0:
            del self.links[symbol]
            self._save()
            return
        if not link:
            if not lot:
                return
            if qty or working:
                self._notice(symbol, "TV 已有未绑定的仓位或挂单")
                return
            if not _TV_SYMBOL.fullmatch(symbol) or int(lot["filled_qty"]) != lot["filled_qty"]:
                self._notice(symbol, "暂不支持此 TV 标的格式或非整数数量")
                return
            live_stop = self._stop(lot)
            if lot["state"] != "ACTIVE" or not live_stop or lot.get("avg_fill", 0) <= 0 or lot.get("side") not in {"long", "short"}:
                self._notice(symbol, "等待实盘成交和保护单确认")
                return
            side = 1 if lot["side"] == "long" else -1
            link = self.links[symbol] = {"lot_id": lot["lot_id"], "side": side, "qty": 0,
                                         "live_stop": live_stop}
            await self._send(tv, link, "open", rows, lot["filled_qty"], symbol=symbol,
                             side=side, qty=lot["filled_qty"], previousQty=0, stop=live_stop)
            return
        if (lot and lot["lot_id"] != link["lot_id"]) or qty != link["qty"] or (
                qty and (pos.get("id") != link.get("position_id") or pos.get("side") != link["side"])):
            self._notice(symbol, "实盘批次或 TV 仓位已变化，暂停同步")
            return
        if not lot and not (tracked and tracked["state"] == "CLOSED"
                            and tracked.get("closed_at", 0) > 0 and tracked.get("exit_px", 0) > 0):
            self._notice(symbol, "缺少该批次的实盘平仓成交记录，暂停同步")
            return
        if lot and lot["state"] != "ACTIVE":
            return
        target = lot["filled_qty"] if lot else 0
        if target < qty:
            await self._send(tv, link, "reduce", rows, target, symbol=symbol, side=link["side"],
                             qty=qty-target, positionId=pos["id"], previousQty=qty,
                             previousStop=pos.get("stopLoss"))
            return
        if target > qty:
            self._notice(symbol, "实盘数量增加，当前仅同步单批开仓与减仓")
            return
        if not lot or not qty:
            return
        live_stop = self._stop(lot)
        if not live_stop or len(stops) != 1 or len(working) != 1 or paper_stop != pos.get("stopLoss") or stop.get("qty") != qty:
            self._notice(symbol, "止损价格、状态或保护数量未对齐")
        elif live_stop == paper_stop:
            if link["live_stop"] != live_stop:
                link["live_stop"] = live_stop
                self._save()
        elif live_stop != link["live_stop"] and paper_stop == link["live_stop"]:
            await self._send(tv, link, "stop", rows, live_stop, symbol=symbol, side=link["side"],
                             previousQty=qty, positionId=pos["id"], previousStop=paper_stop,
                             stop=live_stop, stopId=str(stop["id"]))
        else:
            self._notice(symbol, "TV 止损与实盘不同，请在实盘控制台确认处理", lot_id=lot["lot_id"],
                         paper_order_id=str(stop["id"]), paper_stop=paper_stop, live_stop=live_stop)

    def start(self):
        if config.TV_PAPER_SYNC_ENABLED and self.account:
            self.context.tasks.start("tv_paper_sync", self.run)

    async def run(self):
        if not self.account:
            return
        try:
            self._load()
        except (OSError, ValueError, TypeError):
            self.state = "TV 模拟同步账本无法读取"
            logger.exception("TV 模拟同步账本无法读取")
            return
        while True:
            try:
                async with self.connect() as tv:
                    await tv._send_cdp("Runtime.addBinding", {"name": "__autotradePaperChanged"})
                    try:
                        while True:
                            await self._call(tv, "attach")
                            await self.reconcile(tv)
                            try:
                                await asyncio.wait_for(tv.binding_events.get(), timeout=5)
                            except TimeoutError:
                                pass
                    finally:
                        try:
                            await asyncio.wait_for(self._call(tv, "detach"), timeout=1)
                        except Exception:
                            pass
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self.notices = []
                state = f"TV 模拟同步暂停：{error}"
                if self.state != state:
                    logger.warning("%s", state)
                self.state = state
                await asyncio.sleep(5)
