"""Request/response models. The webhook payload is validated strictly: unknown top-level keys are
rejected so a typo in a TradingView alert fails loudly instead of being silently ignored."""
from datetime import datetime
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class AssetClass(str, Enum):
    stock = "stock"
    option = "option"
    future = "future"
    crypto = "crypto"


class OptionContract(BaseModel):
    """Supplied option-contract data. Nothing here is ever filled in by the system."""
    model_config = ConfigDict(extra="forbid")
    underlying: str = Field(min_length=1, max_length=20)
    right: Literal["call", "put"]
    strike: float = Field(gt=0)
    expiry: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    bid: float | None = Field(default=None, ge=0)
    ask: float | None = Field(default=None, ge=0)
    last: float | None = Field(default=None, ge=0)
    iv: float | None = Field(default=None, ge=0, description="implied volatility, e.g. 0.32")
    delta: float | None = Field(default=None, ge=-1, le=1)
    gamma: float | None = None
    theta: float | None = None
    vega: float | None = None
    open_interest: int | None = Field(default=None, ge=0)
    volume: int | None = Field(default=None, ge=0)
    multiplier: float | None = Field(default=None, gt=0, description="contract multiplier; required to size")


class Structure(BaseModel):
    """Market-structure observations computed by your Pine script (the system can't see the chart)."""
    model_config = ConfigDict(extra="forbid")
    support: list[float] = Field(default_factory=list, max_length=10)
    resistance: list[float] = Field(default_factory=list, max_length=10)
    trend: Literal["up", "down", "range"] | None = None
    bos: Literal["bullish", "bearish"] | None = None            # break of structure
    choch: Literal["bullish", "bearish"] | None = None          # change of character
    liquidity_sweep: Literal["above_high", "below_low"] | None = None
    fvg: list[dict[str, float]] = Field(default_factory=list, max_length=10)  # [{"low":..,"high":..}]
    swing_high: float | None = None
    swing_low: float | None = None


class TradingViewAlert(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    secret: str | None = Field(default=None, exclude=True, repr=False)  # TradingView can't set headers
    ticker: str = Field(min_length=1, max_length=40, pattern=r"^[A-Za-z0-9_.:!/\-]+$")
    exchange: str | None = Field(default=None, max_length=40)
    asset_class: AssetClass | None = None
    timeframe: str = Field(min_length=1, max_length=8)
    strategy: Literal["scalping", "day", "swing"] | None = None
    signal: str | None = Field(default=None, max_length=60)      # e.g. "rsi_bounce_long"
    price: float | None = Field(default=None, gt=0)
    time: str | None = Field(default=None, max_length=40)        # {{timenow}}
    indicators: dict[str, float] = Field(default_factory=dict, max_length=40)
    structure: Structure | None = None
    option: OptionContract | None = None
    notes: str | None = Field(default=None, max_length=500)

    @field_validator("indicators")
    @classmethod
    def _indicator_names(cls, v: dict[str, float]) -> dict[str, float]:
        bad = [k for k in v if not k.replace("_", "").isalnum() or len(k) > 30]
        if bad:
            raise ValueError(f"invalid indicator names: {bad}")
        return {k.lower(): x for k, x in v.items()}


# ---------------------------------------------------------------- Claude's analysis

Decision = Literal["LONG_SETUP", "SHORT_SETUP", "WAIT_FOR_CONFIRMATION", "WAIT", "AVOID"]


class ClaudeAnalysis(BaseModel):
    """What Claude must return. Mirrors ANALYSIS_SCHEMA in analyst.py."""
    model_config = ConfigDict(extra="forbid")
    symbol: str
    asset_class: str
    timeframe: str
    strategy: str
    market_bias: Literal["BULLISH", "BEARISH", "NEUTRAL"]
    setup: str
    bullish_conditions: list[str]
    bearish_conditions: list[str]
    entry_zone: list[float]
    stop_loss: float | None
    take_profit: list[float]
    risk_reward: str
    setup_quality: Literal["A", "B", "C", "D"]
    confidence: Literal["LOW", "MODERATE", "HIGH"]
    confirmation: list[str]
    invalidation: str
    reasons_to_avoid: list[str]
    data_used: list[str]
    data_requests: list[str]
    decision: Decision
    reasoning: str


# ---------------------------------------------------------------- API models


class RiskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    side: Literal["long", "short"]
    entry: float = Field(gt=0)
    stop_loss: float = Field(gt=0)
    take_profit: list[float] = Field(default_factory=list, max_length=5)
    asset_class: AssetClass
    symbol: str | None = None
    account_balance: float | None = Field(default=None, gt=0)
    risk_percent: float | None = None
    multiplier: float | None = Field(default=None, gt=0)


class JournalCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    analysis_id: int | None = None
    symbol: str | None = Field(default=None, max_length=40)
    side: Literal["long", "short"] | None = None
    entry: float | None = Field(default=None, gt=0)
    stop_loss: float | None = Field(default=None, gt=0)
    take_profit: list[float] = Field(default_factory=list, max_length=5)
    quantity: float | None = Field(default=None, gt=0)
    multiplier: float | None = Field(default=None, gt=0, description="$ per point; futures default to the contract spec")
    notes: str | None = Field(default=None, max_length=2000)


class JournalUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["planned", "open", "closed", "skipped"] | None = None
    exit_price: float | None = Field(default=None, gt=0)
    notes: str | None = Field(default=None, max_length=2000)


class WebhookAck(BaseModel):
    status: Literal["accepted", "duplicate"]
    alert_id: int | None
    asset_class: str | None = None
    timeframe: str | None = None
    strategy: str | None = None
    received_at: datetime | None = None
