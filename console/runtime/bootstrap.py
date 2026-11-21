"""Explicit composition root. Tests pass temporary paths; import does not read production state."""

from console.services.night_test import NightTestService
from console.services.tv_paper_sync import TvPaperSync, configured_paper_account
from console.services.ipo_alerts import IpoAlerts
from console.services.strategy_shadow import StrategyShadow
from console.services.auto import AutoService
from engine.backtest_session import BacktestSession
from engine.breakout_tracker import BreakoutTracker
from fastapi import HTTPException
from execution.ibkr import IBKRGateway
from engine.brokers import LiveBroker
from engine.live_news import LiveNewsService
from engine.notifier import Notifier
from typing import Optional
from execution.order_lot import OrderManager
from pathlib import Path
from engine.pyramid_engine import PyramidEngine
from engine.realtime_collector import RealtimeCollector
from engine.live_audit import LiveMarketAudit
from execution.risk import RiskConfig
from execution.risk import RiskGate
from typing import Set
from engine.shadow_tracker import ShadowTracker
from console.runtime.strategy import StrategyRuntime
from fastapi import WebSocket
from zoneinfo import ZoneInfo as _ZoneInfo
import asyncio
import collections
import config
import logging
import os
import re
import sys
import time
from console.shared import CONSOLE_FILE
from dataclasses import dataclass
from console.runtime.tasks import TaskRegistry
from console.services.accounts import AccountsService
from console.services.automatic import AutomaticService
from console.services.clients import ClientsService
from console.services.copy import CopyService
from console.services.desktop import DesktopService
from console.services.events import EventsService
from console.services.lifecycle import LifecycleService
from console.services.monitoring import MonitoringService
from console.services.news import NewsService
from console.services.precheck import PrecheckService
from console.services.reports import ReportsService
from console.services.research import ResearchService
from console.services.settings import SettingsService
from console.services.signals import SignalsService
from console.services.opening_vwap import OpeningVwapService
from console.services.snapshot import SnapshotService
from console.services.system import SystemService
from console.services.trading import TradingService
from console.services.copy import CopySupervisor
from console.services.events import _BufferHandler


@dataclass(frozen=True)
class RuntimePaths:
    data_dir: Path = CONSOLE_FILE.parent.parent / "logs"


class ConsoleServices:
    def __init__(self, paths=None):
        self.paths = paths or RuntimePaths()
        self.tasks = TaskRegistry()
        self.night_test = NightTestService(self.paths.data_dir, self.tasks)
        self.tv_paper_sync = TvPaperSync(self, configured_paper_account(self.paths.data_dir))
        self.app = None
        self.execution_writer_acquired = False
        self.config = config
        self.asyncio = asyncio
        self.os = os
        self.sys = sys
        self.time = time
        self.Path = Path
        self.HTTPException = HTTPException
        self.accounts = AccountsService(self)
        self.automatic = AutomaticService(self)
        self.clients = ClientsService(self)
        self.copy = CopyService(self)
        self.desktop = DesktopService(self)
        self.events = EventsService(self)
        self.ipo_alerts = IpoAlerts(self, config.IPO_OPENING_WATCHES)
        self.lifecycle = LifecycleService(self)
        self.monitoring = MonitoringService(self)
        self.news = NewsService(self)
        self.precheck = PrecheckService(self)
        self.reports = ReportsService(self)
        self.research = ResearchService(self)
        self.settings = SettingsService(self)
        self.signals = SignalsService(self)
        self.opening_vwap = OpeningVwapService(self)
        self.snapshot = SnapshotService(self)
        self.system = SystemService(self)
        self.trading = TradingService(self)
        self.events._log_buffer: "collections.deque" = collections.deque(maxlen=300)
        self.events._buffer_handler = _BufferHandler(self)
        self.system.STATIC_DIR = Path(str(CONSOLE_FILE)).resolve().parent / "static"
        self.clients._LOCAL_ADDRS = ("127.0.0.1", "::1")
        self.clients._CLIENT_NAME_RE = re.compile("^[\\w\\-]{1,20}$")
        self.clients._CLIENTS_PATH = self.paths.data_dir / "clients.json"
        self.clients._CLIENT_TOKEN_TTL_SECONDS = config.CLIENT_TOKEN_TTL_HOURS * 3600
        self.clients._clients: dict = self.clients._load_clients()
        self.settings._SETTINGS_PATH = self.paths.data_dir / "settings.json"
        self.settings._STOP_ATR_MULT_MIN, self.settings._STOP_ATR_MULT_MAX = (0.2, 5.0)
        self.settings._settings: dict = self.settings._load_settings()
        self.settings._PYRAMID_R_MIN, self.settings._PYRAMID_R_MAX = (0.1, 20.0)
        self.copy._COPY_SERVERS_PATH = self.paths.data_dir / "copy_servers.json"
        self.copy._COPY_TRADER_PY = (
            Path(str(CONSOLE_FILE)).resolve().parent.parent / "copy_trader.py"
        )
        self.copy._COPY_SERVER_NAME_RE = re.compile("^[\\w\\-]{1,10}$")
        self.copy._COPY_STATUS_FIELDS = (
            "connected",
            "equity",
            "synced",
            "failed",
            "halted",
            "drawdown_pct",
            "last_sync_ts",
            "last_error",
            "orders",
            "failures",
        )
        self.copy._COPY_CLIENT_ID_MIN = 13
        self.copy._COPY_CLIENT_ID_RESERVED = (2, 3, 12, 46)
        self.copy_supervisor: CopySupervisor = CopySupervisor(self)
        self.gateway: IBKRGateway = IBKRGateway.from_config()
        self.external_guard_gateway = None
        if self.gateway.enabled and config.EXTERNAL_MANUAL_GUARD_ENABLED:
            self.external_guard_gateway = IBKRGateway(
                self.gateway.host, self.gateway.port, 0, self.gateway.account_id,
                self.gateway.fa_group, enabled=True,
            )
            self.external_guard_gateway.external_order_writer = self.gateway
        self.risk_gate: RiskGate = RiskGate(
            RiskConfig(
                per_trade_pct=config.RISK_PER_TRADE_PCT,
                per_trade_usd=config.RISK_PER_TRADE_USD,
                max_order_qty=config.RISK_MAX_ORDER_QTY,
                max_open_positions=config.RISK_MAX_OPEN_POSITIONS,
                max_total_risk_usd=config.RISK_MAX_TOTAL_RISK_USD,
                max_direction_risk_usd=config.RISK_MAX_DIRECTION_RISK_USD,
                daily_loss_limit_usd=config.RISK_DAILY_LOSS_LIMIT_USD,
            ),
            halt_dir=self.paths.data_dir,
        )
        self.order_mgr: OrderManager = OrderManager(
            self.gateway,
            self.risk_gate,
            state_path=self.paths.data_dir / "lots_state.json",
            external_guard_gateway=self.external_guard_gateway,
        )
        self.order_mgr.external_adoption_enabled = not config.EXTERNAL_MANUAL_GUARD_ENABLED
        self.snapshot._ws_clients: Set[WebSocket] = set()
        self.snapshot._broadcast_task: Optional[asyncio.Task] = None
        self.monitoring._eod_task: Optional[asyncio.Task] = None
        self.monitoring._reconcile_task: Optional[asyncio.Task] = None
        self.trading._midpoint_reprice_task: Optional[asyncio.Task] = None
        self.trading._retest_reprice_task: Optional[asyncio.Task] = None
        self.monitoring._external_cleanup_task: Optional[asyncio.Task] = None
        self.trading._midpoint_escalate_attempted: set = set()
        self.market_audit = LiveMarketAudit(self.paths.data_dir / "live_audit.sqlite3")
        self.collector: RealtimeCollector = RealtimeCollector(
            config.WATCHLIST, audit=self.market_audit, market_session=config.TV_MARKET_SESSION,
            default_session_symbols=config.TV_DEFAULT_SESSION_SYMBOLS,
            ipo_watches=config.IPO_OPENING_WATCHES,
            on_ipo_trade=self.ipo_alerts.refresh,
        )
        self.order_mgr.market_health = self.collector.data_health
        self.order_mgr.entry_admission = self.research.check_candidate_entry
        self.order_mgr.candidate_protection_options = self.settings._candidate_order_protection
        self.accounts._equity_cache = {"equity": None, "daily_pnl": None, "ts": 0.0}
        self.order_mgr.position_equity = lambda: self.accounts._equity_cache.get("equity")
        self.monitoring._scan_alerts: dict = {}
        self.monitoring._scan_task: Optional[asyncio.Task] = None
        self.monitoring._stop_alerts: dict = {}
        self.monitoring._stop_guard_task: Optional[asyncio.Task] = None
        self.monitoring._slow_break_alerts: dict = {}
        self.monitoring._slow_break_task: Optional[asyncio.Task] = None
        self.monitoring._auto_breakeven_task: Optional[asyncio.Task] = None
        self.monitoring._auto_close_notices: dict = {}
        self.monitoring._fvg_reverse_alerts: dict = {}
        self.monitoring._fvg_reverse_notices: dict = {}
        self.monitoring._fvg_reverse_acted: dict = {}
        self.monitoring._fvg_reverse_task: Optional[asyncio.Task] = None
        self.shadow_tracker: ShadowTracker = ShadowTracker(db_path=self.paths.data_dir / "research.db")
        self.strategy_shadow = StrategyShadow(
            self.paths.data_dir / "strategy_shadow", config.WATCHLIST, self.settings,
        )
        self.signals._shadow_task: Optional[asyncio.Task] = None
        self.signals._shadow_proposal_recorded: set = set()
        self.automatic._AUTO_NOTICE_EVENTS = {
            "pyramid_order_placed",
            "pyramid_entry_filled",
            "pyramid_order_failed",
            "pyramid_close_requested",
            "pyramid_flat",
            "pyramid_trend_reverse_alert",
            "auto_trend_reverse_alert",
        }
        self.automatic._auto_notices: dict = {}
        self.auto_engine: PyramidEngine = PyramidEngine(
            LiveBroker(self.order_mgr),
            emit=self.automatic._emit_auto,
            max_instances=config.AUTO_MAX_INSTANCES,
            breakeven_enabled=self.settings._pyramid_auto_breakeven(),
            breakeven_r=self.settings._pyramid_breakeven_r(),
            add_unlock_r=self.settings._pyramid_add_unlock_r(),
        )
        self.auto_service = AutoService(
            LiveBroker(self.order_mgr),
            self.collector,
            self.order_mgr,
            default_engine_provider=lambda: self.auto_engine,
            watchlist=config.WATCHLIST,
            default_risk=config.RISK_PER_TRADE_USD,
            allow_short=config.ALLOW_SHORT,
            max_instances=config.AUTO_MAX_INSTANCES,
            emit=lambda *a, **k: self.automatic._emit_auto(*a, **k),
            record_event=lambda *a, **k: self.events.record_event(*a, **k),
        )
        self.strategy_runtime = StrategyRuntime(self.auto_service, require_opening_range=True)
        self.automatic._auto_enabled: bool = False
        self.automatic._auto_task: Optional[asyncio.Task] = None
        self.automatic._auto_last_bar: dict = {}
        self.automatic._auto_strategy_name: str = ""
        self.signals.FVG_EVENT_TTL_SECONDS = 4 * 3600
        self.signals._fvg_events: "collections.deque" = collections.deque()
        self.signals._fvg_retests: "collections.deque" = collections.deque()
        self.signals._fvg_last_bar: dict = {}
        self.signals._fvg_pending: dict = {}
        self.signals._fvg_warnings: "collections.deque" = collections.deque()
        self.signals._fvg_warned: dict = {}
        self.signals._fvg_task: Optional[asyncio.Task] = None
        self.breakout_tracker: BreakoutTracker = BreakoutTracker()
        self.signals._breakout_last_bar: dict = {}
        self.signals._breakout_pending: dict = {}
        self.signals._breakout_signals: "collections.deque" = collections.deque()
        self.signals._breakout_injected: dict = {}
        self.signals._breakout_task: Optional[asyncio.Task] = None
        self.monitoring._naked_position_task: Optional[asyncio.Task] = None
        self.monitoring._health_watch_task: Optional[asyncio.Task] = None
        self.desktop._tv_check_task: Optional[asyncio.Task] = None
        self.reports._report_task: Optional[asyncio.Task] = None
        self.reports._report_loop_task: Optional[asyncio.Task] = None
        self.precheck._precheck_task: Optional[asyncio.Task] = None
        self.lifecycle._startup_task: Optional[asyncio.Task] = None
        self.lifecycle._startup_state: dict = {
            "started_at": time.time(),
            "core_ready": False,
            "browser_ready": False,
            "browser_error": "",
            "split_ready": False,
            "split_error": "",
            "ready": False,
        }
        self.system._system_stopping = False
        self.monitoring._external_cleanup_inflight: dict = {}
        self.monitoring._external_alerts: list = []
        self.monitoring._external_alert_last: dict = {}
        self.reports._REPORTS_DIR = self.paths.data_dir
        self.precheck._PRECHECK_DIR = self.paths.data_dir
        self.notifier: Notifier = Notifier(
            record_event=lambda t, **d: self.events.record_event(t, **d),
            imessage_recipient=lambda: str(
                self.settings._settings.get("imessage_recipient") or ""
            ),
        )
        self.system._TWS_PROCESS_PATTERN = "/Trader Workstation/Trader Workstation.app/Contents/MacOS/JavaApplicationStub"
        self.desktop._TV_CHART_URL = "https://cn.tradingview.com/chart/I2KwX1s0/"
        self.desktop._TV_FOCUS_CHART_URL = "https://cn.tradingview.com/chart/IWzSaxF7/"
        self.desktop._TV_CHROME_PROFILE = str(Path.home() / ".tv-chrome-profile")
        self.desktop._CONSOLE_URL = os.getenv("CONSOLE_URL", "http://127.0.0.1:8000/")
        self.desktop._CONSOLE_AUTO_OPEN_BROWSER = (
            os.getenv("CONSOLE_AUTO_OPEN_BROWSER", "1") != "0"
        )
        self.desktop._CONSOLE_AUTO_SPLIT_BROWSER = (
            os.getenv("CONSOLE_AUTO_SPLIT_BROWSER", "1") != "0"
        )
        self.desktop._CONSOLE_AUTO_ARRANGE_WINDOWS = os.getenv('CONSOLE_AUTO_ARRANGE_WINDOWS', '1') != '0'
        self.research._bt_session: Optional[BacktestSession] = None
        self.news._ZI_ET = _ZoneInfo("America/New_York")
        self.news._live_news = LiveNewsService()
        self.news._news_task: Optional[asyncio.Task] = None
        self.news._news_cache: dict = {}
        self.news._news_last_manual: float = 0.0
        self.news.NEWS_REFRESH_COOLDOWN_S = 600
        self.news.NEWS_AUTO_HM = 7 * 60 + 30
        self.reports._REPORTS_DIR = self.paths.data_dir / "reports"
        self.reports._REPORT_DATE_RE = re.compile("^\\d{8}$")
        self.reports.REPORT_GEN_COOLDOWN_S = 600
        self.reports._report_task: Optional[asyncio.Task] = None
        self.reports._report_state = {
            "running": False,
            "started_at": 0.0,
            "last_result": None,
            "last_error": None,
        }
        self.reports._report_last_done: float = 0.0
        self.reports._REPORT_SOURCES_PATH = self.paths.data_dir / "report_sources.json"
        self.events._buffer_handler.setLevel(logging.INFO)
        logging.getLogger().addHandler(self.events._buffer_handler)

    def close_log_handler(self):
        logging.getLogger().removeHandler(self.events._buffer_handler)
        self.events._buffer_handler.close()


def build_services(paths=None):
    return ConsoleServices(paths)
