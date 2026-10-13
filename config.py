# -*- coding: utf-8 -*-
"""
全局配置中心
============

集中管理所有可调参数：
- CDP 调试端口（控制模块）
- TradingView WebSocket 连接参数（计算模块）
- 监控股票池 / 八宫格布局 / 轮询周期
- Pine Script 自定义指标注册表
"""

from dataclasses import dataclass, field
from typing import Dict, List

# ---------------------------------------------------------------------------
# 控制模块 (UI Grid Controller) 配置
# ---------------------------------------------------------------------------

# Mac 端 TradingView 客户端以 --remote-debugging-port=9222 启动后，
# CDP 的 HTTP 发现端点（/json 列表页）。
# 实测注意：必须用 "localhost"，用 127.0.0.1 会被 DevTools Host 校验
# 拒绝（Python urllib 场景）；且启动 TV 前必须确保无存活实例，
# 否则 Electron 单实例机制会丢弃调试端口参数。
CDP_HTTP_ENDPOINT = "http://localhost:9222"

# CDP 单次请求/求值超时（秒）
CDP_TIMEOUT = 10.0

# ---------------------------------------------------------------------------
# 计算模块 (Data & Strategy Engine) 配置
# ---------------------------------------------------------------------------

# TradingView 私有 WebSocket 协议端点（tradingview-ws 方案）
# 实测校准：必须带 ?&type=chart 参数，否则 create_study 一律被服务端
# 拒绝（"Study not allowed in this connection"，2023-03 后的服务端变更）
TV_WS_URL = "wss://data.tradingview.com/socket.io/websocket?&type=chart"
TV_WS_ORIGIN = "https://www.tradingview.com"

# 未登录游客令牌；如需订阅私有指标（Script$USER;...），
# 请替换为浏览器登录后 Cookie 中的 sessionid 对应 token
TV_AUTH_TOKEN = "unauthorized_user_token"

# 单次采集快照的整体超时（秒）
COLLECT_TIMEOUT = 15.0

# ---------------------------------------------------------------------------
# 股票池与网格布局
# ---------------------------------------------------------------------------

# 后台监控的股票池 = 用户的 TradingView 自选列表（2026-10-09 从图表 CDP 同步）
# 同步脚本：service/sync_watchlist.py（从 TV 图表右侧自选表读取并写回本列表）
WATCHLIST: List[str] = [
    "BLACKBULL:WTI", "NASDAQ:PANW", "NASDAQ:APP", "NYSE:CRCL",
    "NASDAQ:HOOD", "NASDAQ:QCOM", "NASDAQ:COIN", "NASDAQ:MSTR",
    "NASDAQ:AVGO", "NASDAQ:SPCX", "NASDAQ:MU", "NASDAQ:SNDK",
    "NASDAQ:SKHY", "NASDAQ:MRVL", "NASDAQ:SMCI", "NASDAQ:NBIS",
    "NASDAQ:ARM", "NYSE:BE", "NASDAQ:WMT", "NASDAQ:NFLX",
    "NASDAQ:ADBE", "NYSE:UBER", "NASDAQ:PLTR", "NASDAQ:AAPL",
    "NASDAQ:MRNA", "AMEX:SPY", "CAPITALCOM:SPX500", "NASDAQ:GOOGL",
    "NYSE:BA", "NASDAQ:META", "NASDAQ:MSFT", "NASDAQ:QQQ",
    "NYSE:DELL", "NASDAQ:AMZN", "CAPITALCOM:NAS100", "NYSE:BABA",
    "NASDAQ:TSLA", "NASDAQ:NVDA", "NASDAQ:SMH", "NYSE:TSM",
    "NASDAQ:AMD", "NYSE:ORCL", "NASDAQ:INTC", "NASDAQ:CRWV",
]

# 八宫格布局参数（对齐 Premium 套餐每标签页 8 图上限）
# TV 8 图布局为 4 列 x 2 行
GRID_ROWS = 2
GRID_COLS = 4
TOP_N = GRID_ROWS * GRID_COLS  # = 8

# 主轮询周期（秒）：5 秒轮询；如按 K 线收盘触发，可改为 60 并对齐 bar close
# 注意：cloud 数据源串行采集单标的约 5~8s，股票池较大时一轮需数分钟，
# 应把 POLL_INTERVAL 调大（如 300）或减少单轮标的数
POLL_INTERVAL = 5.0

# ---------------------------------------------------------------------------
# 策略参数（传给 engine.strategy.StrategyConfig）
# ---------------------------------------------------------------------------
# 当前云端可用信号：supertrend 方向、attack_zone_flag(0/1)、MA 快/慢线。
# "突破拒绝得分"指标（超级信号脚本）云端不可得（受邀保护脚本源码 404），
# 因此默认不对 rejection_score 设限；后续接入自有打分指标后再收紧。
STRATEGY_REQUIRE_SUPERTREND = 1       # 1=仅多头；-1=仅空头；None=不过滤
STRATEGY_MIN_REJECTION_SCORE = 0.0    # 0.0 = 不过滤
STRATEGY_MIN_ATR = 0.0
STRATEGY_MIN_VOLUME = 0.0
# 额外过滤：要求 5M 进攻区域方向值 == 1（None 表示不过滤）
STRATEGY_REQUIRE_ATTACK_ZONE = None

# ---------------------------------------------------------------------------
# Pine Script 自定义指标注册表
# ---------------------------------------------------------------------------
# 说明：
#   TradingView 私有协议通过 create_study 订阅指标输出。
#   pine_id 形如：
#     - 公开指标:  "PUB;<indicator_id>"
#     - 私有指标:  "USER;<script_id>"（需登录 token）
#     - 内置指标:  "STD;Supertrend" 等
#   version 后缀形如 "@tv-scripting-101!"，留空表示使用最新版本。
#   plot_map 将策略字段名映射到指标返回值中的 plot 索引，
#   需要按你的 Pine Script 中 plot() 声明顺序填写。
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PineIndicatorConfig:
    """单个 Pine 指标的订阅配置。"""

    name: str                       # 人类可读名称，仅用于日志
    pine_id: str                    # 例如 "PUB;xxxxxxxx" / "STD;Supertrend"
    version: str = ""               # 例如 "@tv-scripting-101!"
    inputs: Dict[str, object] = field(default_factory=dict)
    # plot 索引映射：StockSignalData 字段名 -> 指标第 N 个 plot（0 起）
    plot_map: Dict[str, int] = field(default_factory=dict)


# TODO: 替换为你自己的 Pine Script 指标 ID 与 plot 映射
PINE_INDICATORS: List[PineIndicatorConfig] = [
    PineIndicatorConfig(
        name="SuperTrend",
        pine_id="STD;Supertrend",
        inputs={"Factor": 3.0, "ATR Length": 10},
        plot_map={"supertrend": 1},      # plot 1 = 方向（1 多 / -1 空）
    ),
    PineIndicatorConfig(
        name="BreakoutRejectionScore",
        pine_id="PUB;YOUR_INDICATOR_ID",  # <-- 替换为实际指标 ID
        inputs={},
        plot_map={"rejection_score": 0},  # plot 0 = 拒绝得分 0~100
    ),
    PineIndicatorConfig(
        name="ATR",
        pine_id="STD;ATR",
        inputs={"length": 14},
        plot_map={"atr": 0},
    ),
]

# ---------------------------------------------------------------------------
# IBKR 执行层配置（实战经验继承自同级项目 nautilus_ibkr_helloworld）
# ---------------------------------------------------------------------------

import os as _os

IBKR_HOST = "127.0.0.1"
# TWS 实盘=7496 / TWS 模拟=7497 / IB Gateway 实盘=4001
IBKR_PORT = int(_os.environ.get("IBKR_PORT", "7496"))
# client_id 避开 nautilus 项目的 2/3（Data/Exec）和抓取脚本的 46
IBKR_CLIENT_ID = int(_os.environ.get("IBKR_CLIENT_ID", "11"))
# FA 主账号与账户组；faMethod 必须留空——带值会被 IBKR 拒（error 10226/321），
# 留空则使用 FA Group 的默认分配方式（实测生效）
IBKR_ACCOUNT_ID = _os.environ.get("IBKR_ACCOUNT_ID", "F10251881")
IBKR_FA_GROUP = _os.environ.get("IBKR_FA_GROUP", "dt_test")
IBKR_FA_METHOD = ""
# dt_test 账户组成员（下单/资金/盈亏/持仓均只针对此组）。
# 运行时优先从 TWS 的 FA Groups 配置动态查询；此处作为查询失败时的兜底。
IBKR_FA_ACCOUNTS = [
    a.strip() for a in
    _os.environ.get("IBKR_FA_ACCOUNTS", "U11576827,U18082969").split(",")
    if a.strip()
]

# 执行总开关：False = dry-run，批准的提案只记日志（logs/dryrun_orders.jsonl）
# 不发任何真实订单；True 才向 TWS 下单。实盘前请确认风控参数已按账户调整。
EXECUTION_ENABLED = _os.environ.get("EXECUTION_ENABLED", "0") == "1"

# ---------------------------------------------------------------------------
# 风控硬性限额（risk.py 使用；第一笔真实订单前必须就位——验证路径跳过 paper
# 直接实盘小仓位，风控是唯一兜底）
# ---------------------------------------------------------------------------

# 以损定量：每一笔止损的固定损失 = 账户净值 × RISK_PER_TRADE_PCT。
# qty = floor(净值 × 0.1% / |entry - 止损价|)。
# 净值取不到（dry-run 未连 IBKR）时退化用 RISK_PER_TRADE_USD 固定额。
RISK_PER_TRADE_PCT = 0.001        # 0.1% 账户净值
RISK_PER_TRADE_USD = 200.0        # 净值未知时的降级固定风险额
RISK_MAX_ORDER_QTY = 100000       # 单笔最大股数硬顶（仅极端兜底，不再干扰以损定量）
RISK_MAX_OPEN_POSITIONS = 3       # 最大同时持仓数
RISK_DAILY_LOSS_LIMIT_USD = 1000.0  # 日亏损熔断（SOD 净值锚点口径）
ALLOW_SHORT = False               # 是否允许做空（暂时只做多头）
ENTRY_LIMIT_PREMIUM_PCT = 0.5     # 入场用 marketable LIMIT：限价 = 参考价 ±0.5%
                                  # （延迟行情下 IBKR 拒 bare MARKET，error 10349）

# 跟踪止损（TRAIL 括号子单，止损止盈一体）：
#   初始触发价 = 1 分钟 SuperTrend(10,2) 线值（云端采集 st_1m_line）
#   跟踪幅度   = TRAIL_ATR_MULT × ATR(14)（5M，随信号快照采集）
TRAIL_ATR_MULT = 2.0
TP_RR = 0.0                       # 不挂独立止盈单：TRAIL 跟踪止损兼任止盈

# ---------------------------------------------------------------------------
# 运行模式开关
# ---------------------------------------------------------------------------
# 数据源选择：
#   "mock"  - 内置模拟数据源，无需任何外部连接，跑通全流程用
#   "cloud" - TradingView 云端数据服务（service/tv_cloud.js，
#             基于 TradingView-API，后台无界面取 K线/指标/信号，推荐）
#   "ws"    - 手写 TradingView 私有 WS 协议（早期 PoC，仅保留参考）
DATA_SOURCE = "cloud"

# 日志级别
LOG_LEVEL = "INFO"

# 控制器实现选择：
#   "node"   - controller/tv_control.js（基于开源项目 tradingview-mcp，推荐）
#   "python" - controller/tv_cdp.py（纯 Python websockets 实现，备用）
CONTROLLER_IMPL = "node"
