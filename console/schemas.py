"""HTTP request contracts, preserving existing defaults and validation."""

from pydantic import BaseModel
from pydantic import Field
from typing import List
from typing import Optional


class UpdateTrailRequest(BaseModel):
    stop_px: float = Field(..., gt=0)


class LimitStopOrderRequest(BaseModel):
    symbol: str = Field(..., description="如 NVDA / NASDAQ:NVDA")
    side: str = "long"
    qty: int = 0
    limit_px: float = Field(..., gt=0)
    stop_px: float = Field(..., gt=0)
    limit_type: str = "ema"
    reason: str = ""
    risk_usd: float = 0
    max_position_pct: float = 0
    force: bool = False
    twap_batches: int = 0
    twap_interval_s: int = 60


class ClientRegisterRequest(BaseModel):
    name: str = Field(..., description="交易员名称（1-20 字符，无特殊符号）")


class CopyServerCreateRequest(BaseModel):
    """新增跟单服务器（client_id=0 = 自动分配，13 起）。"""

    name: str = Field(..., description="服务器名（1-10 字符，唯一）")
    master_ws: str = "ws://127.0.0.1:8000/ws"
    host: str = "127.0.0.1"
    port: int = 7497
    client_id: int = 0
    account_id: str = ""
    fa_group: str = ""
    enabled: bool = False
    size_mode: str = "risk"
    risk_usd: float = 300
    size_multiplier: float = 0.1
    max_drawdown_pct: float = 3.0
    direction: str = "both"
    copy_existing_on_start: bool = False


class CopyServerUpdateRequest(BaseModel):
    """改参数：只更新显式传入的字段（None = 不动）；name/enabled 走专用端点。"""

    master_ws: Optional[str] = None
    host: Optional[str] = None
    port: Optional[int] = None
    client_id: Optional[int] = None
    account_id: Optional[str] = None
    fa_group: Optional[str] = None
    size_mode: Optional[str] = None
    risk_usd: Optional[float] = None
    size_multiplier: Optional[float] = None
    max_drawdown_pct: Optional[float] = None
    direction: Optional[str] = None
    copy_existing_on_start: Optional[bool] = None


class CopyCmdRequest(BaseModel):
    """人工指令（合并进 logs/copy-{name}-cmd.json，不覆盖已有指令）。"""

    retry: List[str] = Field(default_factory=list)
    dismiss: List[str] = Field(default_factory=list)
    unhalt: bool = False


class SettingsRequest(BaseModel):
    stop_atr_mult: Optional[float] = Field(None, description="止损 ATR 倍数（0.2~5.0）")
    screen_premarket_filter: Optional[bool] = Field(
        None, description="选股盘前区间过滤开关"
    )
    fvg_premarket_filter: Optional[bool] = Field(
        None, description="FVG 监控盘前区间过滤开关"
    )
    imessage_recipient: Optional[str] = Field(
        None, description="iMessage 告警收件人（空串=清除，未配置跳过该通道）"
    )
    pyramid_auto_breakeven: Optional[bool] = Field(
        None, description="金字塔自动保本开关"
    )
    pyramid_breakeven_r: Optional[float] = Field(
        None, description="金字塔自动保本 R 门槛"
    )
    pyramid_add_unlock_r: Optional[float] = Field(
        None, description="金字塔加仓解锁 R 门槛"
    )


class TvShowRequest(BaseModel):
    symbol: str = Field(..., description="如 NASDAQ:AAPL")


class AutoStartRequest(BaseModel):
    instance_id: str = "default"
    strategy: str = "ema9_pyramid"
    symbols: List[str] = Field(
        default_factory=list, description="空 = 全 WATCHLIST（受实例上限约束）"
    )
    params: dict = Field(
        default_factory=dict, description="initial_risk_usd=当前单笔资金"
    )


class AutoAdoptRequest(BaseModel):
    strategy: str = "ema9_pyramid"
    instance_id: str = "default"
    symbol: str
    lot_id: str
    initial_risk_usd: float = 0


class AutoRejectRequest(BaseModel):
    reason: str = ""


class BtLoadRequest(BaseModel):
    symbol: str = Field(..., description="如 NVDA / NASDAQ:NVDA")
    day: str = ""
    strategy: str = "ema9_pyramid"
    params: dict = Field(default_factory=dict)
    bars: int = 500


class ReportSourceAddRequest(BaseModel):
    url: str = Field(..., description="YouTube 链接（watch?v=/youtu.be//shorts/）")
