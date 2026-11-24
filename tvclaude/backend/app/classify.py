"""Identify asset class and normalise TradingView timeframes. Pure functions, no I/O."""
import re

from .schemas import AssetClass, TradingViewAlert

CRYPTO_EXCHANGES = {"BINANCE", "BYBIT", "COINBASE", "KRAKEN", "OKX", "BITGET", "BITSTAMP", "KUCOIN",
                    "GEMINI", "MEXC", "BLOFIN", "BITUNIX", "CRYPTO", "CRYPTOCAP"}
FUTURES_EXCHANGES = {"CME", "CME_MINI", "CBOT", "CBOT_MINI", "NYMEX", "COMEX", "ICEUS", "EUREX"}
OCC_OPTION = re.compile(r"^[A-Z]{1,6}\d{6}[CP]\d{8}$")          # AAPL260116C00200000
DATED_FUTURE = re.compile(r"^[A-Z0-9]{1,4}[FGHJKMNQUVXZ]\d{1,4}$")  # MNQZ26, ESH2027

# TradingView {{interval}} values -> canonical
_TF = {"1": "1m", "3": "3m", "5": "5m", "15": "15m", "30": "30m", "45": "45m", "60": "1h",
       "120": "2h", "240": "4h", "D": "1d", "1D": "1d", "W": "1w", "1W": "1w"}


def split_ticker(ticker: str, exchange: str | None) -> tuple[str, str | None]:
    if ":" in ticker:
        ex, sym = ticker.split(":", 1)
        return sym.upper(), (exchange or ex).upper()
    return ticker.upper(), exchange.upper() if exchange else None


def normalise_timeframe(tf: str) -> str:
    raw = tf.strip()
    if raw in _TF:
        return _TF[raw]
    m = re.fullmatch(r"(\d+)\s*([mMhHdDwW])", raw)
    if not m:
        raise ValueError(f"unrecognised timeframe '{tf}' (use TradingView {{{{interval}}}} or e.g. 5m, 1h, 1d)")
    n, unit = int(m.group(1)), m.group(2).lower()
    if unit == "m" and n % 60 == 0:
        return f"{n // 60}h"
    return f"{n}{unit}"


def asset_class(alert: TradingViewAlert) -> AssetClass:
    """Explicit asset_class wins. Otherwise: option data or an OCC symbol -> option; continuous (MNQ1!)
    or dated (MNQZ26) contracts or a futures exchange -> future; a crypto exchange, perp suffix or
    stablecoin quote -> crypto; anything else -> stock. Send asset_class when in doubt."""
    if alert.asset_class:
        return alert.asset_class
    sym, ex = split_ticker(alert.ticker, alert.exchange)
    if alert.option is not None or OCC_OPTION.match(sym):
        return AssetClass.option
    if ex in CRYPTO_EXCHANGES or sym.endswith((".P", "USDT", "USDC", "BUSD", "PERP")):
        return AssetClass.crypto
    if ex in FUTURES_EXCHANGES or re.search(r"\d!$", sym) or (ex is None and DATED_FUTURE.match(sym)):
        return AssetClass.future
    return AssetClass.stock
