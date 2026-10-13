# -*- coding: utf-8 -*-
"""
提案管理器 (ProposalManager)
============================

信号 → 提案卡 → 人工决策 的状态管理核心。

职责：
1. **跳变检测**：pull 路线读到的是信号电平（每根 bar 一个值），
   只有 0 → ±1 的跳变才构成一次新信号，进行中 bar 不处理。
2. **提案呈现**：新信号经风险模块补全参数后，调控制器批量弹卡。
3. **决策消费**：轮询人工决策——
   - approve: 进 approved 队列（等 execution 层消费），同向信号抑制；
   - reject:  记录并抑制——同一 symbol+side 在信号归零前不再弹卡，
              防止拒绝过的信号反复打扰。
4. **过期作废**：提案只在产生它的那根 bar 有效期内存活，
   下一根 bar 收盘时未决策的提案自动撤卡（expired，不抑制——
   人只是没看到，不是否定了信号）。

决策日志：logs/decisions.jsonl，每行一条 JSON，用于复盘信号有效性。
"""

import inspect
import json
import logging
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_LOG = PROJECT_ROOT / "logs" / "decisions.jsonl"


@dataclass
class Proposal:
    """一张交易提案卡对应的完整数据。"""
    symbol: str
    side: str            # 'long' | 'short'
    entry: float
    stop: float
    qty: int
    risk: float
    trail_amount: float = 0.0       # TRAIL 跟踪幅度（美元，2×ATR）
    tp_px: Optional[float] = None   # 止盈价（v2 起 TRAIL 兼任止盈，不再单独挂）
    reason: str = ""
    bar_time: float = 0  # 产生信号的 bar 收盘时间戳
    id: str = ""

    @property
    def key(self) -> str:
        return f"{self.symbol}|{self.side}"

    def to_card(self) -> dict:
        return {
            "id": self.id, "symbol": self.symbol, "side": self.side,
            "entry": self.entry, "stop": self.stop, "qty": self.qty,
            "risk": self.risk, "reason": self.reason,
        }


class ProposalManager:
    """
    用法::

        mgr = ProposalManager(controller)
        # 每根 bar 收盘：
        signals = {"NASDAQ:QQQ": 1, "NASDAQ:MU": 0, ...}   # 已收盘 bar 的信号值
        await mgr.on_bar_close(signals, enrich_fn, bar_time)
        # 盘中高频轮询人工决策：
        approved = await mgr.poll()   # -> 批准的提案列表（含人改过的参数）
    """

    def __init__(self, controller, log_path: Path = DEFAULT_LOG,
                 on_decision=None, on_present=None) -> None:
        """
        on_decision(outcome, proposal, raw): 每个决策被消费的**瞬间**触发
            （同步/异步函数均可），用于实时输出/通知，例如::

                def on_decision(outcome, p, raw):
                    print(f"[{outcome}] {p.symbol} {p.side} qty={p.qty}")

        on_present(proposals): 每批新提案弹卡后触发。
        """
        self._ctl = controller
        self._log_path = Path(log_path)
        self._on_decision = on_decision
        self._on_present = on_present
        self._active: Dict[str, Proposal] = {}      # symbol -> 在屏提案
        self._suppressed: Dict[str, float] = {}     # symbol|side -> 抑制起始时间
        self._last_signal: Dict[str, int] = {}      # symbol -> -1/0/1
        self._counter = 0

    @staticmethod
    async def _emit(cb, *args) -> None:
        """触发回调，同步/异步皆可；回调异常不影响主流程。"""
        if cb is None:
            return
        try:
            r = cb(*args)
            if inspect.isawaitable(r):
                await r
        except Exception:
            logger.exception("回调执行异常")

    # ------------------------------------------------------------------
    # 跳变检测与抑制
    # ------------------------------------------------------------------

    def detect_edges(self, signals: Dict[str, int]) -> List[Tuple[str, str]]:
        """
        检测 0→±1 跳变，返回 [(symbol, side), ...]。
        信号归零时解除该 symbol 两个方向的抑制（新一轮周期开始）。
        首次见到的 symbol 只记录不触发——当前值不是跳变
        （否则进程重启会把所有在趋势里的股票误报成新信号）。
        """
        fired: List[Tuple[str, str]] = []
        for sym, val in signals.items():
            val = 1 if val > 0 else (-1 if val < 0 else 0)
            if sym not in self._last_signal:
                self._last_signal[sym] = val
                continue
            prev = self._last_signal[sym]
            if val == 0 and prev != 0:
                self._suppressed.pop(f"{sym}|long", None)
                self._suppressed.pop(f"{sym}|short", None)
                logger.info("%s 信号归零，解除抑制", sym)
            elif val != 0 and prev == 0:
                fired.append((sym, "long" if val > 0 else "short"))
            self._last_signal[sym] = val
        return fired

    def is_suppressed(self, symbol: str, side: str) -> bool:
        return f"{symbol}|{side}" in self._suppressed

    # ------------------------------------------------------------------
    # bar 收盘：过期旧提案 + 呈现新提案
    # ------------------------------------------------------------------

    async def on_bar_close(self, proposals: List[Proposal],
                           bar_time: Optional[float] = None) -> List[Proposal]:
        """
        每根已收盘 bar 调用一次。
        proposals 为上游（策略+风控）为本 bar 新信号生成的完整提案。
        返回实际弹卡的提案（被抑制/在屏的已剔除）。
        """
        bar_time = bar_time or time.time()
        # 1) 上一 bar 的未决策提案全部过期作废
        await self._expire_active(bar_time)
        # 2) 过滤：在屏的（理论不会出现，已过期）和被抑制的
        fresh: List[Proposal] = []
        for p in proposals:
            p.bar_time = bar_time
            if p.symbol in self._active:
                logger.info("%s 提案已在屏，跳过", p.symbol)
                continue
            if self.is_suppressed(p.symbol, p.side):
                logger.info("%s %s 已被人工处理过，信号归零前不再弹卡",
                            p.symbol, p.side)
                continue
            if not p.id:
                self._counter += 1
                p.id = f"pp{int(bar_time)}x{self._counter}"
            fresh.append(p)
        # 3) 弹卡
        if fresh:
            await self._ctl.show_proposals([p.to_card() for p in fresh])
            for p in fresh:
                self._active[p.symbol] = p
            logger.info("本 bar 新提案 %d 张: %s",
                        len(fresh), ", ".join(p.symbol for p in fresh))
            await self._emit(self._on_present, fresh)
        return fresh

    async def _expire_active(self, bar_time: float) -> None:
        if not self._active:
            return
        for p in self._active.values():
            self._log("expired", p, {})
            logger.info("%s 提案过期未决策，撤卡", p.symbol)
        self._active.clear()
        await self._ctl.clear_proposals()

    # ------------------------------------------------------------------
    # 盘中：消费人工决策
    # ------------------------------------------------------------------

    async def poll(self) -> List[Proposal]:
        """
        轮询人工决策，返回**批准的**提案列表（含人修改后的 qty/stop）。
        拒绝的记录并抑制；批准进 approved 语义由调用方（execution 层）处理。
        """
        decisions = await self._ctl.poll_decisions()
        approved: List[Proposal] = []
        for d in decisions:
            # 卡片 id 反查提案；找不到说明是过期卡的迟到的点击，忽略
            p = next((x for x in self._active.values() if x.id == d.get("id")),
                     None)
            if p is None:
                logger.warning("收到未知提案 %s 的决策，忽略", d.get("id"))
                continue
            self._active.pop(p.symbol, None)
            self._suppressed[p.key] = time.time()
            changes = {"qty": d.get("qty"), "stop": d.get("stop")}
            if d.get("action") == "approve":
                p.qty = int(d.get("qty") or p.qty)
                p.stop = float(d.get("stop") or p.stop)
                self._log("approved", p, changes)
                approved.append(p)
                logger.info("✅ %s %s 已批准: qty=%d stop=%.2f",
                            p.symbol, p.side, p.qty, p.stop)
                await self._emit(self._on_decision, "approved", p, d)
            else:
                self._log("rejected", p, changes)
                logger.info("❌ %s %s 已拒绝，信号归零前不再提案",
                            p.symbol, p.side)
                await self._emit(self._on_decision, "rejected", p, d)
        return approved

    # ------------------------------------------------------------------
    # 日志
    # ------------------------------------------------------------------

    def _log(self, outcome: str, p: Proposal, extra: dict) -> None:
        rec = {
            "ts": time.time(), "outcome": outcome, **asdict(p), **extra,
        }
        try:
            self._log_path.parent.mkdir(parents=True, exist_ok=True)
            with self._log_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except OSError:
            logger.exception("决策日志写入失败")

    # ------------------------------------------------------------------
    # 状态查询（调试/监控用）
    # ------------------------------------------------------------------

    @property
    def active(self) -> Dict[str, Proposal]:
        return dict(self._active)

    @property
    def suppressed(self) -> Dict[str, float]:
        return dict(self._suppressed)
