"""HTTP request contracts, preserving existing defaults and validation."""

from pydantic import BaseModel, root_validator, StrictBool, conint
from pydantic import Field
from typing import List, Literal
from typing import Optional


class UpdateTrailRequest(BaseModel):
    stop_px: float = Field(..., gt=0)


class IntelligentExitOptions(BaseModel):
    version: Literal[1] = 1
    enabled: StrictBool = False
    breakeven_minutes: conint(strict=True, ge=1, le=120) = 5

    class Config:
        extra = 'forbid'


class CandidateProtectionOptions(BaseModel):
    version: Literal[1] = 1
    timed_breakeven_enabled: StrictBool = True
    breakeven_minutes: conint(strict=True, ge=1, le=120) = 5
    trailing_enabled: StrictBool = False
    trailing_source: Literal["ema9_5m", "st_1m_10_3"] = "ema9_5m"

    class Config:
        extra = 'forbid'


class CandidateOrderOptions(CandidateProtectionOptions):
    vwap_outside_only: StrictBool = True


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
    market_direction_mode: Literal["st", "manual", "candidate"] = "st"
    manual_stop_mode: Literal["atr_2", "vwap_sigma"] = "atr_2"
    candidate_ranking: Literal["trend"] = "trend"
    candidate_view: Literal["mixed", "long", "short"] = "mixed"
    vwap_limit_level: Literal["vwap", "upper", "lower"] = "vwap"
    range_direction_confirmed: bool = False
    entry_mode: Literal["default", "rhythm_atr", "rhythm_sigma"] = "default"
    candidate_order: Optional[CandidateOrderOptions] = None
    twap_batches: int = 0
    twap_interval_s: int = 60


class OpeningBreakoutOrderRequest(BaseModel):
    signal_id: str = Field(..., min_length=1, max_length=160)
    max_position_pct: float = Field(0, ge=0, le=200)


class OpeningBreakoutPrepareRequest(OpeningBreakoutOrderRequest):
    side: Literal["long", "short"]
    cancel: bool = False


class OpeningVwapPrepareRequest(BaseModel):
    signal_id: str = Field(min_length=1, max_length=160)
    side: Literal["long", "short"]
    cancel: bool = False


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
    intelligent_exit: Optional[IntelligentExitOptions] = None
    candidate_order_protection: Optional[CandidateProtectionOptions] = None
    stop_atr_mult: Optional[float] = Field(None, description="止损 ATR 倍数（0.2~5.0）")
    screen_premarket_filter: Optional[bool] = Field(
        None, description="选股盘前区间过滤开关"
    )
    screen_opening_range_filter: Optional[bool] = Field(None, description="选股小区间过滤开关")
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


class TvViewRequest(TvShowRequest):
    source_token: str | None = Field(default=None, pattern=r"^[a-f0-9]{32}$")


class TvGridRequest(BaseModel):
    symbols: List[str] = Field(min_items=4, max_items=8)
    source_token: str = Field(pattern=r"^[a-f0-9]{32}$")
    layout: Literal['candidates', 'focus'] = 'candidates'

    @root_validator(skip_on_failure=True)
    def check_layout_size(cls, values):
        if len(values['symbols']) != (6 if values['layout'] == 'focus' else 8):
            raise ValueError('focus需要6个标的，candidates需要8个标的')
        return values


class TvMarkerRequest(BaseModel):
    source_token: str = Field(pattern=r"^[a-f0-9]{32}$")


class TvBindRequest(TvMarkerRequest):
    target_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,80}$")


class GoldenEntryRequest(BaseModel):
    symbol: str = Field(min_length=1, max_length=64)
    bar_time: int = Field(gt=0)
    direction: int = Field(ge=-1, le=1)
    manual_window: bool = False


class AutoStartRequest(BaseModel):
    confirmed_directions: dict[str, int] = Field(default_factory=dict)
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


class AutoInstanceBreakevenRequest(BaseModel):
    group_id: str = Field(..., min_length=1, max_length=128)
    qty: int = Field(..., gt=0, strict=True)
    avg_cost: float = Field(..., gt=0, allow_inf_nan=False)


class AutoRejectRequest(BaseModel):
    reason: str = ""


class BtLoadRequest(BaseModel):
    symbol: str = Field(..., description="如 NVDA / NASDAQ:NVDA")
    day: str = ""
    strategy: str = "ema9_pyramid"
    params: dict = Field(default_factory=dict)
    bars: int = 500


class BtRunRequest(BtLoadRequest):
    """A complete, request-local execution backtest with a manual start time."""

    start_time: str = Field(default="09:45", **{
        ("pattern" if hasattr(BaseModel, "model_validate") else "regex"): r"^(?:[01]\d|2[0-3]):[0-5]\d$"
    })
    bars: int = Field(default=500, ge=1, le=5000)


class ReportSourceAddRequest(BaseModel):
    url: str = Field(..., description="YouTube 链接（watch?v=/youtu.be//shorts/）")
