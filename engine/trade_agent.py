# -*- coding: utf-8 -*-
"""
交易 Agent (TradeAgent)
========================

每根 5M bar 收盘后，读取当前 K 线 + trend_state 指标 + 仓位状态，
按本项目进场规则（trend_state 回踩体系）调 LLM 判读是否入场：
**LLM 只做语义判断（enter/watch + confidence + reason），
入场价/止损/仓位永远代码算**（防幻觉根本设计，移植自 nautilus）。

- 建仓（埋单不追）：enter → place_limit_stop_lot（LMT 父 + STP 子，
  限价 = 线值，止损 = 限价 ∓ 1×ATR）；3 根 bar 未成交自动撤单；
- 保本（只紧不松）：ACTIVE 且浮盈 ≥ 1R → update_trail 移到成本价；
- 硬闸门（代码强制，LLM 无权逾越）：
  枚举纠正 / 每标的每日 enter 上限 2 / enter 必须有代码判定的回踩 /
  trend_state 同向 / confidence 下限 / 已有持仓不重复进场 /
  LLM 故障降级 watch（agent 是增强不是依赖）；
- 决策日志 logs/agent-YYYYMMDD.jsonl（packet + 输出 + token 用量，
  教学数据集 + 费用对账）。
"""

import asyncio
import json
import logging
import time
from collections import deque
from pathlib import Path
from typing import Dict, List, Optional

import config
from engine import agent_llm

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
LOGS_DIR = PROJECT_ROOT / "logs"

RULEBOOK_VERSION = "v1"

SYSTEM_PROMPT = f"""你是趋势回踩交易系统的入场判读员（规则手册 {RULEBOOK_VERSION}）。

【世界观】K 线的运行是突破成功与突破失败的游戏，指标线构成支撑和压力。
单边趋势中，价格回踩支撑线并收回（突破失败=回踩成功）是核心入场点。

【趋势过滤（系统已判定，仅供你参考）】
- trend_state = 1 单边多头 / -1 单边空头 / 0 震荡（震荡不交易）
- 下单方向永远与趋势同向，不存在反向订单

【入场规则（回踩信号）】
- 多头：信号 bar 的 low 刺穿线值（low <= 线值）且 close 收回线上（close > 线值）
- 空头：信号 bar 的 high 刺穿线值（high >= 线值）且 close 收回线下（close < 线值）
- 影线跨线才算回踩；整根在线下方向上顶不算
- 两条入场线：ema_fast（EMA20，浅层）与 channel_edge（SuperTrend±1×ATR，深层）
- packet 中 pullback.ema / pullback.channel 是系统按上述规则的判定结果，
  全部为 false 时绝对没有入场信号

【信号质量判断（你的职责）】
价格、止损、仓位全部由系统决定，你不输出任何数字价格。
你只判断「这个回踩信号值不值得进」，参考：
- 信号 bar 形态：长下影线（多头）/长上影线（空头）、实体收在有利端 = 高质量；
  影线占比（lower_wick_pct/upper_wick_pct）大 = 拒绝有力
- EMA 发散度 divergence 大 = 趋势强；但过大可能末端
- 浅层线（ema_fast）回踩优于深层线（channel_edge）；深层回踩 = 趋势变浅
- 已有持仓（position.state != flat）时不要再进场

【输出契约】只输出一个 JSON 对象，枚举逐字照抄：
{{"action": "enter" 或 "watch", "confidence": 0.0~1.0, "reason": "≤40字中文"}}
- enter = 这个回踩信号值得进场；watch = 不值得/没有信号/不确定
- 拿不准一律 watch。错过不可惜，做错才可惜。"""

# 估算费用（2026-10 官方价，闲时口径，元/百万 token；仅用于前端对账参考）
_PRICE_CNY_PER_M = {
    "deepseek": {"in": 1.0, "out": 4.0},
    "kimi": {"in": 6.5, "out": 27.0},
}


def _bar_marks(bar: dict) -> dict:
    """K 线形态预算（禁止 AI 算数：全部代码算好）。"""
    o, h, l, c = bar.get("open"), bar.get("high"), bar.get("low"), bar.get("close")
    if None in (o, h, l, c) or h <= l:
        return {"body_pct": 0, "lower_wick_pct": 0, "upper_wick_pct": 0}
    rng = h - l
    return {
        "body_pct": round(abs(c - o) / rng, 2),
        "lower_wick_pct": round((min(o, c) - l) / rng, 2),
        "upper_wick_pct": round((h - max(o, c)) / rng, 2),
    }


class TradeAgent:
    """每根 5M bar 判读一次的入场 agent（全自动，LLM 判读 + 代码定价）。"""

    def __init__(self, gateway, order_mgr, risk_gate, collector,
                 record_event=None) -> None:
        self._gw = gateway
        self._om = order_mgr
        self._risk = risk_gate
        self._collector = collector
        self._record_event = record_event or (lambda *a, **k: None)
        self.enabled = bool(config.AGENT_ENABLED)
        self._sem = asyncio.Semaphore(5)          # LLM 并发上限
        self._last_bar_seen: Optional[float] = None
        self._agent_lots: Dict[str, dict] = {}    # lot_id -> 跟踪信息
        self._entries_today: Dict[str, int] = {}  # symbol -> 今日 enter 数
        self._day = time.strftime("%Y%m%d")
        self._decisions: deque = deque(maxlen=200)
        # 费用/用量计数（与成本估算对账）
        self._cnt = {"calls": 0, "enter": 0, "watch": 0, "fallback": 0,
                     "schema_fixed": 0, "prescreen": 0,
                     "tokens_in": 0, "tokens_out": 0}

    # ------------------------------------------------------------------
    # 主循环钩子：新 5M bar 收盘
    # ------------------------------------------------------------------

    async def on_bar_close(self, market: dict, bar_time: float) -> None:
        """每根新收盘 bar：逐标的 packet → LLM → 闸门 → 下单。"""
        if not self.enabled:
            return
        if self._day != time.strftime("%Y%m%d"):
            self._day = time.strftime("%Y%m%d")
            self._entries_today.clear()
            self._agent_lots.clear()
        symbols = config.AGENT_SYMBOLS or sorted((market or {}).keys())
        tasks = [self._judge_symbol(sym, market.get(sym) or {}, bar_time)
                 for sym in symbols if (market or {}).get(sym)]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def manage(self, market: dict, bar_time: float) -> None:
        """每根新收盘 bar：埋单过期撤单 + 浮盈达标移保本。"""
        if not self.enabled or not self._agent_lots:
            return
        for lot_id, info in list(self._agent_lots.items()):
            lot = self._om.get(lot_id)
            if lot is None:
                self._agent_lots.pop(lot_id, None)
                continue
            if lot.state == "PENDING":
                # 埋单过期：N 根 5M bar 未成交 → 撤单（不追价）
                if bar_time - info["created_bar_time"] \
                        >= config.AGENT_ENTRY_EXPIRE_BARS * 300:
                    ok, err = await self._om.cancel_lot(lot_id)
                    logger.info("agent 埋单过期撤单 %s %s: %s",
                                lot.symbol, lot_id, "成功" if ok else err)
                    self._record_event("agent_expire_cancel", lot_id=lot_id,
                                       symbol=lot.symbol, ok=ok, error=err)
                    if ok:
                        self._agent_lots.pop(lot_id, None)
            elif lot.state == "ACTIVE" and not info["breakeven_done"]:
                # 保本：浮盈 ≥ AGENT_BREAKEVEN_R → 止损移到成本（只紧不松）
                close = ((market.get(lot.symbol) or {}).get("bar") or {}).get("close")
                r_dist = info["r_dist"]
                if close and r_dist > 0 and lot.avg_fill > 0:
                    ur = ((close - lot.avg_fill) / r_dist if lot.side == "long"
                          else (lot.avg_fill - close) / r_dist)
                    if ur >= config.AGENT_BREAKEVEN_R:
                        ok, err = await self._om.update_trail(
                            lot_id, round(lot.avg_fill, 2))
                        if ok:
                            info["breakeven_done"] = True
                            logger.info("agent 保本 %s %s: 浮盈 %.2fR，止损移至成本 %.2f",
                                        lot.symbol, lot_id, ur, lot.avg_fill)
                            self._record_event("agent_breakeven", lot_id=lot_id,
                                               symbol=lot.symbol,
                                               unrealized_r=round(ur, 2),
                                               new_stop=lot.avg_fill)
            elif lot.state in ("CLOSED", "CANCELLED", "FAILED"):
                info["final_state"] = lot.state

    # ------------------------------------------------------------------
    # 单标的判读
    # ------------------------------------------------------------------

    async def _judge_symbol(self, symbol: str, data: dict, bar_time: float) -> None:
        ts_c = data.get("ts_c") or data.get("ts") or {}
        trend = ts_c.get("trend_state")
        # 廉价预筛：震荡 = 规则上不可能有信号，不调 LLM（零成本）
        if trend not in (1, -1):
            self._cnt["prescreen"] += 1
            return
        packet = self._build_packet(symbol, data, ts_c, bar_time)
        t0 = time.monotonic()
        try:
            async with self._sem:
                out = await asyncio.to_thread(
                    agent_llm.call_llm, SYSTEM_PROMPT,
                    json.dumps(packet, ensure_ascii=False))
        except Exception as e:
            self._cnt["fallback"] += 1
            logger.warning("agent LLM 故障降级 watch %s: %s", symbol, e)
            self._log_decision(symbol, packet, None, "watch",
                               f"LLM故障降级: {e}", executed=False)
            return
        self._cnt["calls"] += 1
        usage = out.get("_usage") or {}
        self._cnt["tokens_in"] += usage.get("prompt_tokens", 0)
        self._cnt["tokens_out"] += usage.get("completion_tokens", 0)

        schema_fixed = []
        action = out.get("action")
        if action not in ("enter", "watch"):
            schema_fixed.append(f"action:{action!r}->watch")
            self._cnt["schema_fixed"] += 1
            action = "watch"
        try:
            confidence = float(out.get("confidence", 0))
        except (TypeError, ValueError):
            confidence = 0.0
            schema_fixed.append("confidence:非法->0")
        reason = str(out.get("reason", ""))[:60]

        executed = False
        lot_id = None
        veto = self._gate(symbol, action, confidence, packet)
        if veto:
            if veto != reason:
                reason = f"{reason}｜闸门:{veto}" if reason else f"闸门:{veto}"
            action = "watch"
        if action == "enter":
            lot_id = await self._execute(symbol, packet, reason)
            executed = lot_id is not None
        self._cnt["enter" if executed else "watch"] += 1
        self._log_decision(symbol, packet, out, action, reason,
                           executed=executed, lot_id=lot_id,
                           schema_fixed=schema_fixed,
                           latency=round(time.monotonic() - t0, 2))

    # ------------------------------------------------------------------
    # packet / 闸门 / 执行
    # ------------------------------------------------------------------

    def _position_state(self, symbol: str, side: str) -> dict:
        """该标的的仓位状态（agent 自己的 lot + 系统全部 lot）。"""
        lots = [l for l in self._om.list_lots(active_only=True, symbol=symbol)]
        if not lots:
            return {"state": "flat"}
        st = lots[0]
        state = "pending" if st["state"] == "PENDING" else "active"
        out = {"state": state, "side": st["side"], "qty": st.get("filled_qty") or st["qty"],
               "entry": st.get("avg_fill") or st["entry_px"], "stop": st["stop_px"]}
        lid = st["lot_id"]
        info = self._agent_lots.get(lid)
        if info:
            out["breakeven_done"] = info["breakeven_done"]
        return out

    def _build_packet(self, symbol: str, data: dict, ts_c: dict,
                      bar_time: float) -> dict:
        bar_c = data.get("bar_c") or {}
        side = "long" if ts_c.get("trend_state") == 1 else "short"
        low, high = bar_c.get("low"), bar_c.get("high")
        close = bar_c.get("close")
        pullback = {"ema": False, "channel": False}
        lines = {}
        touched_lines = []
        for key, name in (("ema_fast", "ema"), ("channel_edge", "channel")):
            line = ts_c.get(key)
            lines[key] = round(line, 4) if line else None
            if not line or low is None or high is None or close is None:
                continue
            hit = (low <= line and close > line) if side == "long" \
                else (high >= line and close < line)
            pullback[name] = bool(hit)
            if hit:
                touched_lines.append((name, line))
        lines["supertrend"] = ts_c.get("supertrend")
        lines["atr"] = ts_c.get("atr")
        # 入场线选择：多头取被触线中更优（更低）价，空头取更高价
        entry_line, entry_px = None, None
        if touched_lines:
            entry_line, entry_px = (min(touched_lines, key=lambda x: x[1])
                                    if side == "long"
                                    else max(touched_lines, key=lambda x: x[1]))
        atr = ts_c.get("atr") or 0
        sign = 1 if side == "long" else -1
        return {
            "symbol": symbol,
            "side": side,
            "trend_state": ts_c.get("trend_state"),
            "bar": {**{k: bar_c.get(k) for k in
                       ("time", "open", "high", "low", "close", "volume")},
                    **_bar_marks(bar_c)},
            "lines": lines,
            "pullback": pullback,
            "position": self._position_state(symbol, side),
            "entry_plan": ({"line": entry_line, "entry": round(entry_px, 2),
                            "stop": round(entry_px - sign * atr, 2)}
                           if entry_px and atr > 0 else None),
            "rulebook_version": RULEBOOK_VERSION,
        }

    def _gate(self, symbol: str, action: str, confidence: float,
              packet: dict) -> Optional[str]:
        """硬闸门：返回 None 放行，否则返回拦截原因（强制降级 watch）。"""
        if action != "enter":
            return None
        pb = packet.get("pullback") or {}
        if not (pb.get("ema") or pb.get("channel")):
            return "无代码判定的回踩（LLM 不能凭空进场）"
        if self._entries_today.get(symbol, 0) >= config.AGENT_MAX_ENTRIES_PER_SYMBOL_DAY:
            return f"超每标的每日上限 {config.AGENT_MAX_ENTRIES_PER_SYMBOL_DAY}"
        if confidence < config.AGENT_MIN_CONFIDENCE:
            return f"confidence {confidence} 低于下限 {config.AGENT_MIN_CONFIDENCE}"
        pos = packet.get("position") or {}
        if pos.get("state") != "flat":
            return "已有持仓/挂单，不重复进场"
        plan = packet.get("entry_plan")
        if not plan or plan.get("entry", 0) <= 0 or plan.get("stop", 0) <= 0:
            return "入场计划无效（缺线值/ATR）"
        return None

    async def _execute(self, symbol: str, packet: dict, reason: str) -> Optional[str]:
        plan = packet["entry_plan"]
        side = packet["side"]
        equity = await self._gw.net_liquidation()
        qty = self._risk.size_position(plan["entry"], plan["stop"], equity)
        if qty <= 0:
            logger.warning("agent 以损定量为 0 %s，放弃", symbol)
            return None
        lot, err = await self._om.place_limit_stop_lot(
            symbol=symbol, side=side, qty=qty,
            limit_px=plan["entry"], stop_px=plan["stop"],
            limit_type=f"agent_{plan['line']}",
            reason=f"agent: {reason}"[:80],
            risk_usd=0,
            signal_snapshot={"agent_packet": packet})
        if lot is None:
            logger.warning("agent 下单失败 %s: %s", symbol, err)
            self._record_event("agent_order_failed", symbol=symbol,
                               side=side, error=err)
            return None
        self._entries_today[symbol] = self._entries_today.get(symbol, 0) + 1
        self._agent_lots[lot.lot_id] = {
            "symbol": symbol, "side": side,
            "created_bar_time": packet["bar"].get("time") or 0,
            "r_dist": abs(plan["entry"] - plan["stop"]),
            "breakeven_done": False,
        }
        logger.info("agent 建仓 %s %s %s线 x%d @%.2f 损 %.2f | %s",
                    symbol, side, plan["line"], qty, plan["entry"],
                    plan["stop"], reason)
        self._record_event("agent_order_placed", lot_id=lot.lot_id,
                           symbol=symbol, side=side, qty=qty,
                           limit_px=plan["entry"], stop_px=plan["stop"],
                           line=plan["line"], reason=reason)
        return lot.lot_id

    # ------------------------------------------------------------------
    # 决策日志 / 统计
    # ------------------------------------------------------------------

    def _log_decision(self, symbol: str, packet: dict, out: Optional[dict],
                      action: str, reason: str, executed: bool,
                      lot_id: Optional[str] = None,
                      schema_fixed: Optional[list] = None,
                      latency: float = 0.0) -> None:
        rec = {
            "ts": time.time(),
            "symbol": symbol,
            "action": action,
            "reason": reason,
            "executed": executed,
            "lot_id": lot_id,
            "schema_fixed": schema_fixed or [],
            "latency_s": latency,
            "packet": packet,
        }
        if out:
            rec["output"] = {k: v for k, v in out.items() if not k.startswith("_")}
            rec["model"] = out.get("_model")
            rec["usage"] = out.get("_usage")
            if out.get("_reasoning"):
                rec["reasoning"] = out["_reasoning"]
        self._decisions.append(rec)
        try:
            path = LOGS_DIR / f"agent-{time.strftime('%Y%m%d')}.jsonl"
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except OSError as e:
            logger.warning("agent 决策日志写入失败: %s", e)

    def stats(self) -> dict:
        usage_in, usage_out = self._cnt["tokens_in"], self._cnt["tokens_out"]
        backend = None
        try:
            backend = __import__("os").environ.get("LLM_BACKEND", "deepseek")
        except Exception:
            backend = "deepseek"
        price = _PRICE_CNY_PER_M.get(backend, _PRICE_CNY_PER_M["deepseek"])
        cost_cny = round(usage_in * price["in"] / 1e6
                         + usage_out * price["out"] / 1e6, 4)
        # 交易统计：agent lot 的 MFE_R / 保本 / 胜率
        closed, wins, breakevens, mfes = 0, 0, 0, []
        for lot_id, info in self._agent_lots.items():
            lot = self._om.get(lot_id)
            r = info["r_dist"]
            if info["breakeven_done"]:
                breakevens += 1
            if lot is not None and r > 0 and lot.mfe > 0:
                mfes.append(round(lot.mfe / (r * max(lot.filled_qty, 1)), 2))
            if lot is not None and lot.state == "CLOSED":
                closed += 1
                if lot.realized_pnl > 0:
                    wins += 1
        return {
            "enabled": self.enabled,
            "backend": backend,
            "day": self._day,
            **{k: self._cnt[k] for k in
               ("calls", "enter", "watch", "fallback", "schema_fixed", "prescreen")},
            "tokens_in": usage_in,
            "tokens_out": usage_out,
            "cost_cny_est": cost_cny,
            "agent_lots": len(self._agent_lots),
            "closed": closed,
            "win_rate": round(wins / closed * 100, 1) if closed else 0.0,
            "breakevens": breakevens,
            "avg_mfe_r": round(sum(mfes) / len(mfes), 2) if mfes else 0.0,
        }

    def decisions(self, limit: int = 50) -> List[dict]:
        out = []
        for rec in list(self._decisions)[-limit:]:
            p = rec.get("packet") or {}
            out.append({
                "ts": rec["ts"], "symbol": rec["symbol"],
                "side": p.get("side"), "action": rec["action"],
                "confidence": (rec.get("output") or {}).get("confidence"),
                "reason": rec["reason"], "executed": rec["executed"],
                "lot_id": rec.get("lot_id"), "model": rec.get("model"),
                "latency_s": rec.get("latency_s"),
                "schema_fixed": rec.get("schema_fixed"),
            })
        out.reverse()
        return out
