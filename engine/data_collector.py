# -*- coding: utf-8 -*-
"""
数据采集模块 (TradingView Data Collector)
=========================================

两条数据源实现，通过 config.USE_MOCK_DATA 切换：

1. ``TradingViewDataCollector``（真实数据源）
   基于 TradingView 私有 WebSocket 协议（tradingview-ws 方案）：
   - 报价会话 (quote session)：批量订阅几十只股票的实时价格/成交量；
   - 图表会话 (chart session)：对每只股票 resolve_symbol + create_series
     + create_study，直接获取 Pine Script 指标的已计算输出，
     保证"所见即所得"，与图表上看到的指标值完全一致。

   协议要点：
   - 帧格式: ``~m~<length>~m~<json>``，一次 TCP 消息可能携带多帧；
   - 心跳: 服务端发 ``~h~<n>``，客户端必须原样回帧，否则连接被断；
   - 指标数据经 "du" 消息回推，plot 值按 config.PINE_INDICATORS 的
     plot_map 映射到 StockSignalData 字段。

2. ``MockDataCollector``（PoC 模拟源）
   随机游走生成伪信号，用于在没有 TV 连接时跑通
   "采集 -> 策略 -> 八宫格灌入" 全流程。
"""

import asyncio
import json
import logging
import random
import re
import time
import uuid
from typing import Dict, List, Optional

import websockets

import config
from engine.strategy import StockSignalData

logger = logging.getLogger(__name__)

# ~m~<len>~m~ 帧头正则
_FRAME_RE = re.compile(r"~m~(\d+)~m~")


def _pack_frame(method: str, params: list) -> str:
    """按 TV 私有协议打包一帧。"""
    payload = json.dumps({"m": method, "p": params})
    return f"~m~{len(payload)}~m~{payload}"


def _unpack_frames(buffer: str):
    """
    从接收缓冲区中拆出完整帧，返回 (frames, 剩余buffer)。
    心跳帧 (~h~n) 原样作为字符串返回。
    """
    frames = []
    while buffer:
        if buffer.startswith("~h~"):
            m = re.match(r"~h~(\d+)", buffer)
            if not m:
                break
            frames.append(buffer[: m.end()])
            buffer = buffer[m.end():]
            continue
        m = _FRAME_RE.match(buffer)
        if not m:
            break
        length = int(m.group(1))
        start = m.end()
        if len(buffer) < start + length:
            break  # 帧不完整，等下一次 recv
        frames.append(buffer[start:start + length])
        buffer = buffer[start + length:]
    return frames, buffer


class TradingViewDataCollector:
    """
    TradingView 私有 WS 采集器。

    用法::

        async with TradingViewDataCollector(config.WATCHLIST) as c:
            snapshot = await c.collect()
    """

    def __init__(self, watchlist: List[str], timeframe: str = "5"):
        self.watchlist = watchlist
        self.timeframe = timeframe          # K线周期，"5"=5min, "1D"=日线
        self._ws: Optional[websockets.WebSocketClientProtocol] = None
        self._buffer = ""
        # symbol -> 原始信号字段（指标 plot 映射后的半成品）
        self._raw: Dict[str, Dict[str, float]] = {
            s: {} for s in watchlist
        }
        # chart session 与 symbol 的映射
        self._cs_by_symbol: Dict[str, str] = {}

    # ------------------------------------------------------------------
    # 连接生命周期
    # ------------------------------------------------------------------

    async def __aenter__(self) -> "TradingViewDataCollector":
        await self.connect()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

    async def connect(self) -> None:
        """建立 WS 连接并完成握手（鉴权 + 报价会话 + 图表会话）。"""
        self._ws = await websockets.connect(
            config.TV_WS_URL,
            origin=config.TV_WS_ORIGIN,
            max_size=8 * 1024 * 1024,
            ping_interval=None,   # TV 使用应用层 ~h~ 心跳，不用 WS ping
        )
        qs = f"qs_{uuid.uuid4().hex[:12]}"
        self._quote_session = qs

        # 1) 鉴权
        await self._send("set_auth_token", [config.TV_AUTH_TOKEN])

        # 2) 报价会话：订阅价格/成交量等基础字段
        await self._send("quote_create_session", [qs])
        await self._send("quote_set_fields", [
            qs, "lp", "volume", "bid", "ask", "ch", "chp",
        ])
        for symbol in self.watchlist:
            await self._send("quote_add_symbols", [qs, symbol])

        # 3) 图表会话：每只股票一个 session，挂载 Pine 指标
        #    注意：TV 的 chart session 单会话单 symbol，几十只股票
        #    会有几十个 session；免费会话存在并发上限，
        #    生产环境建议分批采集或升级账号。
        for symbol in self.watchlist:
            cs = f"cs_{uuid.uuid4().hex[:12]}"
            self._cs_by_symbol[symbol] = cs
            await self._send("chart_create_session", [cs, ""])
            resolve_payload = json.dumps({
                "symbol": symbol,
                "adjustment": "splits",
                "session": "regular",
            })
            await self._send("resolve_symbol",
                             [cs, "symbol_1", f"={resolve_payload}"])
            await self._send("create_series",
                             [cs, "sds_1", "s1", "symbol_1",
                              self.timeframe, 300])
            for ind in config.PINE_INDICATORS:
                study_id = f"st_{ind.name}"
                pine = ind.pine_id + ind.version
                # create_study 参数: [cs, study_id, study_id, series,
                #                     pine标识, 输入参数]
                await self._send("create_study",
                                 [cs, study_id, study_id, "sds_1",
                                  pine, ind.inputs])
        logger.info("TV WS 握手完成，监控 %d 只股票", len(self.watchlist))

    async def close(self) -> None:
        if self._ws is not None:
            await self._ws.close()
            self._ws = None

    async def _send(self, method: str, params: list) -> None:
        assert self._ws is not None, "connect() 未被调用"
        await self._ws.send(_pack_frame(method, params))

    # ------------------------------------------------------------------
    # 采集主逻辑
    # ------------------------------------------------------------------

    async def collect(self) -> Dict[str, StockSignalData]:
        """
        在超时窗口内持续消费推送消息，返回 symbol -> StockSignalData 快照。
        未收到数据的股票保留上一次的值/默认值。
        """
        assert self._ws is not None, "connect() 未被调用"
        deadline = time.monotonic() + config.COLLECT_TIMEOUT
        try:
            while time.monotonic() < deadline:
                remaining = deadline - time.monotonic()
                try:
                    raw = await asyncio.wait_for(
                        self._ws.recv(), timeout=min(remaining, 5.0)
                    )
                except asyncio.TimeoutError:
                    break  # 静默窗口期结束，返回当前快照
                await self._handle_raw(raw)
        except websockets.ConnectionClosed as e:
            logger.error("TV WS 连接断开: %s，本轮回传残留快照", e)

        now = time.time()
        return {
            symbol: StockSignalData(
                symbol=symbol,
                supertrend=int(fields.get("supertrend", 0)),
                rejection_score=float(fields.get("rejection_score", 0.0)),
                atr=float(fields.get("atr", 0.0)),
                close=float(fields.get("close", 0.0)),
                volume=float(fields.get("volume", 0.0)),
                timestamp=fields.get("timestamp", now),
                extra={k: v for k, v in fields.items()
                       if k not in {"supertrend", "rejection_score",
                                    "atr", "close", "volume", "timestamp"}},
            )
            for symbol, fields in self._raw.items()
        }

    async def _handle_raw(self, raw: str) -> None:
        """处理一次 recv 的原始字节（可能包含多帧/心跳）。"""
        self._buffer += raw
        frames, self._buffer = _unpack_frames(self._buffer)
        for frame in frames:
            if frame.startswith("~h~"):
                # 应用层心跳：原样回帧
                hb = frame
                await self._ws.send(f"~m~{len(hb)}~m~{hb}")
                continue
            try:
                msg = json.loads(frame)
            except json.JSONDecodeError:
                continue
            method, params = msg.get("m"), msg.get("p", [])
            if method == "qsd" and len(params) >= 2:
                self._on_quote(params[1])
            elif method == "du" and len(params) >= 2:
                self._on_study_update(params[1])
            elif method == "critical_error":
                logger.error("TV 协议错误: %s", params)

    def _on_quote(self, payload: dict) -> None:
        """处理报价推送 (qsd)：回填 close / volume。"""
        symbol = payload.get("n")
        values = payload.get("v", {})
        if symbol not in self._raw:
            return
        if "lp" in values:
            self._raw[symbol]["close"] = float(values["lp"])
        if "volume" in values:
            self._raw[symbol]["volume"] = float(values["volume"])
        self._raw[symbol]["timestamp"] = time.time()

    def _on_study_update(self, payload: dict) -> None:
        """
        处理指标推送 (du)。

        du 结构: { "<cs_session>": { "st_xxx": { "st": [ {i, v:[ts,p0,p1...]} ] } } }
        每个 plot 的最新值按 config.PINE_INDICATORS.plot_map 映射到
        StockSignalData 字段。注意不同版本协议字段可能略有差异，
        联调时请打印原始 payload 校准。
        """
        for cs, studies in payload.items():
            symbol = self._symbol_of_session(cs)
            if symbol is None or not isinstance(studies, dict):
                continue
            for ind in config.PINE_INDICATORS:
                study = studies.get(f"st_{ind.name}")
                if not study:
                    continue
                points = study.get("st", [])
                if not points:
                    continue
                latest = points[-1].get("v", [])
                for field_name, plot_idx in ind.plot_map.items():
                    if plot_idx + 1 < len(latest):  # v[0] 是时间戳
                        value = latest[plot_idx + 1]
                        if value is not None:
                            self._raw[symbol][field_name] = float(value)
            self._raw[symbol]["timestamp"] = time.time()

    def _symbol_of_session(self, cs: str) -> Optional[str]:
        for symbol, session in self._cs_by_symbol.items():
            if session == cs:
                return symbol
        return None


# ---------------------------------------------------------------------------
# PoC 模拟数据源
# ---------------------------------------------------------------------------


class MockDataCollector:
    """
    模拟采集器：随机游走生成伪指标数据。

    与真实采集器保持同一接口（async with + collect()），
    使 main.py 无需关心数据源差异（计算模块与控制模块解耦）。
    """

    def __init__(self, watchlist: List[str], timeframe: str = "5"):
        del timeframe
        self._state: Dict[str, Dict[str, float]] = {}
        for symbol in watchlist:
            self._state[symbol] = {
                "close": random.uniform(50, 500),
                "supertrend": random.choice([-1, 1]),
                "rejection_score": random.uniform(0, 100),
                "atr": random.uniform(0.5, 8.0),
                "volume": random.uniform(1e5, 5e6),
            }

    async def __aenter__(self) -> "MockDataCollector":
        return self

    async def __aexit__(self, *exc) -> None:
        return None

    async def collect(self) -> Dict[str, StockSignalData]:
        now = time.time()
        snapshot: Dict[str, StockSignalData] = {}
        for symbol, s in self._state.items():
            # 随机游走：模拟指标随行情演化
            s["close"] *= 1 + random.uniform(-0.005, 0.005)
            s["rejection_score"] = min(
                100.0, max(0.0,
                           s["rejection_score"] + random.uniform(-8, 8)))
            s["atr"] = max(0.1, s["atr"] + random.uniform(-0.3, 0.3))
            if random.random() < 0.1:  # 10% 概率 SuperTrend 翻转
                s["supertrend"] *= -1
            snapshot[symbol] = StockSignalData(
                symbol=symbol,
                supertrend=int(s["supertrend"]),
                rejection_score=round(s["rejection_score"], 2),
                atr=round(s["atr"], 3),
                close=round(s["close"], 2),
                volume=s["volume"],
                timestamp=now,
            )
        return snapshot


def build_collector(watchlist: List[str]):
    """按 config.DATA_SOURCE 选择数据源实现（mock/cloud/ws）。"""
    source = getattr(config, "DATA_SOURCE", "mock")
    if source == "cloud":
        from engine.cloud_collector import CloudDataCollector
        cls = CloudDataCollector
    elif source == "ws":
        cls = TradingViewDataCollector
    else:
        cls = MockDataCollector
    logger.info("数据源: %s (%s)", cls.__name__, source)
    return cls(watchlist)
