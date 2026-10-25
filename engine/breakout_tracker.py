# -*- coding: utf-8 -*-
"""
盘前突破 + FVG 确认 流式信号跟踪器 (BreakoutTracker)
====================================================

把 research_breakout.py 的批量「盘前突破 + FVG 确认」算法改造成实盘逐 bar
流式检测（2026-10-25 起多空双出：站上高点=多头、跌破低点=空头，口径与
批量脚本 --side both 完全一致，回放逐条对拍验收）。

算法规格（与 research_breakout.py 逐条对齐）：
1. 盘前区间 = 当天 04:00–09:25 ET 的 5M 高低点（hm < 570 的 bar 全部累积；
   遇到第一根 hm >= 570 的 bar 锁定区间；pmh - pml <= 0 当日不参与）。
2. 09:45 ET 起（2026-10-25 用户定：窗口从 09:30–10:00 挪到 09:45–10:30）：前收 <= 盘前高 且 本收 > 盘前高 = 多头突破候选
   （每一次越界都算，收回来再越界是新一次）；前收 >= 盘前低 且 本收 <
   盘前低 = 空头突破候选（跌破低点，镜像）。
   拥挤计数多空混算（批量口径，对拍一致的关键）。
3. 候选挂起等该标的的下一根 bar：同向 FVG（多 fvg_event==1 / 空 -1）
   且同向 K 线（多 close>open / 空 close<open）→ 候选成立，
   确认时刻 = 这根 FVG bar 的时间；不满足 → 作废。
4. 动态拥挤规则：同一确认时刻成立的候选（跨 symbol 汇总，多空混计）
   <= 3 只 → 立即出信号；> 3 只 → 升级再等一根，要求三连阳
   （突破根+FVG根+第三根全阳且高点依次抬高；空头镜像三连阴低点降低），
   确认时刻延后到第三根 bar 时间，第三根不满足 → 作废。
5. 确认时刻（含三连延后）必须 hm <= 630（10:30 ET），超出丢弃。
6. 期货/CFD 剔除：symbol 以 BLACKBULL: / CAPITALCOM: 开头的不参与。

流式化接口：
- on_bar(symbol, bar) -> Optional[dict]：bar = {time(epoch秒), open, high,
  low, close, fvg_event(int, 缺省按 0)}。返回三连阳路径即时成立的信号
  （每组最多一根第三根 bar 属同一 symbol，故至多一条），其余情况 None。
- flush(bar_time) -> List[dict]：外部在同一根 bar 时间的所有 symbol 都
  喂完后调用；对该时刻（及之前未判定）的候选组做拥挤终判：<=3 全部
  发出，>3 升级等第三根（第三根由各自 symbol 的下一根 on_bar 解析）。
- set_premarket(symbol, pmh, pml, day) -> bool：注入权威盘前区间
  （如实盘 TV「5M 进攻区域」指标）。注入后该 symbol 当天以此为准、
  跳过自累积（已锁定的也会改按注入值）；值非法（None / pmh<=pml /
  pml<=0）→ 忽略并返回 False，维持自累积。未注入的 symbol 行为不变，
  批量回放口径不受影响。
- 新交易日自动重置每 symbol 的盘前累积/挂起候选；跨天未判定候选组
  整体丢弃（与批量脚本 nxt 不存在即作废同口径）。

纯逻辑、无 IO、无全局状态；信号 dict 字段与 research_breakout 输出一致：
symbol/ticker/side/confirm_hm/confirm_time/break_time/close/
premarket_high/premarket_low/zone_width/crowd/triple（另附
confirm_epoch/break_epoch 供前端排序与展示）。
"""

from datetime import datetime
from typing import Dict, List, Optional
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

PREMARKET_END_HM = 9 * 60 + 30  # 09:30 ET：盘前累积边界（hm < 570 为盘前）
BREAK_MIN_HM = 9 * 60 + 45      # 09:45 ET 起检测突破（2026-10-25 用户定：窗口 09:45–10:30）
MAX_HM = 10 * 60 + 30           # 确认时刻上限 10:30 ET（含三连延后）
CROWD_MAX = 3                 # 同确认时刻候选数上限，超过升级三连确认
EXCLUDE_PREFIXES = ("BLACKBULL:", "CAPITALCOM:")   # 期货/CFD 不参与


def _et(ts: float) -> datetime:
    return datetime.fromtimestamp(ts, ET)


def _hm(ts: float) -> int:
    d = _et(ts)
    return d.hour * 60 + d.minute


class BreakoutTracker:
    """盘前突破流式检测（每 symbol 独立状态 + 跨 symbol 拥挤分组）。"""

    def __init__(self) -> None:
        self._states: Dict[str, dict] = {}     # symbol -> 每标的流式状态
        self._groups: Dict[float, dict] = {}   # 确认bar epoch -> 候选组
        self._day: Optional[str] = None        # 当前交易日（ET 日期串）

    # ------------------------------------------------------------------
    # 主接口
    # ------------------------------------------------------------------

    def on_bar(self, symbol: str, bar: dict) -> Optional[dict]:
        """喂一根已确认 bar；三连阳路径即时成立时返回信号 dict，否则 None。"""
        if symbol.startswith(EXCLUDE_PREFIXES):
            return None
        bt = bar.get("time")
        close = bar.get("close")
        if not bt or close is None:
            return None
        day = _et(bt).strftime("%Y-%m-%d")
        if self._day is not None and day > self._day:
            self._rollover_day(day)
        elif self._day is None:
            self._day = day
        st = self._states.setdefault(symbol, self._fresh_state(day))
        if st["day"] != day:                   # 该 symbol 跨天：重置盘前累积
            st.update(self._fresh_state(day))
        hm = _hm(bt)

        sig = self._resolve_escalated(symbol, st, bar)
        self._resolve_pending(symbol, st, bar)

        if hm < PREMARKET_END_HM:
            # 盘前累积（锁定后同一天的 bar 只会 hm>=570，无需防回退；
            # 已注入权威区间的 symbol 跳过自累积，以注入值为准）
            if not st["locked"] and st["inj"] is None:
                high, low = bar.get("high"), bar.get("low")
                if high is not None and low is not None:
                    st["pmh"] = high if st["pmh"] is None else max(st["pmh"], high)
                    st["pml"] = low if st["pml"] is None else min(st["pml"], low)
            st["prev_close"] = close
            return sig

        if not st["locked"]:                   # 第一根常规时段 bar：锁定区间
            st["locked"] = True
            if st["inj"] is not None:          # 注入的权威区间优先
                st["pmh"], st["pml"] = st["inj"]
                st["eligible"] = True
            else:
                st["eligible"] = (st["pmh"] is not None
                                  and st["pmh"] - st["pml"] > 0)
        if (st["eligible"] and st["prev_close"] is not None
                and hm >= BREAK_MIN_HM):      # 突破检测起点 09:45（盘前锁定仍是 09:30）
            pmh, pml, prev = st["pmh"], st["pml"], st["prev_close"]
            if prev <= pmh and close > pmh:
                st["pending"] = {"side": "站上高点", "want_fvg": 1,
                                 "break_bar": dict(bar)}
            elif prev >= pml and close < pml:
                st["pending"] = {"side": "跌破低点", "want_fvg": -1,
                                 "break_bar": dict(bar)}
        st["prev_close"] = close
        return sig

    def set_premarket(self, symbol: str, pmh, pml, day: str) -> bool:
        """注入权威盘前区间（TV「5M 进攻区域」指标，盘中重启控制台也能拿到
        正确区间）。day = ET 交易日（YYYY-MM-DD，调用方按 bar 时间算）。
        注入后该 symbol 当天以此为准、跳过自累积；已锁定的改按注入值。
        值非法（None / pmh<=pml / pml<=0）→ 忽略返回 False，维持自累积。"""
        if symbol.startswith(EXCLUDE_PREFIXES):
            return False
        try:
            pmh, pml = float(pmh), float(pml)
        except (TypeError, ValueError):
            return False
        if not pmh > pml > 0:
            return False
        st = self._states.get(symbol)
        if st is None or st["day"] != day:
            st = self._states.setdefault(symbol, self._fresh_state(day))
            if st["day"] != day:               # 跨天注入：重置（同 on_bar）
                st.update(self._fresh_state(day))
        st["inj"] = (pmh, pml)
        if st["locked"]:
            st["pmh"], st["pml"] = st["inj"]
            st["eligible"] = True
        return True

    def flush(self, bar_time: float) -> List[dict]:
        """同一根 bar 时间的全部 symbol 喂完后调用：拥挤终判。

        <=3 只 → 立即出信号；>3 只 → 升级三连确认（候选移交各自 symbol
        的 escalated 挂起，等下一根 on_bar 解析）。返回本拍发出的信号。"""
        out: List[dict] = []
        for ctime in sorted(self._groups):
            if ctime > bar_time:
                continue
            grp = self._groups.pop(ctime)
            cands = grp["cands"]
            grp["crowd"] = len(cands)
            if len(cands) <= CROWD_MAX:
                for c in cands:
                    sig = self._make_signal(c, c["fvg_bar"], triple=False,
                                            crowd=len(cands))
                    if sig is not None:
                        out.append(sig)
            else:
                for c in cands:
                    c["crowd"] = len(cands)
                    st = self._states.get(c["symbol"])
                    if st is not None and st["day"] == c["day"]:
                        st["escalated"].append(c)
        return out

    # ------------------------------------------------------------------
    # 内部：候选解析
    # ------------------------------------------------------------------

    @staticmethod
    def _fresh_state(day: str) -> dict:
        return {"day": day, "pmh": None, "pml": None, "locked": False,
                "eligible": False, "prev_close": None, "pending": None,
                "escalated": [], "inj": None}

    def _rollover_day(self, day: str) -> None:
        """跨交易日：未判定候选组整体丢弃（同批量 nxt 缺失即作废口径）。"""
        self._day = day
        self._groups.clear()

    def _resolve_pending(self, symbol: str, st: dict, bar: dict) -> None:
        """上一根若是突破候选，本根为「下一根」：同向 FVG + 同向 K 线才成立。"""
        cand = st["pending"]
        if cand is None:
            return
        st["pending"] = None
        fvg_event = bar.get("fvg_event") or 0
        if fvg_event != cand["want_fvg"]:
            return
        o, c = bar.get("open"), bar.get("close")
        if o is None or c is None:
            return
        if cand["want_fvg"] == 1 and not c > o:
            return
        if cand["want_fvg"] == -1 and not c < o:
            return
        cand["fvg_bar"] = dict(bar)
        cand["symbol"] = symbol
        cand["day"] = st["day"]
        self._snapshot_zone(st, cand)
        grp = self._groups.setdefault(bar["time"], {"cands": []})
        grp["cands"].append(cand)

    def _resolve_escalated(self, symbol: str, st: dict, bar: dict) -> Optional[dict]:
        """本 bar 作为升级候选的「第三根」：三连阳/阴 + 极值递推才成立。"""
        if not st["escalated"]:
            return None
        sig = None
        waiting, st["escalated"] = st["escalated"], []
        for c in waiting:
            b, nxt, third = c["break_bar"], c["fvg_bar"], bar
            o3, c3 = third.get("open"), third.get("close")
            h3, l3 = third.get("high"), third.get("low")
            if None in (o3, c3, h3, l3):
                continue
            if c["side"] == "站上高点":
                ok = (b["close"] > b["open"] and nxt["close"] > nxt["open"]
                      and c3 > o3 and b["high"] < nxt["high"] < h3)
            else:
                ok = (b["close"] < b["open"] and nxt["close"] < nxt["open"]
                      and c3 < o3 and b["low"] > nxt["low"] > l3)
            if ok:
                s = self._make_signal(c, third, triple=True, crowd=c["crowd"])
                if s is not None:
                    sig = s
        return sig

    # ------------------------------------------------------------------
    # 内部：信号构造（字段与 research_breakout.analyze_day 一致）
    # ------------------------------------------------------------------

    @staticmethod
    def _make_signal(cand: dict, confirm_bar: dict, triple: bool,
                     crowd: int) -> Optional[dict]:
        """确认时刻 >10:30 ET → None。多空双出（2026-10-25 起：
        站上高点=多头 / 跌破低点=空头，三连路径空头走三连阴镜像）。"""
        hm = _hm(confirm_bar["time"])
        if hm > MAX_HM:
            return None
        st_pmh, st_pml = cand["pmh"], cand["pml"]
        b = cand["break_bar"]
        return {
            "symbol": cand["symbol"],
            "ticker": cand["symbol"].split(":")[-1],
            "side": cand["side"],
            "confirm_hm": hm,
            "confirm_time": _et(confirm_bar["time"]).strftime("%H:%M"),
            "confirm_epoch": confirm_bar["time"],
            "break_time": _et(b["time"]).strftime("%H:%M"),
            "break_epoch": b["time"],
            "close": b["close"],
            "premarket_high": round(st_pmh, 2),
            "premarket_low": round(st_pml, 2),
            "zone_width": round(st_pmh - st_pml, 2),
            "crowd": crowd,
            "triple": triple,
        }

    # ------------------------------------------------------------------
    # 候选入组时补齐区间快照（解析时锁定值，防后续状态变化）
    # ------------------------------------------------------------------

    def _snapshot_zone(self, st: dict, cand: dict) -> None:
        cand["pmh"], cand["pml"] = st["pmh"], st["pml"]
