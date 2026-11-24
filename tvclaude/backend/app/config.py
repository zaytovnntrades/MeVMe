"""Settings from environment variables (or a .env file). No credential has a default."""
from functools import lru_cache

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore",
                                      env_ignore_empty=True)   # KEY= in .env means "not set"

    # --- security -------------------------------------------------------------------------
    webhook_secret: str = Field(min_length=16, description="Shared secret TradingView sends")
    dashboard_token: str = Field(min_length=16, description="Bearer token for /api/* and /ws")
    cors_origins: list[str] = ["http://localhost:3000"]
    rate_limit_per_minute: int = 30          # per client IP on the webhook
    trust_proxy: bool = False                # read X-Forwarded-For (only behind your own proxy)
    tradingview_ip_allowlist: list[str] = [] # e.g. TradingView's published webhook IPs; empty = off
    max_body_bytes: int = 32_000

    # --- Claude -----------------------------------------------------------------------------
    anthropic_api_key: str | None = None     # read by the SDK; never sent to the frontend
    anthropic_model: str = "claude-opus-5-5"
    analysis_effort: str = "medium"          # low | medium | high | xhigh | max
    analysis_timeout_s: float = 120
    analysis_workers: int = 2

    # --- storage ----------------------------------------------------------------------------
    database_url: str = "sqlite:///./tvclaude.db"   # docker-compose sets PostgreSQL
    redis_url: str | None = None                     # optional: queue + rate limit + dedup

    # --- risk -------------------------------------------------------------------------------
    account_balance: float | None = None     # required for position sizing; never assumed
    risk_percent: float = 1.0
    allowed_risk_percents: list[float] = [0.5, 1.0, 2.0]
    min_reward_risk: float = 2.0             # 2.0 = 1:2
    rr_target_index: int = 2                 # which take-profit the min R:R is judged at (1-based)
    contract_specs_file: str | None = None   # JSON: futures/options multipliers; never assumed
    max_target_r: float | None = None        # targets further than this many R are flagged
    target_ladder: list[float] = []          # e.g. [2, 3, 5]: where Claude should aim TP1..TP3, in R

    # --- account drawdown (prop-firm style) -----------------------------------------------
    drawdown_limit: float | None = None      # e.g. 1000: total loss that ends the account
    drawdown_mode: str = "static"            # static | trailing (closed-trade peak) | eod_trailing (MFF-style)
    drawdown_lock_above_start: float | None = None   # trailing floor stops at start + this (MFF Builder: 100)
    account_phase: str = "evaluation"        # evaluation | funded (funded rules can be stricter: re-check them)

    # --- Shield: daily stop, session, kill switch -------------------------------------------
    daily_loss_limit: float | None = None    # e.g. 500: no new setups once one more loss would pass this
    max_consecutive_losses: int | None = None  # e.g. 2: done for the day after this many full losses in a row
    entry_window: str | None = None          # e.g. "09:35-11:30" New York time, Mon-Fri; empty = always

    # --- coordinator ------------------------------------------------------------------------
    daily_report_time: str | None = None     # e.g. "16:05" New York, weekdays; empty = no scheduled report
    weekly_review_day: str = "Fri"           # Mon..Fri: the weekly review goes out after that day's report

    # --- what the system trades -----------------------------------------------------------
    allowed_asset_classes: list[str] = []    # empty = all; e.g. ["future"]
    allowed_futures_roots: list[str] = []    # empty = all; e.g. ["NQ", "MNQ"]

    # --- notifications ----------------------------------------------------------------------
    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None
    discord_webhook_url: str | None = None   # Channel settings > Integrations > Webhooks > Copy URL
    notify_decisions: list[str] = ["LONG_SETUP", "SHORT_SETUP"]

    # --- execution --------------------------------------------------------------------------
    trading_mode: str = "paper"

    @field_validator("analysis_effort")
    @classmethod
    def _effort(cls, v: str) -> str:
        if v not in {"low", "medium", "high", "xhigh", "max"}:
            raise ValueError("analysis_effort must be low|medium|high|xhigh|max")
        return v

    @field_validator("account_phase")
    @classmethod
    def _phase(cls, v: str) -> str:
        if v not in ("evaluation", "funded"):
            raise ValueError("account_phase must be evaluation or funded")
        return v

    @field_validator("entry_window")
    @classmethod
    def _window(cls, v: str | None) -> str | None:
        if v:
            from .shield import parse_window
            parse_window(v)
        return v

    @field_validator("discord_webhook_url")
    @classmethod
    def _discord(cls, v: str | None) -> str | None:
        if v and not v.startswith(("https://discord.com/api/webhooks/", "https://discordapp.com/api/webhooks/")):
            raise ValueError("discord_webhook_url must be a Discord webhook URL (https://discord.com/api/webhooks/...)")
        return v

    @field_validator("daily_report_time")
    @classmethod
    def _report_time(cls, v: str | None) -> str | None:
        if v:
            from datetime import time
            try:
                time.fromisoformat(v)
            except ValueError as e:
                raise ValueError("daily_report_time must look like 16:05") from e
        return v

    @field_validator("weekly_review_day")
    @classmethod
    def _review_day(cls, v: str) -> str:
        if v not in ("Mon", "Tue", "Wed", "Thu", "Fri"):
            raise ValueError("weekly_review_day must be Mon, Tue, Wed, Thu or Fri")
        return v

    @field_validator("drawdown_mode")
    @classmethod
    def _dd_mode(cls, v: str) -> str:
        if v not in ("static", "trailing", "eod_trailing"):
            raise ValueError("drawdown_mode must be static, trailing or eod_trailing")
        return v

    @field_validator("allowed_futures_roots")
    @classmethod
    def _roots(cls, v: list[str]) -> list[str]:
        return [r.upper() for r in v]

    @field_validator("trading_mode")
    @classmethod
    def _paper_only(cls, v: str) -> str:
        if v != "paper":
            raise ValueError(
                "trading_mode must be 'paper'. This system is analysis-only; broker execution "
                "does not exist and needs your explicit authorization before it is ever added."
            )
        return v

    @model_validator(mode="after")
    def _risk(self) -> "Settings":
        if self.risk_percent not in self.allowed_risk_percents:
            raise ValueError(f"risk_percent {self.risk_percent} is not in allowed_risk_percents "
                             f"{self.allowed_risk_percents}")
        if self.account_balance is not None and self.account_balance <= 0:
            raise ValueError("account_balance must be > 0")
        if self.min_reward_risk <= 0 or self.rr_target_index < 1:
            raise ValueError("min_reward_risk must be > 0 and rr_target_index >= 1")
        if self.drawdown_limit is not None:
            if self.drawdown_limit <= 0:
                raise ValueError("drawdown_limit must be > 0")
            if self.account_balance is not None and \
                    self.account_balance * self.risk_percent / 100 >= self.drawdown_limit:
                raise ValueError("one full loss at risk_percent would reach drawdown_limit: lower risk_percent")
        bad = set(self.allowed_asset_classes) - {"stock", "option", "future", "crypto"}
        if bad:
            raise ValueError(f"unknown asset classes {sorted(bad)}")
        return self

    @property
    def telegram_enabled(self) -> bool:
        return bool(self.telegram_bot_token and self.telegram_chat_id)


@lru_cache
def get_settings() -> Settings:
    return Settings()
