# -*- coding: utf-8 -*-
"""
影子回测跟踪器 (ShadowTracker)
==============================

对每一次「回踩事件」登记一笔影子交易，按**回测方法**逐根已收盘 5M bar
推演（与 backtest_pullback.py 同一套语义：每根 bar 用 high/low 判定
止损与最大有利偏移，15:55 ET 最后一根 bar 收盘了结），不真实下单，
用于评估信号有效性。

- 回踩事件定义（确认 bar_c + trend_state 确认值 ts_c，所见即所得）：
  - 多头（trend_state == 1）：low 刺穿线值（low <= line）且收盘收回线上
    （close > line）——影线跨线才算回踩；
  - 空头（trend_state == -1）：high 刺穿线值（high >= line）且收盘收回
    线下（close < line）。
  线有两条：ema_fast（EMA20）与 channel_edge（ST±1×ATR 通道边沿）。
- 影子交易：入场 = 事件 bar 的线值；止损 = 入场 ∓ 1×ATR（与实盘一致）；
  1R = 1×ATR。事件 bar 的下一根 bar 才开始推演（bar 收盘才知道事件）。
- 推演规则（每根已收盘 bar，多仓为例，空仓镜像）：
  - mfe_rr（止损前最大盈亏比）：max(历史, (bar.high − entry)/ATR)；
  - 止损：bar.low <= stop → stopped，outcome = −1R（同 bar 先记 high 的
    有利极值再判止损——5M 粒度无法分辨先后，取有利口径）；
  - mfe_rr_eod（收盘前最大盈亏比）：止损后仍继续逐 bar 跟踪，衡量信号
    全部潜力（止损后价格又走多远）；
  - 15:55 ET 的 bar：未止损的按该 bar 收盘价了结（收盘了结），全部
    标记 settled 落盘。
- 统计：达到 1R/2R/.../5R 的信号数与占比（止损前/收盘前两种口径）；
  连 1R 都未达就止损的「直接亏损」占比；按线型/方向分组。

入场窗口 9:30–15:50 ET（与回测一致，最后一根不开仓）；事件只在常规
时段产生。当日记录落盘 logs/shadow-YYYYMMDD.json，重启后统计不丢
（未结算的盘中逐 bar 状态从下一根新 bar 继续推演，已处理 bar 不重放）。
"""

import json
import logging
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Tuple
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
LOGS_DIR = PROJECT_ROOT / "logs"
ET = ZoneInfo("America/New_York")

LINES = (("ema", "ema_fast"), ("channel", "channel_edge"))
ENTRY_START_HM = 9 * 60 + 30      # 9:30 ET 起
ENTRY_END_HM = 15 * 60 + 55       # 15:55 ET 起不开新仓（15:50 最后一根信号）
SETTLE_HM = 15 * 60 + 55          # 15:55 ET 的 bar = 收盘了结


def _et_hm(ts: float) -> int:
    """epoch 秒 → 美东 时*60+分。"""
    d = datetime.fromtimestamp(ts, ET)
    return d.hour * 60 + d.minute


@dataclass
class ShadowTrade:
    shadow_id: str
    symbol: str
    ticker: str
    side: str                       # long / short
    line_type: str                  # ema / channel
    bar_time: float                 # 信号 bar（已收盘）时间
    entry: float                    # 线值（入场价）
    stop: float                     # 入场 ∓ 1×ATR
    atr: float                      # 1R 距离
    created_at: float = field(default_factory=time.time)
    mfe_rr: float = 0.0             # 止损前最大盈亏比（止损后冻结）
    mfe_rr_eod: float = 0.0         # 收盘前最大盈亏比（止损后仍跟踪）
    stopped: bool = False
    stopped_at: float = 0.0         # 触发 bar 的时间
    settled: bool = False
    settled_at: float = 0.0
    close_px: float = 0.0           # 最近 bar 收盘价（结算参考）
    outcome_rr: float = 0.0         # 止损=-1；未止损=收盘结算 RR

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "ShadowTrade":
        return ShadowTrade(**d)


class ShadowTracker:
    """回踩事件检测 + 逐 bar 回测推演 + 统计。
    persist=True（默认，实时模式）：当日记录落盘 logs/shadow-YYYYMMDD.json；
    persist=False（历史回测模式）：纯内存，不读写任何文件。"""

    def __init__(self, logs_dir: Path = LOGS_DIR, persist: bool = True) -> None:
        self._logs_dir = Path(logs_dir)
        self._persist = persist
        self._trades: Dict[str, ShadowTrade] = {}        # 全部（含已结算）
        self._event_keys: set = set()                    # (symbol, line, bar_time) 去重
        self._last_bar_time: Dict[str, float] = {}       # symbol -> 已推演到的 bar_c.time
        self._day = time.strftime("%Y%m%d")
        self._seq = 0
        if self._persist:
            self._load_today()

    # ------------------------------------------------------------------
    # 主入口（每 tick 调用）：逐 symbol 处理新收盘 bar
    # ------------------------------------------------------------------

    def process(self, market: dict) -> Tuple[List[ShadowTrade], List[ShadowTrade]]:
        """处理所有标的的新确认 bar：先推演已有影子交易（回测逐 bar），
        再检测本 bar 产生的新回踩事件（下一根 bar 才开始推演）。
        返回 (新事件列表, 新结算列表)。
        历史回测：按时间正序逐 bar 喂入 {sym: {"bar_c": bar, "ts_c": ts}} 即可，
        事件窗口/收盘了结全部按 bar 的美东时间判定，与调用时的真实时钟无关。"""
        if self._persist and self._day != time.strftime("%Y%m%d"):
            self._rollover_day()
        new_events: List[ShadowTrade] = []
        new_settled: List[ShadowTrade] = []
        for symbol, data in (market or {}).items():
            bar_c = data.get("bar_c") or {}
            ts_c = data.get("ts_c") or data.get("ts") or {}
            bt = bar_c.get("time")
            if not bt or self._last_bar_time.get(symbol) == bt:
                continue
            self._last_bar_time[symbol] = bt
            # 1) 回测推演：本 bar 作用于该标的所有未结算影子交易
            for tr in self._trades.values():
                if tr.symbol == symbol and not tr.settled:
                    if self._apply_bar(tr, bar_c):
                        new_settled.append(tr)
            # 2) 事件检测：入场窗口内 + 趋势过滤
            hm = _et_hm(bt)
            if ENTRY_START_HM <= hm < ENTRY_END_HM:
                tr = self._detect(symbol, bar_c, ts_c)
                new_events.extend(tr)
        if new_settled:
            self._save_day()
        return new_events, new_settled

    # ------------------------------------------------------------------
    # 事件检测（每线每 bar 最多一次）
    # ------------------------------------------------------------------

    def _detect(self, symbol: str, bar_c: dict, ts_c: dict) -> List[ShadowTrade]:
        out: List[ShadowTrade] = []
        trend = ts_c.get("trend_state")
        if trend not in (1, -1):
            return out
        atr = ts_c.get("atr")
        if not atr or atr <= 0:
            return out
        low, high = bar_c.get("low"), bar_c.get("high")
        close = bar_c.get("close")
        bt = bar_c.get("time")
        if low is None or high is None or close is None or bt is None:
            return out
        side = "long" if trend == 1 else "short"
        for line_type, key in LINES:
            line = ts_c.get(key)
            if not line or line <= 0:
                continue
            ev_key = (symbol, line_type, bt)
            if ev_key in self._event_keys:
                continue
            if side == "long":
                touched = low <= line and close > line    # 刺穿收回（影线跨线）
            else:
                touched = high >= line and close < line
            if not touched:
                continue
            self._event_keys.add(ev_key)
            sign = 1 if side == "long" else -1
            stop = round(line - sign * atr, 2)
            if stop <= 0:
                continue
            self._seq += 1
            tr = ShadowTrade(
                shadow_id=f"sh-{int(bt)}-{self._seq:03d}",
                symbol=symbol, ticker=symbol.split(":")[-1],
                side=side, line_type=line_type, bar_time=float(bt),
                entry=round(line, 4), stop=stop, atr=round(float(atr), 4),
                close_px=close,
            )
            self._trades[tr.shadow_id] = tr
            out.append(tr)
            logger.info("回踩事件 %s %s %s线 入场 %.2f 止损 %.2f (1R=%.4f)",
                        symbol, side, line_type, tr.entry, tr.stop, tr.atr)
        return out

    # ------------------------------------------------------------------
    # 逐 bar 回测推演（backtest 语义：high/low 判 MFE 与止损，15:55 了结）
    # ------------------------------------------------------------------

    def _favorable_rr(self, tr: ShadowTrade, px: float) -> float:
        if tr.side == "long":
            return (px - tr.entry) / tr.atr
        return (tr.entry - px) / tr.atr

    def _apply_bar(self, tr: ShadowTrade, bar: dict) -> bool:
        """本 bar 作用于影子交易；返回 True 表示本 bar 使其结算（settled）。"""
        high, low = bar.get("high"), bar.get("low")
        close = bar.get("close")
        bt = bar.get("time") or 0
        if close:
            tr.close_px = close
        favor = high if tr.side == "long" else low
        # 收盘前最大盈亏比：全天逐 bar 跟踪（止损后不冻结）
        if favor is not None:
            tr.mfe_rr_eod = max(tr.mfe_rr_eod,
                                round(self._favorable_rr(tr, favor), 3))
        if not tr.stopped:
            # 止损前 MFE（含触发 bar 的有利极值，取有利口径）
            if favor is not None:
                tr.mfe_rr = max(tr.mfe_rr,
                                round(self._favorable_rr(tr, favor), 3))
            # 止损判定：多: low <= stop / 空: high >= stop
            adverse = low if tr.side == "long" else high
            if adverse is not None and (
                    (tr.side == "long" and adverse <= tr.stop)
                    or (tr.side == "short" and adverse >= tr.stop)):
                tr.stopped = True
                tr.stopped_at = bt
                tr.outcome_rr = -1.0
                logger.info("影子止损 %s %s %s线 @%.2f（止损前最大 %.2fR）",
                            tr.symbol, tr.side, tr.line_type, tr.stop,
                            tr.mfe_rr)
                self._save_day()
        # 收盘了结：15:55 ET 的 bar 收盘，全部结算
        if _et_hm(bt) >= SETTLE_HM:
            if not tr.stopped:
                px = close or tr.close_px or tr.entry
                tr.outcome_rr = round(self._favorable_rr(tr, px), 3)
                tr.mfe_rr = max(tr.mfe_rr, tr.outcome_rr)
            tr.settled = True
            tr.settled_at = time.time()
            return True
        return False

    # ------------------------------------------------------------------
    # 统计
    # ------------------------------------------------------------------

    def stats(self, max_n: int = 5) -> dict:
        """当日统计：N 倍盈亏比分布 + 未达 1R 直接亏损占比。"""
        settled = [t for t in self._trades.values() if t.settled]
        active = [t for t in self._trades.values() if not t.settled]
        n_settled = len(settled)

        def _dist(field_name: str) -> dict:
            out = {}
            for n in range(1, max_n + 1):
                c = sum(1 for t in settled
                        if getattr(t, field_name) >= n)
                out[str(n)] = {
                    "count": c,
                    "pct": round(c / n_settled * 100, 1) if n_settled else 0.0,
                }
            return out

        # 连 1R 都没到就止损（直接亏损）
        no_1r = [t for t in settled if t.stopped and t.mfe_rr < 1]
        by_line: Dict[str, dict] = {}
        for lt, _ in LINES:
            rows = [t for t in settled if t.line_type == lt]
            ge1 = sum(1 for t in rows if t.mfe_rr >= 1)
            by_line[lt] = {
                "count": len(rows),
                "ge_1r": ge1,
                "pct_ge_1r": round(ge1 / len(rows) * 100, 1) if rows else 0.0,
                "avg_mfe": (round(sum(t.mfe_rr for t in rows) / len(rows), 2)
                            if rows else 0.0),
            }
        by_side: Dict[str, dict] = {}
        for sd in ("long", "short"):
            rows = [t for t in settled if t.side == sd]
            ge1 = sum(1 for t in rows if t.mfe_rr >= 1)
            by_side[sd] = {
                "count": len(rows),
                "pct_ge_1r": round(ge1 / len(rows) * 100, 1) if rows else 0.0,
            }
        return {
            "day": self._day,
            "total_events": len(self._trades),
            "active": len(active),
            "settled": n_settled,
            "stopped": sum(1 for t in settled if t.stopped),
            "dist_before_stop": _dist("mfe_rr"),      # 止损前最大盈亏比 ≥N 分布
            "dist_eod": _dist("mfe_rr_eod"),          # 收盘前最大盈亏比 ≥N 分布
            "no_1r_count": len(no_1r),                # 未达 1R 直接止损数
            "no_1r_pct": (round(len(no_1r) / n_settled * 100, 1)
                          if n_settled else 0.0),
            "avg_mfe_before_stop": (round(sum(t.mfe_rr for t in settled)
                                          / n_settled, 2) if n_settled else 0.0),
            "avg_mfe_eod": (round(sum(t.mfe_rr_eod for t in settled)
                                  / n_settled, 2) if n_settled else 0.0),
            "max_mfe_before_stop": (round(max(t.mfe_rr for t in settled), 2)
                                    if n_settled else 0.0),
            "max_mfe_eod": (round(max(t.mfe_rr_eod for t in settled), 2)
                            if n_settled else 0.0),
            "avg_outcome_rr": (round(sum(t.outcome_rr for t in settled)
                                     / n_settled, 2) if n_settled else 0.0),
            "by_line": by_line,
            "by_side": by_side,
        }

    def events(self, limit: int = 200) -> List[dict]:
        rows = sorted(self._trades.values(),
                      key=lambda t: t.bar_time, reverse=True)
        return [t.to_dict() for t in rows[:limit]]

    # ------------------------------------------------------------------
    # 持久化（按天落盘，重启后统计不丢）
    # ------------------------------------------------------------------

    def _day_file(self) -> Path:
        return self._logs_dir / f"shadow-{self._day}.json"

    def _save_day(self) -> None:
        if not self._persist:
            return
        try:
            self._logs_dir.mkdir(parents=True, exist_ok=True)
            payload = [t.to_dict() for t in self._trades.values()]
            self._day_file().write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8")
        except OSError:
            logger.exception("影子交易落盘失败")

    def _load_today(self) -> None:
        path = self._day_file()
        if not path.exists():
            return
        try:
            for d in json.loads(path.read_text(encoding="utf-8")):
                tr = ShadowTrade.from_dict(d)
                self._trades[tr.shadow_id] = tr
                self._event_keys.add((tr.symbol, tr.line_type, tr.bar_time))
                self._last_bar_time[tr.symbol] = max(
                    self._last_bar_time.get(tr.symbol, 0), tr.bar_time)
                self._seq += 1
            logger.info("已恢复当日影子交易 %d 笔", len(self._trades))
        except (OSError, ValueError, TypeError) as e:
            logger.warning("影子交易加载失败: %s", e)

    def _rollover_day(self) -> None:
        self._day = time.strftime("%Y%m%d")
        self._trades.clear()
        self._event_keys.clear()
        self._last_bar_time.clear()
        self._seq = 0
        if self._persist:
            self._load_today()
