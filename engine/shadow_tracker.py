# -*- coding: utf-8 -*-
"""
统一影子跟踪器 (ShadowTracker)
==============================

对**每一类交易信号**单独建档、假设成交、逐根已收盘 5M bar 跟踪、落库复盘
（不真实下单），用于评估信号有效性。2026-10-25 起由「只覆盖趋势回踩」
扩展为统一影子跟踪。

信号类型（signal_type）与假设成交口径：
- pullback    趋势回踩事件（本跟踪器自检：刺穿收回线值）——入场=线值（即成交）
- proposal    自动策略候选（ST/EMA 回踩限价单）——限价挂 limit_px，触及才成交
- fvg_event   FVG 多/空事件（收盘确认）——市价=事件确认 bar 收盘价
- fvg_retest  FVG 首次回踩——限价挂区域边沿（多=top/空=bottom），触及即成交
              （事件本身即首次触及，建档即成交）
- fvg_warn    FVG 预警（形成中）——市价=预警时实时价
- breakout    盘前突破/跌破——市价=确认 bar 收盘价（meta 带 crowd/triple/区间）

跟踪语义（每笔影子单）：
- 成交后逐根已收盘 bar（bar_c）更新：MFE/MAE（价格与 R 两种口径，R =
  |入场−止损|，止损缺失回退 1×ATR）、reached_1r/2r、是否触止损
  （多 low<=stop / 空 high>=stop，同 bar 先记有利极值再判止损）。
- 止损前 MFE/MAE 止损后冻结（mfe_r/mae_r）；收盘前口径止损后仍跟踪
  （mfe_r_eod/mae_r_eod，衡量信号全部潜力）。
- 结局分类：hit_stop（触止损=−1R）/ reached_2R（盘中到过 2R）/
  eod_flat（15:55 ET 最后一根强平结算，不隔夜）/ unfilled（限价未触及）。
- 跨天处理：看到新交易日（按 bar 的 ET 日期前滚）时，未结算的一律按最近
  收盘价 eod_flat 强制结算（跨天不隔夜，与实盘 EOD 强平一致）。

持久化：logs/research.db 表 shadow_signals（research_db.shadow_upsert），
建档插入、每 bar 更新、结局定稿；重启后从库内恢复当日未结算单继续推演
（原 JSON 日文件已废弃，SQLite 取而代之）。
persist=False（历史回测模式）：纯内存，不写库。

控制台「影子」tab 兼容：ShadowTrade 原有字段名/语义全部保留
（mfe_rr/mfe_rr_eod/stopped/outcome_rr/line_type/entry/stop/atr/bar_time…），
stats() 原有键一个不删，新增 by_type/outcomes 分组。
"""

import json
import logging
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import research_db

logger = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")

LINES = (("ema", "ema_fast"), ("channel", "channel_edge"))
ENTRY_START_HM = 9 * 60 + 30      # 9:30 ET 起（pullback 自检事件窗口）
ENTRY_END_HM = 15 * 60 + 55       # 15:55 ET 起不开新仓（15:50 最后一根信号）
SETTLE_HM = 15 * 60 + 55          # 15:55 ET 的 bar = 收盘了结

SIGNAL_TYPES = ("pullback", "proposal", "fvg_event", "fvg_retest",
                "fvg_warn", "breakout")
OUTCOMES = ("hit_stop", "reached_2R", "eod_flat", "unfilled")


def _et(ts: float) -> datetime:
    return datetime.fromtimestamp(ts, ET)


def _et_hm(ts: float) -> int:
    """epoch 秒 → 美东 时*60+分。"""
    d = _et(ts)
    return d.hour * 60 + d.minute


@dataclass
class ShadowTrade:
    shadow_id: str
    symbol: str
    ticker: str
    side: str                       # long / short
    line_type: str                  # ema / channel / ""（非回踩类信号）
    bar_time: float                 # 信号 bar（已收盘）时间
    entry: float                    # 入场价（market=触发价 / limit=挂单价）
    stop: float                     # 止损价（缺失回退 入场∓1×ATR）
    atr: float                      # 参考 ATR
    created_at: float = field(default_factory=time.time)
    mfe_rr: float = 0.0             # 止损前最大盈亏比（止损后冻结）
    mfe_rr_eod: float = 0.0         # 收盘前最大盈亏比（止损后仍跟踪）
    stopped: bool = False
    stopped_at: float = 0.0         # 触发 bar 的时间
    settled: bool = False
    settled_at: float = 0.0
    close_px: float = 0.0           # 最近 bar 收盘价（结算参考）
    outcome_rr: float = 0.0         # 止损=-1；未止损=收盘结算 RR
    # ---- 统一影子跟踪扩展字段（2026-10-25） ----
    signal_type: str = "pullback"   # 见 SIGNAL_TYPES
    fill_mode: str = "market"       # market（立即成交）/ limit（触及才成交）
    limit_px: float = 0.0           # limit 挂单价（=entry）
    filled: bool = True             # market 建档即成交
    fill_px: float = 0.0
    fill_time: float = 0.0
    mae_rr: float = 0.0             # 止损前最大浮亏（正数计幅，止损后冻结）
    mae_rr_eod: float = 0.0         # 收盘前最大浮亏（止损后仍跟踪）
    reached_1r: bool = False
    reached_2r: bool = False
    outcome: str = ""               # hit_stop/reached_2R/eod_flat/unfilled
    meta: dict = field(default_factory=dict)   # crowd/triple/区间/策略名/ATR倍数…

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "ShadowTrade":
        return ShadowTrade(**d)

    # ---- SQLite 行映射 ----
    def to_row(self, day: str) -> dict:
        return {
            "id": self.shadow_id, "day": day, "type": self.signal_type,
            "symbol": self.symbol, "ticker": self.ticker, "side": self.side,
            "fill_mode": self.fill_mode,
            "trigger_px": self.entry, "limit_px": self.limit_px or None,
            "stop_px": self.stop, "atr": self.atr,
            "bar_time": int(self.bar_time),
            "meta": json.dumps(self.meta, ensure_ascii=False),
            "filled": int(self.filled),
            "fill_px": self.fill_px or None,
            "fill_time": int(self.fill_time) if self.fill_time else None,
            "mfe_r": self.mfe_rr, "mae_r": self.mae_rr,
            "mfe_r_eod": self.mfe_rr_eod, "mae_r_eod": self.mae_rr_eod,
            "reached_1r": int(self.reached_1r),
            "reached_2r": int(self.reached_2r),
            "hit_stop": int(self.stopped),
            "last_px": self.close_px or None,
            "outcome": self.outcome or None,
            "outcome_r": self.outcome_rr if self.settled else None,
            "settled": int(self.settled),
            "settled_at": self.settled_at or None,
            "created_at": self.created_at, "updated_at": time.time(),
        }

    @staticmethod
    def from_row(r: dict) -> "ShadowTrade":
        tr = ShadowTrade(
            shadow_id=r["id"], symbol=r["symbol"], ticker=r["ticker"] or "",
            side=r["side"], line_type=(r.get("meta") and
                                       json.loads(r["meta"]).get("line_type")) or "",
            bar_time=float(r["bar_time"]), entry=float(r["trigger_px"]),
            stop=float(r["stop_px"] or 0), atr=float(r["atr"] or 0),
            created_at=float(r["created_at"]),
            mfe_rr=r["mfe_r"] or 0.0, mfe_rr_eod=r["mfe_r_eod"] or 0.0,
            stopped=bool(r["hit_stop"]), settled=bool(r["settled"]),
            settled_at=r["settled_at"] or 0.0,
            close_px=r["last_px"] or 0.0,
            outcome_rr=r["outcome_r"] or 0.0,
            signal_type=r["type"], fill_mode=r["fill_mode"],
            limit_px=r["limit_px"] or 0.0,
            filled=bool(r["filled"]), fill_px=r["fill_px"] or 0.0,
            fill_time=float(r["fill_time"] or 0),
            mae_rr=r["mae_r"] or 0.0, mae_rr_eod=r["mae_r_eod"] or 0.0,
            reached_1r=bool(r["reached_1r"]), reached_2r=bool(r["reached_2r"]),
            outcome=r["outcome"] or "",
            meta=json.loads(r["meta"]) if r.get("meta") else {},
        )
        # pullback 的线型在旧口径里是一等字段：回填进 line_type
        if not tr.line_type and tr.signal_type == "pullback":
            tr.line_type = tr.meta.get("line_type", "")
        return tr


class ShadowTracker:
    """统一影子跟踪：多类型信号建档 + 逐 bar 推演 + 统计 + SQLite 持久化。

    persist=True（默认，实时模式）：写 logs/research.db 的 shadow_signals
    表并在启动时恢复当日未结算单；persist=False（回测模式）：纯内存。
    db_path 可指定其它库文件（测试用 tmp）。"""

    def __init__(self, persist: bool = True,
                 db_path: Optional[Path] = None,
                 load_day: Optional[str] = None) -> None:
        self._persist = persist
        self._db_path = Path(db_path) if db_path else research_db.DEFAULT_DB
        self._load_day = load_day             # 恢复指定交易日（缺省=今天 ET）
        self._trades: Dict[str, ShadowTrade] = {}        # 全部（含已结算）
        self._event_keys: set = set()                    # 建档去重 key
        self._last_bar_time: Dict[str, float] = {}       # symbol -> 已推演到的 bar_c.time
        self._day = ""                                   # 当前交易日（bar 驱动）
        self._seq = 0
        if self._persist:
            self._load_today()

    # ------------------------------------------------------------------
    # 建档（外部信号源调用）
    # ------------------------------------------------------------------

    def record(self, signal_type: str, symbol: str, side: str,
               trigger_px: float, stop_px: float, atr: float,
               bar_time: float, meta: Optional[dict] = None,
               fill_mode: str = "market",
               limit_px: float = 0.0,
               dedup_key: Optional[str] = None) -> Optional["ShadowTrade"]:
        """登记一笔影子单。market=立即按 trigger_px 成交；limit=挂 limit_px
        等后续确认 bar 触及。止损缺失/非法回退 入场∓1×ATR。
        dedup_key 缺省 = (signal_type, symbol, bar_time)；已建档返回 None。"""
        assert signal_type in SIGNAL_TYPES, f"未知信号类型: {signal_type}"
        assert side in ("long", "short"), f"非法方向: {side}"
        key = dedup_key or f"{signal_type}:{symbol}:{int(bar_time)}"
        if key in self._event_keys:
            return None
        entry = round(float(limit_px if fill_mode == "limit" else trigger_px), 4)
        if entry <= 0:
            return None
        sign = 1 if side == "long" else -1
        stop = float(stop_px or 0)
        if not stop > 0 or (side == "long" and stop >= entry) \
                or (side == "short" and stop <= entry):
            stop = round(entry - sign * float(atr or 0), 2)   # 1×ATR 兜底
        if not stop > 0 or abs(entry - stop) <= 0:
            return None
        self._event_keys.add(key)
        self._seq += 1
        day = _et(bar_time).strftime("%Y-%m-%d")
        tr = ShadowTrade(
            shadow_id=f"sh-{signal_type}-{int(bar_time)}-{self._seq:03d}",
            symbol=symbol, ticker=symbol.split(":")[-1], side=side,
            line_type=(meta or {}).get("line_type", ""),
            bar_time=float(bar_time), entry=entry, stop=round(stop, 4),
            atr=round(float(atr or abs(entry - stop)), 4),
            signal_type=signal_type, fill_mode=fill_mode,
            limit_px=round(float(limit_px), 4) if fill_mode == "limit" else 0.0,
            meta=dict(meta or {}),
        )
        if fill_mode == "market":
            tr.filled = True
            tr.fill_px = entry
            tr.fill_time = float(bar_time)
        else:
            tr.filled = False                 # dataclass 默认 True，限价单显式置否
            tr.fill_px = 0.0
            tr.fill_time = 0.0
        self._trades[tr.shadow_id] = tr
        self._persist_trade(tr, day)
        logger.info("影子建档 %s %s %s 入场 %.2f 止损 %.2f (1R=%.2f) %s",
                    signal_type, symbol, side, tr.entry, tr.stop,
                    abs(tr.entry - tr.stop), meta or "")
        return tr

    # ------------------------------------------------------------------
    # 主入口（每 tick 调用）：逐 symbol 处理新收盘 bar
    # ------------------------------------------------------------------

    def process(self, market: dict) -> Tuple[List[ShadowTrade], List[ShadowTrade]]:
        """处理所有标的的新确认 bar：先限价成交判定与已有影子单推演
        （回测逐 bar），再检测本 bar 产生的新回踩事件（下一根 bar 才开始
        推演）。返回 (新事件列表, 新结算列表)。
        历史回测：按时间正序逐 bar 喂入 {sym: {"bar_c": bar, "ts_c": ts}}
        即可；事件窗口/收盘了结/跨天全部按 bar 的美东时间判定。"""
        new_events: List[ShadowTrade] = []
        new_settled: List[ShadowTrade] = []
        for symbol, data in (market or {}).items():
            bar_c = data.get("bar_c") or {}
            ts_c = data.get("ts_c") or data.get("ts") or {}
            bt = bar_c.get("time")
            if not bt or self._last_bar_time.get(symbol) == bt:
                continue
            day = _et(bt).strftime("%Y%m%d")
            if self._day and day > self._day:
                self._rollover_day(day, new_settled)   # 跨天：未结算强平
            elif not self._day:
                self._day = day
            if day < self._day:
                continue                    # 旧交易日的迟到 bar 不回放
            self._last_bar_time[symbol] = bt
            # 1) 本 bar 作用于该标的所有未结算影子单（先限价成交，再推演）
            for tr in self._trades.values():
                if tr.symbol != symbol or tr.settled:
                    continue
                if not tr.filled:
                    self._try_fill(tr, bar_c)
                    if not tr.filled:
                        if _et_hm(bt) >= SETTLE_HM:
                            self._settle(tr, bar_c)
                            new_settled.append(tr)
                        continue
                if self._apply_bar(tr, bar_c):
                    new_settled.append(tr)
            # 2) 回踩事件自检（pullback）：入场窗口内 + 趋势过滤
            hm = _et_hm(bt)
            if ENTRY_START_HM <= hm < ENTRY_END_HM:
                tr = self._detect(symbol, bar_c, ts_c)
                new_events.extend(tr)
        return new_events, new_settled

    # ------------------------------------------------------------------
    # 限价成交判定（触及才成交）
    # ------------------------------------------------------------------

    def _try_fill(self, tr: ShadowTrade, bar: dict) -> None:
        """limit 单：本确认 bar 触及挂单价 → 按挂单价成交。"""
        if tr.fill_mode != "limit" or tr.filled:
            return
        bt = bar.get("time") or 0
        if bt <= tr.bar_time:
            return                            # 信号 bar 之后的 bar 才判定
        low, high = bar.get("low"), bar.get("high")
        if low is None or high is None:
            return
        touched = (low <= tr.limit_px) if tr.side == "long" \
            else (high >= tr.limit_px)
        if touched:
            tr.filled = True
            tr.fill_px = tr.limit_px
            tr.fill_time = float(bt)
            tr.close_px = bar.get("close") or 0.0
            logger.info("影子限价成交 %s %s @%.2f", tr.symbol, tr.side,
                        tr.fill_px)
            self._persist_trade(tr)

    # ------------------------------------------------------------------
    # 回踩事件自检（pullback，原有行为保留）
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
            if side == "long":
                touched = low <= line and close > line    # 刺穿收回（影线跨线）
            else:
                touched = high >= line and close < line
            if not touched:
                continue
            sign = 1 if side == "long" else -1
            stop = round(line - sign * atr, 2)
            if stop <= 0:
                continue
            tr = self.record(
                "pullback", symbol, side, trigger_px=line, stop_px=stop,
                atr=atr, bar_time=bt, fill_mode="market",
                meta={"line_type": line_type},
                dedup_key=f"pullback:{symbol}:{line_type}:{int(bt)}")
            if tr is not None:
                tr.close_px = close
                out.append(tr)
        return out

    # ------------------------------------------------------------------
    # 逐 bar 回测推演（backtest 语义：high/low 判 MFE/MAE 与止损，15:55 了结）
    # ------------------------------------------------------------------

    def _risk(self, tr: ShadowTrade) -> float:
        """1R = |入场 − 止损|（止损缺失回退 ATR）。"""
        r = abs(tr.entry - tr.stop)
        return r if r > 0 else (tr.atr or 1.0)

    def _rr(self, tr: ShadowTrade, px: float) -> float:
        """价格 → R 倍数（含方向）。"""
        sign = 1 if tr.side == "long" else -1
        return sign * (px - tr.entry) / self._risk(tr)

    def _apply_bar(self, tr: ShadowTrade, bar: dict) -> bool:
        """本 bar 作用于影子单（仅已成交）；返回 True 表示本 bar 使其结算。"""
        bt = bar.get("time") or 0
        if bt <= (tr.fill_time or tr.bar_time):
            return False                        # 成交 bar 之后的 bar 才推演
        high, low = bar.get("high"), bar.get("low")
        close = bar.get("close")
        if close:
            tr.close_px = close
        favor = high if tr.side == "long" else low
        adverse = low if tr.side == "long" else high
        # 收盘前口径：全天逐 bar 跟踪（止损后不冻结）
        if favor is not None:
            tr.mfe_rr_eod = max(tr.mfe_rr_eod, round(self._rr(tr, favor), 3))
        if adverse is not None:
            tr.mae_rr_eod = max(tr.mae_rr_eod, round(-self._rr(tr, adverse), 3))
        if not tr.stopped:
            # 止损前口径（含触发 bar 的极值，取有利口径）
            if favor is not None:
                tr.mfe_rr = max(tr.mfe_rr, round(self._rr(tr, favor), 3))
            if adverse is not None:
                tr.mae_rr = max(tr.mae_rr, round(-self._rr(tr, adverse), 3))
            tr.reached_1r = tr.reached_1r or tr.mfe_rr >= 1
            tr.reached_2r = tr.reached_2r or tr.mfe_rr >= 2
            # 止损判定：多 low<=stop / 空 high>=stop
            if adverse is not None and (
                    (tr.side == "long" and adverse <= tr.stop)
                    or (tr.side == "short" and adverse >= tr.stop)):
                tr.stopped = True
                tr.stopped_at = bt
                tr.outcome_rr = -1.0
                logger.info("影子止损 %s %s %s @%.2f（止损前最大 %.2fR）",
                            tr.symbol, tr.side, tr.signal_type, tr.stop,
                            tr.mfe_rr)
        # 收盘了结：15:55 ET 的 bar 收盘，全部结算（跨天不隔夜）
        if _et_hm(bt) >= SETTLE_HM:
            self._settle(tr, bar)
            return True
        self._persist_trade(tr)
        return False

    def _settle(self, tr: ShadowTrade, bar: dict) -> None:
        """结局定稿：hit_stop / reached_2R / eod_flat / unfilled。"""
        close = bar.get("close")
        if close:
            tr.close_px = close
        if not tr.filled:
            tr.outcome = "unfilled"           # 限价未触及
            tr.outcome_rr = 0.0
        elif tr.stopped:
            tr.outcome = "hit_stop"
            tr.outcome_rr = -1.0
        else:
            px = tr.close_px or tr.entry
            tr.outcome_rr = round(self._rr(tr, px), 3)
            tr.mfe_rr = max(tr.mfe_rr, tr.outcome_rr)
            tr.reached_1r = tr.reached_1r or tr.mfe_rr >= 1
            tr.reached_2r = tr.reached_2r or tr.mfe_rr >= 2
            tr.outcome = "reached_2R" if tr.reached_2r else "eod_flat"
        tr.settled = True
        tr.settled_at = time.time()
        self._persist_trade(tr)
        logger.info("影子定稿 %s %s %s 结局 %s %.2fR",
                    tr.symbol, tr.side, tr.signal_type, tr.outcome,
                    tr.outcome_rr)

    # ------------------------------------------------------------------
    # 统计
    # ------------------------------------------------------------------

    def stats(self, max_n: int = 5) -> dict:
        """当日统计：N 倍盈亏比分布 + 未达 1R 直接亏损占比 + 按类型/结局分组。
        原有键全部保留（前端影子 tab 兼容），新增 by_type / outcomes。"""
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
        by_type: Dict[str, dict] = {}
        for st in SIGNAL_TYPES:
            rows = [t for t in settled if t.signal_type == st]
            if not rows:
                continue
            by_type[st] = {
                "count": len(rows),
                "pct_ge_1r": round(sum(1 for t in rows if t.mfe_rr >= 1)
                                   / len(rows) * 100, 1),
                "avg_mfe": round(sum(t.mfe_rr for t in rows) / len(rows), 2),
                "avg_mae": round(sum(t.mae_rr for t in rows) / len(rows), 2),
                "avg_outcome_rr": round(sum(t.outcome_rr for t in rows)
                                        / len(rows), 2),
            }
        outcomes: Dict[str, int] = {}
        for t in settled:
            outcomes[t.outcome or "?"] = outcomes.get(t.outcome or "?", 0) + 1
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
            "avg_mae_before_stop": (round(sum(t.mae_rr for t in settled)
                                          / n_settled, 2) if n_settled else 0.0),
            "avg_mae_eod": (round(sum(t.mae_rr_eod for t in settled)
                                  / n_settled, 2) if n_settled else 0.0),
            "max_mfe_before_stop": (round(max(t.mfe_rr for t in settled), 2)
                                    if n_settled else 0.0),
            "max_mfe_eod": (round(max(t.mfe_rr_eod for t in settled), 2)
                            if n_settled else 0.0),
            "avg_outcome_rr": (round(sum(t.outcome_rr for t in settled)
                                     / n_settled, 2) if n_settled else 0.0),
            "by_line": by_line,
            "by_side": by_side,
            "by_type": by_type,          # 按信号类型分组（新增）
            "outcomes": outcomes,        # 结局分布（新增）
        }

    def events(self, limit: int = 200) -> List[dict]:
        rows = sorted(self._trades.values(),
                      key=lambda t: t.bar_time, reverse=True)
        return [t.to_dict() for t in rows[:limit]]

    # ------------------------------------------------------------------
    # 持久化（SQLite research.db shadow_signals；原 JSON 日文件已废弃）
    # ------------------------------------------------------------------

    def _persist_trade(self, tr: ShadowTrade, day: Optional[str] = None) -> None:
        if not self._persist:
            return
        try:
            research_db.shadow_upsert(
                tr.to_row(day or _et(tr.bar_time).strftime("%Y-%m-%d")),
                db_path=self._db_path)
        except Exception:
            logger.exception("影子单落库失败 %s", tr.shadow_id)

    def _load_today(self) -> None:
        day = self._load_day or datetime.now(ET).strftime("%Y-%m-%d")
        try:
            rows = research_db.shadow_load_day(day, db_path=self._db_path)
        except Exception as e:
            logger.warning("影子单加载失败: %s", e)
            return
        for r in rows:
            tr = ShadowTrade.from_row(r)
            self._trades[tr.shadow_id] = tr
            self._event_keys.add(f"{tr.signal_type}:{tr.symbol}:{int(tr.bar_time)}")
            if tr.signal_type == "pullback":
                self._event_keys.add(
                    f"pullback:{tr.symbol}:{tr.line_type}:{int(tr.bar_time)}")
            self._last_bar_time[tr.symbol] = max(
                self._last_bar_time.get(tr.symbol, 0),
                tr.fill_time or tr.bar_time)
            self._seq += 1
        if rows:
            self._day = day.replace("-", "")
            logger.info("已从库恢复当日影子单 %d 笔", len(rows))

    def _rollover_day(self, day: str,
                      new_settled: Optional[list] = None) -> None:
        """跨交易日（bar 驱动前滚）：未结算一律按最近收盘价 eod_flat 强平
        （跨天不隔夜），然后清空内存进入新一天。"""
        for tr in self._trades.values():
            if tr.settled:
                continue
            tr.settled = True
            tr.settled_at = time.time()
            if not tr.filled:
                tr.outcome = "unfilled"
                tr.outcome_rr = 0.0
            elif tr.stopped:
                tr.outcome = "hit_stop"
                tr.outcome_rr = -1.0
            else:
                px = tr.close_px or tr.entry
                tr.outcome_rr = round(self._rr(tr, px), 3)
                tr.outcome = "reached_2R" if tr.reached_2r else "eod_flat"
            self._persist_trade(tr, day=(
                datetime.strptime(self._day, "%Y%m%d").strftime("%Y-%m-%d")
                if self._day else None))
            if new_settled is not None:
                new_settled.append(tr)
        self._day = day
        self._trades.clear()
        self._event_keys.clear()
        self._last_bar_time.clear()
        self._seq = 0
