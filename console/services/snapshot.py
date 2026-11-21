"""snapshot application functions and owned state. No console.app dependency."""

import asyncio
import time
from console.services.chart_entries import entry_markers
from hmac import compare_digest

from fastapi import WebSocket, WebSocketDisconnect

import config
from console.shared import logger
from execution.copy_channel import copy_channel_token
from execution.entry_admission import TD_THRESHOLD


class SnapshotService:
    def __init__(self, context):
        self.context = context

    async def build_snapshot(self) -> dict:
        status = await self.context.accounts.get_status_cached()
        open_symbols = len(
            {l["symbol"] for l in self.context.order_mgr.list_lots(active_only=True)}
        )
        external = getattr(self.context, "external_guard_gateway", None)
        verified_accounts = (external._guard_verified_accounts if external and external._connected
                             and external._guard_verified_connection is not None else ())
        return {
            "type": "snapshot",
            "ts": time.time(),
            "market_frame_id": self.context.collector.market_frame_id,
            "ipo_openings": self.context.ipo_alerts.snapshot(self.context.collector,time.time()),
            "entry_candidates": self.context.research.entry_candidates(),
            "status": {
                "execution_enabled": bool(self.context.gateway.enabled),
                "connected": bool(self.context.gateway._connected),
                "broker_route": {
                    "account": getattr(self.context.gateway, "account_id", None),
                    "fa_group": getattr(self.context.gateway, "fa_group", None),
                },
                "dry_run": not bool(self.context.gateway.enabled),
                "external_order_guard": {
                    "enabled": external is not None,
                    "connected": bool(external and external._connected),
                    "client_id": external.client_id if external else None,
                    "submit_client_id": self.context.gateway.client_id if external else None,
                    "fa_group": external.fa_group if external else None,
                    "exempt_tickers": list(config.EXTERNAL_ORDER_GUARD_EXEMPT_TICKERS),
                    "confirmed_member_count": len(verified_accounts),
                    "member_suffixes": sorted(account[-4:] for account in verified_accounts),
                },
                "equity": status["equity"],
                "daily_pnl": status["daily_pnl"],
                "halted": self.context.risk_gate.is_halted(),
                "recovery": self.context.order_mgr.recovery_status(),
                "open_symbols": open_symbols,
                "total_risk_usd": self.context.order_mgr.risk_exposure()["total"],
                "max_total_risk_usd": config.RISK_MAX_TOTAL_RISK_USD,
                "max_direction_risk_usd": config.RISK_MAX_DIRECTION_RISK_USD,
                "long_risk_usd": self.context.order_mgr.risk_exposure()["long"],
                "short_risk_usd": self.context.order_mgr.risk_exposure()["short"],
                "total_position_pct": self.context.accounts._total_position_pct(
                    status["equity"]
                ),
                "max_order_qty": config.RISK_MAX_ORDER_QTY,
                "max_symbol_position_pct": config.RISK_MAX_SYMBOL_POSITION_PCT,
                "risk_per_trade_usd": config.RISK_PER_TRADE_USD,
                "allow_short": config.ALLOW_SHORT,
                "manual_market_st_interval_seconds": 60,
                "tv_view_same_window": True,
                "tv_single_chart_cell": 6,
                "tv_grid_same_window": True,
                "tv_grid_cells": 8,
                "tv_grid_layouts": {'candidates': 8, 'focus': 6},
                "candidate_entry_required": getattr(self.context.order_mgr,'entry_admission',None) is not None,
                "candidate_td_threshold": TD_THRESHOLD,
                "candidate_ema9_filter": {"period": 9, "interval_seconds": 300, "price_source": "bar.close", "ema_source": "ts.ema_9"},
                "manual_stop_modes": ['atr_2','vwap_sigma'],
                "candidate_order_policy_version": 1,
                "candidate_protection_live_version": 1,
                "candidate_order_protection": self.context.settings._candidate_order_protection(),
                "tv_entry_markers": True,
                "fvg_chase_max_ratio": config.FVG_CHASE_MAX_ATR_RATIO,
                "stop_atr_mult": self.context.settings._stop_atr_mult(),
                "pyramid_auto_breakeven": self.context.settings._pyramid_auto_breakeven(),
                "pyramid_breakeven_r": self.context.settings._pyramid_breakeven_r(),
                "pyramid_add_unlock_r": self.context.settings._pyramid_add_unlock_r(),
                "screen_premarket_filter": self.context.settings._screen_premarket_filter(),
                "screen_opening_range_filter": self.context.settings._screen_opening_range_filter(),
                "fvg_premarket_filter": self.context.settings._fvg_premarket_filter(),
                "news": self.context.news._news_status(),
                "imessage_recipient": str(
                    self.context.settings._settings.get("imessage_recipient") or ""
                ),
                "market_age": None
                if self.context.collector._last_content_update == 0
                else round(self.context.collector.age(), 1),
                "market_transport_age": self.context.collector.data_health()[
                    "transport_age"
                ],
                "market_bar_age": self.context.collector.data_health()["bar_age"],
                "market_stale": self.context.collector.data_health()["stale"],
                "market_stale_reason": self.context.collector.data_health()["reason"],
                "market_running": self.context.collector.running,
                "market_session": self.context.collector.market_session_status(),
                "data_time": self.context.collector.latest_bar_time,
                "server_time": time.time(),
                "ibkr_time": self.context.gateway.ibkr_time,
                "bar_update_delay": self.context.collector.bar_update_delay,
                "tv_study": self.context.collector.study_health(),
                "tv_paper_sync": self.context.tv_paper_sync.status(),
            },
            "market": {
                symbol: {key: value for key, value in data.items() if key != "opening_breakout_history"}
                for symbol, data in self.context.collector.latest().items()
            },
            "vwap_distribution": self.context.collector.vwap_distribution,
            "vwap_opening_signals": self.context.opening_vwap.display(time.time()),
            "positions": self.context.order_mgr.aggregate(),
            "lots": self.context.order_mgr.list_lots(),
            "tv_entry_markers": entry_markers(self.context.order_mgr.list_lots(), now=time.time()),
            "logs": list(self.context.events._log_buffer),
            "screen": self.context.signals.compute_screen(),
            "momentum": self.context.signals.compute_momentum(),
            "scan_alerts": sorted(
                self.context.monitoring._scan_alerts.values(),
                key=lambda x: x.get("scanned_at", 0),
                reverse=True,
            ),
            "stop_alerts": sorted(
                self.context.monitoring._stop_alerts.values(),
                key=lambda x: x.get("checked_at", 0),
                reverse=True,
            ),
            "external_alerts": list(self.context.monitoring._external_alerts),
            "external_entry_results": self.context.order_mgr.external_entry_results(),
            "slow_break_alerts": sorted(
                self.context.monitoring._slow_break_alerts.values(),
                key=lambda x: x.get("alerted_at", 0),
                reverse=True,
            ),
            "auto_close_notices": sorted(
                self.context.monitoring._auto_close_notices.values(),
                key=lambda x: x.get("closed_at", 0),
                reverse=True,
            ),
            "auto_notices": sorted(
                self.context.automatic._auto_notices.values(),
                key=lambda x: (x.get("ts", 0), x.get("id", "")),
                reverse=True,
            ),
            "fvg_reverse_alerts": sorted(
                self.context.monitoring._fvg_reverse_alerts.values(),
                key=lambda x: x.get("alerted_at", 0),
                reverse=True,
            ),
            "fvg_reverse_notices": sorted(
                self.context.monitoring._fvg_reverse_notices.values(),
                key=lambda x: x.get("closed_at", 0),
                reverse=True,
            ),
            "shadow": {
                "stats": self.context.shadow_tracker.stats(),
                "events": self.context.shadow_tracker.events(100),
            },
            "fvg_monitor": self.context.signals._fvg_monitor_snapshot(),
            "breakout_signals": list(self.context.signals._breakout_signals),
            "opening_breakouts": self.context.signals.opening_breakout_display(time.time()),
            "opening_breakout_coverage": self.context.signals.opening_breakout_coverage(
                config.WATCHLIST, time.time()
            ),
            "alerts": self.context.notifier.active_alerts(),
            "auto": {
                "enabled": self.context.automatic._auto_enabled,
                "strategy": self.context.automatic._auto_strategy_name,
                "models": self.context.auto_service.registry.describe(),
                "instances": self.context.automatic._auto_instances(),
                "proposals": [],
            },
            "clients": self.context.clients._clients_snapshot(),
            "copy": self.context.copy._copy_snapshot(),
        }

    async def _broadcast_loop(self) -> None:
        """每 1s 广播一次快照（含 TV 实时数据）。"""
        while True:
            try:
                self.context.accounts._refresh_marks_from_tv()
                snapshot = await self.context.snapshot.build_snapshot()
                dead = []
                for ws in list(self.context.snapshot._ws_clients):
                    try:
                        if ws.url.path == "/ws/copy":
                            await ws.send_json(self._copy_frame(snapshot))
                        else:
                            await ws.send_json(snapshot)
                    except Exception:
                        dead.append(ws)
                for ws in dead:
                    self.context.snapshot._ws_clients.discard(ws)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.exception("广播循环异常: %s", e)
            await asyncio.sleep(1.0)

    async def api_status(self) -> dict:
        return (await self.context.snapshot.build_snapshot())["status"]

    @staticmethod
    def _copy_frame(snapshot):
        status = snapshot["status"]
        return {"type": "snapshot", "ts": snapshot["ts"], "lots": snapshot["lots"],
                "status": {"equity": status.get("equity"), "dry_run": status.get("dry_run")}}

    async def ws_copy_endpoint(self, ws: WebSocket) -> None:
        token = copy_channel_token()
        supplied = ws.headers.get("authorization", "").encode("utf-8")
        if not token or not compare_digest(supplied, f"Bearer {token}".encode()):
            await ws.close(code=1008)
            return
        await self.ws_endpoint(ws, copy_only=True)

    async def ws_endpoint(self, ws: WebSocket, *, copy_only=False) -> None:
        await ws.accept()
        self.context.snapshot._ws_clients.add(ws)
        try:
            snapshot = await self.context.snapshot.build_snapshot()
            await ws.send_json(self._copy_frame(snapshot) if copy_only else snapshot)
            while True:
                await ws.receive_text()
        except WebSocketDisconnect:
            pass
        except Exception:
            pass
        finally:
            self.context.snapshot._ws_clients.discard(ws)
