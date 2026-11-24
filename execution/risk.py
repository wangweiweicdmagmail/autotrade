# -*- coding: utf-8 -*-
"""
风控硬性限额 (RiskGate)
=======================

实盘唯一兜底（验证路径跳过 paper 直接实盘小仓位），
第一笔真实订单前必须就位。所有开仓请求必须先过 RiskGate。

规则（参数集中在 config.py 的 RISK_* 段）：
1. 以损定量：qty = RISK_PER_TRADE_USD / (entry - stop)，再被
   RISK_MAX_ORDER_QTY 封顶；
2. 同时持仓数 <= RISK_MAX_OPEN_POSITIONS；
3. 总风险敞口预算：全部开放 lot 剩余风险（PENDING 全额计划风险，
   已成交按止损到成本的不利距离，保本后归 0）+ 本笔计划风险
   > RISK_MAX_TOTAL_RISK_USD 时拦截（0 = 不限制）；
4. 日亏损熔断：当日累计盈亏 <= -RISK_DAILY_LOSS_LIMIT_USD 后，
   当日不再开新仓（halt 状态落盘 logs/halt-YYYYMMDD，重启不丢）；
5. 止损价合法性：多仓 stop < entry、空仓 stop > entry，且距离 > 0。
6. 生产禁止多空并存，同向可多笔/同股加仓；仅未结束及受理未知批次保留方向。

参考 nautilus_ibkr_helloworld 的分层风控（时段/熔断/冷却/以损定量），
v1 先实现硬性限额，冷却/相关组等后续补。
"""

import logging
import math
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
HALT_DIR = PROJECT_ROOT / "logs"


@dataclass(frozen=True)
class RiskConfig:
    per_trade_pct: float = 0.001      # 单笔风险 = 账户净值 × 0.1%
    per_trade_usd: float = 200.0      # 净值未知时的降级固定额
    max_order_qty: int = 200
    max_open_positions: int = 3
    max_total_risk_usd: float = 1500.0  # 总风险敞口预算（0 = 不限制）
    daily_loss_limit_usd: float = 1000.0
    daily_loss_halt_enabled: bool = False  # 日亏损熔断开关（默认关闭）
    max_direction_risk_usd: float = 0.0  # 兼容离线配置；生产从 config 注入
    max_direction_positions: int = 0  # 独立笔数限制；当前生产为0，不限制同向笔数
    single_direction_only: bool = False  # 生产从config注入True；离线可显式使用旧配置


class RiskGate:
    """开仓前的硬性风控闸门。全部方法纯内存 + halt 标志落盘。"""

    def __init__(self, cfg: RiskConfig, halt_dir: Path = HALT_DIR) -> None:
        self.cfg = cfg
        self._halt_dir = halt_dir

    # ------------------------------------------------------------------
    # 以损定量
    # ------------------------------------------------------------------

    def size_position(self, entry: float, stop: float,
                      equity: Optional[float] = None) -> int:
        """
        qty = floor(风险额 / |entry - stop|)，风险额 = 净值 × per_trade_pct
        （equity 为 None 时用 per_trade_usd 固定额）。返回 0 = 距离非法。
        """
        dist = abs(entry - stop)
        if dist <= 0:
            return 0
        risk_usd = (equity * self.cfg.per_trade_pct
                    if equity else self.cfg.per_trade_usd)
        qty = math.floor(risk_usd / dist)
        return min(qty, self.cfg.max_order_qty)

    # ------------------------------------------------------------------
    # 开仓检查
    # ------------------------------------------------------------------

    def check_new_order(self, symbol: str, side: str, entry: float,
                        stop: float, qty: int,
                        open_positions: int,
                        daily_pnl: Optional[float] = None,
                        risk_usd: float = 0,
                        open_risk_usd: float = 0,
                        risk_ref_px: float = 0,
                        open_direction_risk_usd: float = 0,
                        open_direction_positions: Optional[int] = None,
                        open_opposite_positions: Optional[int] = None) -> Tuple[bool, str]:
        """
        返回 (是否放行, 原因)。daily_pnl 为当日累计盈亏（实时+已实现），
        由调用方从 IBKR 账户摘要取得；None = 取不到（FA 口径未解决时），
        熔断检查跳过但每次显式告警（不静默失效）。
        risk_usd 为设定风险金额：实际风险（qty×|ref−stop|）超出其 10% 则拦截。
        ref = risk_ref_px（>0 时，预期成交价）或 entry；marketable 单的溢价
        限价只做成交保护，不进风险口径（BA 实锤：0.5% 溢价≈止损距离被误计）。
        open_risk_usd 为现有全部开放 lot 的总风险敞口（OrderManager.risk_exposure），
        与本笔计划风险相加超过 max_total_risk_usd 预算时拦截（0 = 不限制）。
        open_direction_positions 是同向未结束批次数，包含预约意图、挂单、
        持仓和平仓中；不因保本或风险金额归零而释放名额。
        single_direction_only开启时，相反方向必须没有未结束批次；
        同方向不因该开关限制笔数，同股加仓仍受风险金额和合计仓位约束。
        """
        if self.is_halted():
            return False, "日亏损熔断中，今日不再开新仓"
        if self.cfg.daily_loss_halt_enabled and daily_pnl is not None:
            if daily_pnl <= -self.cfg.daily_loss_limit_usd:
                self.halt(f"daily_pnl={daily_pnl:.2f} 触发熔断")
                return False, f"日亏损 {daily_pnl:.2f} 达到熔断线，已熔断"
        if self.cfg.max_open_positions > 0 \
                and open_positions >= self.cfg.max_open_positions:
            return False, (f"持仓数 {open_positions} 达到上限 "
                           f"{self.cfg.max_open_positions}")   # 0 = 不限制
        if self.cfg.single_direction_only:
            if type(open_opposite_positions) is not int or open_opposite_positions < 0:
                return False, "现有订单方向未确认，禁止新开仓"
            if open_opposite_positions > 0:
                existing, requested = ("空头", "多头") if side == "long" else ("多头", "空头")
                return False, (f"已有{existing}{open_opposite_positions}笔未结束订单，不能新开{requested}；"
                               "不允许多空并存，原方向挂单及持仓全部结束后才可换向")
        if self.cfg.max_direction_positions > 0:
            direction = "多头" if side == "long" else "空头"
            if type(open_direction_positions) is not int or open_direction_positions < 0:
                return False, f"{direction}持仓笔数未确认，禁止新开仓"
            if open_direction_positions >= self.cfg.max_direction_positions:
                return False, (f"{direction}已有{open_direction_positions}笔未结束订单，"
                               f"单方向最多{self.cfg.max_direction_positions}笔"
                               "（挂单、持仓、平仓中均占位，保本不释放）")
        if qty <= 0:
            return False, "qty <= 0"
        if stop <= 0 or entry <= 0:
            return False, f"价格非法: entry={entry} stop={stop}（止损必须为正）"
        if qty > self.cfg.max_order_qty:
            return False, f"qty {qty} 超过单笔硬顶 {self.cfg.max_order_qty}"
        if side == "long" and not stop < entry:
            return False, f"多仓止损 {stop} 必须低于入场 {entry}"
        if side == "short" and not stop > entry:
            return False, f"空仓止损 {stop} 必须高于入场 {entry}"
        # 风险偏差容差：实际风险 ≤ 设定风险 × 1.10（允许 10% 上浮）
        ref = risk_ref_px if risk_ref_px > 0 else entry
        if risk_usd and risk_usd > 0:
            actual = qty * abs(ref - stop)
            if actual > risk_usd * 1.10:
                return False, (f"实际风险 {actual:.2f} 超出设定风险 "
                               f"{risk_usd:.2f} 的 10% 容差")
        # 总风险敞口预算：现有敞口 + 本笔计划风险 ≤ 预算（0 = 不限制）
        if self.cfg.max_total_risk_usd > 0:
            planned = qty * abs(ref - stop)
            if open_risk_usd + planned > self.cfg.max_total_risk_usd:
                return False, (f"总风险敞口 {open_risk_usd:.0f}+{planned:.0f}="
                               f"{open_risk_usd + planned:.0f} 超预算 "
                               f"{self.cfg.max_total_risk_usd:.0f}")
        if self.cfg.max_direction_risk_usd > 0:
            planned = qty * abs(ref - stop)
            if open_direction_risk_usd + planned > self.cfg.max_direction_risk_usd + 1e-8:
                direction = "多头" if side == "long" else "空头"
                return False, (f"{direction}风险敞口 {open_direction_risk_usd:.0f}+{planned:.0f}="
                               f"{open_direction_risk_usd + planned:.0f} 超预算 "
                               f"{self.cfg.max_direction_risk_usd:.0f}")
        return True, "ok"

    # ------------------------------------------------------------------
    # 熔断状态（落盘防重启绕过，按自然日生效）
    # ------------------------------------------------------------------

    def _halt_file(self) -> Path:
        return self._halt_dir / f"halt-{time.strftime('%Y%m%d')}"

    def is_halted(self) -> bool:
        return self._halt_file().exists()

    def halt(self, reason: str) -> None:
        try:
            self._halt_dir.mkdir(parents=True, exist_ok=True)
            self._halt_file().write_text(
                f"{time.strftime('%Y-%m-%d %H:%M:%S')} {reason}\n",
                encoding="utf-8")
        except OSError:
            logger.exception("熔断标志落盘失败")
        logger.error("🚨 风控熔断: %s", reason)

    def reset_halt(self) -> bool:
        """人工解除当日熔断（谨慎使用）。"""
        try:
            self._halt_file().unlink(missing_ok=True)
            return True
        except OSError:
            return False
