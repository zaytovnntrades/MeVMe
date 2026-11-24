"""The three analysis modules. Each one tells Claude what to evaluate, which inputs it expects, and how
far from the current price its levels may plausibly sit (used to catch invented or absurd levels)."""
from dataclasses import dataclass, field


@dataclass(frozen=True)
class StrategyModule:
    name: str
    title: str
    timeframes: tuple[str, ...]
    checklist: tuple[str, ...]
    expected_inputs: tuple[str, ...]
    max_level_distance_pct: float   # any entry/stop/target further than this from price is rejected
    holding: str
    notes: tuple[str, ...] = field(default_factory=tuple)

    def missing_inputs(self, indicators: dict[str, float], has_structure: bool, has_volume: bool) -> list[str]:
        have = set(indicators) | ({"structure"} if has_structure else set()) | ({"volume"} if has_volume else set())
        return [i for i in self.expected_inputs if i not in have]


SCALPING = StrategyModule(
    name="scalping", title="Scalping", timeframes=("1m", "3m", "5m"),
    checklist=(
        "Price relative to VWAP (above = long bias, below = short bias) and distance from it",
        "EMA 9/21 alignment and whether price is respecting EMA 9",
        "RSI momentum: direction, 50-line, extremes; no fresh longs into overbought without a sweep",
        "Volume confirmation: current volume vs average on the signal bar",
        "Liquidity sweep of a recent high/low followed by a reclaim",
        "Stop beyond the sweep/swing point; targets at the nearest liquidity or VWAP bands",
    ),
    expected_inputs=("vwap", "ema9", "ema21", "rsi", "volume", "volume_avg", "structure"),
    max_level_distance_pct=1.5, holding="minutes",
    notes=("Fees and slippage matter most here: a 1:2 target on a very tight stop may not survive costs.",),
)

DAY = StrategyModule(
    name="day", title="Day trading", timeframes=("5m", "15m", "30m"),
    checklist=(
        "Market structure: higher highs/lows or lower highs/lows; any BOS or CHoCH",
        "Key intraday support and resistance supplied in the alert",
        "VWAP position and reaction",
        "EMA 9/21/50 alignment",
        "Breakout confirmation: close beyond the level with volume, or a retest that holds",
        "Session timing caveats if supplied (open, lunch, close)",
    ),
    expected_inputs=("vwap", "ema9", "ema21", "ema50", "rsi", "volume", "structure"),
    max_level_distance_pct=3.0, holding="hours, flat by session end",
)

SWING = StrategyModule(
    name="swing", title="Swing trading", timeframes=("1h", "2h", "4h", "1d"),
    checklist=(
        "Higher-timeframe structure and trend continuation vs reversal",
        "Major support and resistance supplied in the alert",
        "MACD line/signal/histogram: crosses, momentum expansion or fading",
        "RSI divergence against price swings (only if swing values are supplied)",
        "EMA 21/50 trend filter",
        "Stops beyond the structural swing, not inside daily noise",
    ),
    expected_inputs=("ema21", "ema50", "rsi", "macd", "macd_signal", "macd_hist", "structure"),
    max_level_distance_pct=15.0, holding="days to weeks",
)

MODULES = {m.name: m for m in (SCALPING, DAY, SWING)}


def select(timeframe: str, requested: str | None) -> StrategyModule:
    """An explicit strategy wins if it fits the timeframe; otherwise the timeframe decides.
    5m belongs to both scalping and day trading and defaults to scalping unless the alert says 'day'."""
    if requested:
        mod = MODULES[requested]
        if timeframe not in mod.timeframes:
            raise ValueError(f"strategy '{requested}' covers {', '.join(mod.timeframes)}, not {timeframe}")
        return mod
    for mod in (SCALPING, DAY, SWING):
        if timeframe in mod.timeframes:
            return mod
    raise ValueError(f"no strategy module covers timeframe {timeframe} "
                     f"(scalping 1-5m, day 5-30m, swing 1h-1d)")
