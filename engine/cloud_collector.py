# -*- coding: utf-8 -*-
"""
云端数据采集器 (TradingView Cloud Data Collector)
=================================================

通过 subprocess 调用 Node 云端数据服务（service/tv_cloud.js，
基于开源项目 TradingView-API），从 TradingView 云端直接获取
K线 + 指标 + 信号数据，无需 TV 界面显示。

数据流：
    Python asyncio subprocess
      -> node service/tv_cloud.js --symbols ...
        -> TradingView 云端（登录态 WS）
      <- JSON（bars / study periods / graphic 信号）
    -> 解析为 StockSignalData 快照

注意：
- 串行采集，单标的约 5~8s；几十只股票的股票池一轮需要数分钟，
  适合分钟级轮询；秒级高频场景需后续改为常驻 daemon 模式（规划中）。
- 凭据在 service/creds.json（sessionid cookie），过期时需重新提取
  （可通过 CDP 从登录的桌面客户端刷新，见 controller/tv_cdp.py）。
"""

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Dict, List, Optional

from engine.strategy import StockSignalData

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SERVICE_DIR = PROJECT_ROOT / "service"
TV_CLOUD_JS = SERVICE_DIR / "tv_cloud.js"

# study periods 中代表"无穷大/无值"的哨兵
_NO_VALUE = 1e100


def _supertrend_direction(periods: list) -> int:
    """从 SuperTrend 最新 period 解析方向：1 多头 / -1 空头 / 0 未知。"""
    if not periods:
        return 0
    latest = periods[-1]
    up = latest.get("Up_Trend", _NO_VALUE)
    down = latest.get("Down_Trend", _NO_VALUE)
    if up is not None and up < _NO_VALUE:
        return 1
    if down is not None and down < _NO_VALUE:
        return -1
    return 0


def _attack_zone_flag(periods: list) -> Optional[float]:
    """5M 进攻区域方向过滤值（Bar_Color：1 允许多 / 0 不允许）。"""
    if not periods:
        return None
    return periods[-1].get("Bar_Color")


def _supertrend_line(periods: list) -> Optional[float]:
    """SuperTrend 线值（天然追踪止损线）：多头取 Up_Trend，空头取 Down_Trend。"""
    if not periods:
        return None
    latest = periods[-1]
    up = latest.get("Up_Trend", _NO_VALUE)
    down = latest.get("Down_Trend", _NO_VALUE)
    if up is not None and up < _NO_VALUE:
        return up
    if down is not None and down < _NO_VALUE:
        return down
    return None


def _atr_from_bars(bars: list, length: int = 14) -> float:
    """从 K 线序列算 ATR（简单平均），用于初始止损距离与以损定量。
    注意 tv_cloud.js 的 bar 字段是 max/min（不是 high/low）。"""
    if len(bars) < length + 1:
        return 0.0
    trs = []
    for i in range(-length, 0):
        h = float(bars[i].get("max", bars[i].get("high", 0)) or 0)
        l = float(bars[i].get("min", bars[i].get("low", 0)) or 0)
        pc = float(bars[i - 1].get("close", 0))
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return sum(trs) / len(trs) if trs else 0.0


def _field(periods: list, name: str) -> Optional[float]:
    """取最新 period 的某个字段值；无效值（1e100/None）返回 None。"""
    if not periods:
        return None
    v = periods[-1].get(name, _NO_VALUE)
    if v is None or v >= _NO_VALUE:
        return None
    return float(v)


class CloudDataCollector:
    """
    云端数据采集器，接口与 MockDataCollector/TradingViewDataCollector 一致：
    ``async with CloudDataCollector(watchlist) as c: snapshot = await c.collect()``
    """

    def __init__(self, watchlist: List[str], timeframe: str = "5",
                 bars: int = 300, session: str = "extended",
                 per_symbol_timeout: int = 30):
        self.watchlist = watchlist
        self.timeframe = timeframe
        self.bars = bars
        self.session = session
        self.per_symbol_timeout = per_symbol_timeout

    async def __aenter__(self) -> "CloudDataCollector":
        if not TV_CLOUD_JS.exists():
            raise RuntimeError(f"云端服务不存在: {TV_CLOUD_JS}")
        if not (SERVICE_DIR / "creds.json").exists():
            raise RuntimeError(
                "缺少 service/creds.json（TradingView sessionid 凭据）")
        return self

    async def __aexit__(self, *exc) -> None:
        return None

    async def collect(self) -> Dict[str, StockSignalData]:
        """对全股票池执行一轮云端采集，返回 symbol -> StockSignalData。"""
        # 整体超时 = 单标的超时 * 标的数 + 余量
        overall_timeout = self.per_symbol_timeout * len(self.watchlist) + 60
        cmd = [
            "node", str(TV_CLOUD_JS),
            "--symbols", ",".join(self.watchlist),
            "--timeframe", self.timeframe,
            "--range", str(self.bars),
            "--session", self.session,
        ]
        t0 = time.monotonic()
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(SERVICE_DIR),
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=overall_timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise RuntimeError(f"云端采集超时（>{overall_timeout}s）")

        if stderr:
            for line in stderr.decode("utf-8", "replace").splitlines():
                logger.info("[tv_cloud] %s", line)
        if proc.returncode != 0:
            raise RuntimeError(f"tv_cloud.js 退出码 {proc.returncode}")

        payload = json.loads(stdout.decode("utf-8"))
        snapshot = self._to_signals(payload)
        logger.info("云端采集完成: %d 标的, 耗时 %.1fs",
                    len(snapshot), time.monotonic() - t0)
        return snapshot

    # ------------------------------------------------------------------
    # JSON -> StockSignalData 映射
    # ------------------------------------------------------------------

    def _to_signals(self, payload: dict) -> Dict[str, StockSignalData]:
        now = time.time()
        snapshot: Dict[str, StockSignalData] = {}
        for symbol, data in payload.get("symbols", {}).items():
            bars = data.get("bars") or []
            studies = data.get("studies") or {}

            latest_bar = bars[-1] if bars else {}
            st_periods = (studies.get("supertrend") or {}).get("periods") or []
            supertrend = _supertrend_direction(st_periods)
            az_flag = _attack_zone_flag(
                (studies.get("attack_zone") or {}).get("periods") or [])
            ma_periods = (studies.get("fast_slow_ma") or {}).get("periods") or []
            ma_latest = ma_periods[-1] if ma_periods else {}
            # 1M SuperTrend(10,2) 线值：跟踪止损（TRAIL 单）的初始触发价
            st1m_periods = (studies.get("supertrend_1m") or {}).get("periods") or []
            st_1m_line = _supertrend_line(st1m_periods)

            # st_channel_www（SuperTrend Channel）：ST 止损线 + 通道边沿
            stc_periods = (studies.get("st_channel_www") or {}).get("periods") or []
            # EMA(7)：EMA 限价单价格线
            ema7_periods = (studies.get("ema7") or {}).get("periods") or []

            # 进攻区域图形（信号数据）：统计 box/line 数量
            az_graphic = (studies.get("attack_zone") or {}).get("graphic") or {}
            az_boxes = len(az_graphic.get("boxes") or [])
            az_lines = len(az_graphic.get("lines") or [])

            snapshot[symbol] = StockSignalData(
                symbol=symbol,
                supertrend=supertrend,
                # 突破拒绝得分暂无对应指标（"超级信号"脚本云端不可得，
                # 详见项目文档）；当前置 0，策略阈值需相应调整。
                rejection_score=0.0,
                atr=_atr_from_bars(bars),
                close=float(latest_bar.get("close", 0.0)),
                volume=float(latest_bar.get("volume", 0.0)),
                timestamp=float(latest_bar.get("time", now)),
                extra={
                    "attack_zone_flag": az_flag,
                    "attack_zone_boxes": az_boxes,
                    "attack_zone_lines": az_lines,
                    "ma_fast": ma_latest.get("MA_1"),
                    "ma_slow": ma_latest.get("MA_2"),
                    # SuperTrend 线值：追踪止损棘轮的驱动源
                    "supertrend_line": _supertrend_line(st_periods),
                    # 1M ST(10,2) 线值：TRAIL 单初始触发价
                    "st_1m_line": st_1m_line,
                    # st_channel_www：ST 止损线（多头 Up / 空头 Down）
                    "st_up_trend": _field(stc_periods, "Up_Trend"),
                    "st_down_trend": _field(stc_periods, "Down_Trend"),
                    # st_channel_www：通道边沿（多头 ST+ATR / 空头 ST-ATR）
                    "st_up_channel": _field(stc_periods, "Uptrend_Channel"),
                    "st_down_channel": _field(stc_periods, "Downtrend_Channel"),
                    "st_body_middle": _field(stc_periods, "Body_Middle"),
                    # EMA(7)：EMA 限价单价格线
                    "ema7": _field(ema7_periods, "EMA"),
                },
            )
        return snapshot
