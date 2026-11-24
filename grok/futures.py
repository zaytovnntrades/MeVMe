"""
Futures support for grok_gate: TradersPost-format TradingView alerts, CME contract specs,
contract-based risk, and CME equity-index trading hours.

The gate never BUILDS a TradersPost order. Your TradingView alert carries the TradersPost
JSON ({"ticker", "action", "sentiment", "quantity", "price", "stopLoss", ...}); the gate reads
it for risk checks and forwards it unchanged (minus gate_notes). Where a field's meaning
can't be pinned down from the payload alone (for example stopLoss.amount), risk counts as
unmeasurable, so entries are blocked whenever a risk limit needs that number.

Not handled: CME holidays and early closes. Check the CME holiday calendar yourself.
"""
import re
from datetime import datetime
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

# Dollar value of a 1.00 move in price, per contract. Add others with futures_point_values in risk.json.
POINT_VALUE = {
    "NQ": 20.0, "MNQ": 2.0,     # Nasdaq-100
    "ES": 50.0, "MES": 5.0,     # S&P 500
    "YM": 5.0, "MYM": 0.5,      # Dow
    "RTY": 50.0, "M2K": 5.0,    # Russell 2000
}
INDEX_ROOTS = set(POINT_VALUE)  # all trade CME Globex equity-index hours
MONTH_CODES = "FGHJKMNQUVXZ"
ENTRY_ACTIONS = {"buy": "long", "sell": "short"}
EXIT_ACTIONS = {"exit", "cancel"}


def is_traderspost(payload: dict) -> bool:
    return "ticker" in payload and "action" in payload and "exchange" not in payload


def root_symbol(ticker) -> str:
    """CME_MINI:MNQ1!, MNQ1!, MNQZ2026, MNQZ26, mnq -> MNQ.  M2KZ26 -> M2K."""
    t = str(ticker).strip().upper().split(":")[-1]
    t = re.sub(r"\d+!$", "", t)                                  # continuous contract: MNQ1!
    m = re.fullmatch(rf"([A-Z0-9]+?)[{MONTH_CODES}](\d{{1,4}})", t)  # dated contract: MNQZ26
    if m and m.group(1) in POINT_VALUE:
        return m.group(1)
    return t


def classify(payload: dict) -> tuple[str, str | None]:
    """('entry', 'long'|'short') or ('exit', None).

    exit/cancel, sentiment "flat", and a buy or sell AGAINST the stated sentiment
    (e.g. sell with sentiment long = trimming a long) are exits: never blocked.
    Anything unrecognised is passed through like an exit, so TradersPost rejects it
    rather than the gate trapping you in a position."""
    action = str(payload.get("action", "")).strip().lower()
    sentiment = str(payload.get("sentiment", "")).strip().lower()
    if action in EXIT_ACTIONS or sentiment == "flat":
        return "exit", None
    if action == "add":
        return ("entry", sentiment) if sentiment in ("long", "short") else ("exit", None)
    direction = ENTRY_ACTIONS.get(action)
    if not direction:
        return "exit", None
    if sentiment in ("long", "short") and sentiment != direction:
        return "exit", None
    return "entry", direction


def _num(v) -> float | None:
    try:
        f = float(v)
        return f if f == f and abs(f) != float("inf") else None
    except (TypeError, ValueError):
        return None


def _per_root(value, root: str):
    """A limit may be one number for every contract or a {"MNQ": 5, "NQ": 1} dict."""
    if isinstance(value, dict):
        return value.get(root)
    return value


def measure(payload: dict, limits: dict) -> dict:
    root = root_symbol(payload.get("ticker", ""))
    pv = (limits.get("futures_point_values") or {}).get(root) or POINT_VALUE.get(root)
    qtype = str(payload.get("quantityType", "fixed_quantity")).lower()
    contracts = _num(payload.get("quantity")) if qtype in ("fixed_quantity", "contracts") else None
    price = _num(payload.get("price")) or _num(payload.get("signal_price"))

    sl = payload.get("stopLoss")
    has_sl = isinstance(sl, dict) and any(_num(sl.get(k)) for k in ("stopPrice", "percent", "amount"))
    stop_points = None
    if has_sl and price:
        if _num(sl.get("stopPrice")):
            stop_points = abs(price - _num(sl["stopPrice"]))
        elif _num(sl.get("percent")):
            stop_points = price * _num(sl["percent"]) / 100
        # stopLoss.amount: meaning not verified here, so stop distance stays unknown.

    risk = contracts * stop_points * pv if None not in (contracts, stop_points, pv) else None
    notional = contracts * price * pv if None not in (contracts, price, pv) else None
    return {"market": "futures", "symbol": root, "point_value": pv, "contracts": contracts,
            "quantity_type": qtype, "price": price, "has_stop_loss": has_sl,
            "stop_points": stop_points, "risk_usdt": risk, "notional_usdt": notional}


def session_block(root: str, when: datetime, minutes_before_close: float = 0) -> str | None:
    """CME Globex equity-index hours, US Eastern: Sunday 18:00 to Friday 17:00, with a daily
    17:00-18:00 break. Returns a reason if a NEW entry shouldn't be opened now, else None."""
    if root not in INDEX_ROOTS:
        return None  # unknown product: no session rule, rather than a wrong one
    et = when.astimezone(ET)
    wd, t = et.weekday(), et.hour * 60 + et.minute  # Mon=0 .. Sun=6
    close, reopen = 17 * 60, 18 * 60
    if wd == 5 or (wd == 4 and t >= close) or (wd == 6 and t < reopen):
        return f"market closed (weekend): {et:%a %H:%M} ET"
    if close <= t < reopen:
        return f"market closed (daily break 17:00-18:00 ET): {et:%a %H:%M} ET"
    if minutes_before_close and wd <= 4 and close - minutes_before_close <= t < close:
        return f"too close to the 17:00 ET close ({close - t} min left, limit {minutes_before_close:g})"
    return None


def check_static(m: dict, limits: dict, now: float) -> list[str]:
    r = []
    root = m["symbol"]
    if root in limits.get("blocked_symbols", []):
        r.append(f"blocked: {root} is on blocked_symbols")
    allowed = limits.get("allowed_symbols")
    if allowed and root not in allowed:
        r.append(f"not allowed: {root} is not on allowed_symbols")

    if limits.get("futures_session"):
        why = session_block(root, datetime.fromtimestamp(now, ET),
                            limits.get("no_entry_minutes_before_close") or 0)
        if why:
            r.append(why)

    cap = _per_root(limits.get("max_contracts"), root)
    if cap is not None:
        if m["contracts"] is None:
            r.append(f"max_contracts: can't count contracts (quantityType '{m['quantity_type']}' "
                     "or no quantity; send a fixed quantity)")
        elif m["contracts"] > cap:
            r.append(f"{m['contracts']:g} contracts > max_contracts {cap:g} for {root}")

    if limits.get("require_stop_loss") and not m["has_stop_loss"]:
        r.append("no stop-loss: require_stop_loss is on (add stopLoss to the alert)")
    cap = _per_root(limits.get("max_stop_points"), root)
    if cap is not None and m["has_stop_loss"]:
        if m["stop_points"] is None:
            r.append("max_stop_points: can't measure the stop (use stopLoss.stopPrice or percent, plus price)")
        elif m["stop_points"] > cap + 1e-9:
            r.append(f"stop {m['stop_points']:g} pts > max_stop_points {cap:g} for {root}")

    if "max_risk_usdt" in limits or "max_daily_risk_usdt" in limits:
        if m["risk_usdt"] is None:
            if m["point_value"] is None:
                why = f"unknown contract {root} (add it to futures_point_values)"
            elif not m["has_stop_loss"]:
                why = "no stop-loss, so the loss is unbounded"
            else:
                why = "contracts, price or stop distance unknown"
            r.append(f"risk can't be measured ({why})")
        elif "max_risk_usdt" in limits and m["risk_usdt"] > limits["max_risk_usdt"] + 1e-9:
            r.append(f"trade risk ${m['risk_usdt']:,.2f} > max_risk_usdt ${limits['max_risk_usdt']:,.2f}")
    return r


def describe_contract(root: str) -> str:
    pv = POINT_VALUE.get(root)
    return f"{root}: ${pv:g} per point" if pv else f"{root}: unknown point value"


def flatten_order(payload: dict, direction: str) -> dict | None:
    """If a blocked entry was a REVERSAL (prev_market_position is the opposite side), the exit
    message that closes the old position, so a veto never leaves you holding what the strategy
    wanted out of. Needs "prev_market_position": "{{strategy.prev_market_position}}" in the alert."""
    prev = str(payload.get("prev_market_position", "")).strip().lower()
    opposite = "short" if direction == "long" else "long"
    if prev != opposite:
        return None
    return {"ticker": payload.get("ticker"), "action": "exit"}
