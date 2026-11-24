"""
Risk limits for grok_gate entries, ported from trader-risk's check.py and adapted to
StrategyFactory crypto-perp payloads. Pure code, no API calls; runs before Grok is asked.

Every limit is optional: leave a key out of risk.json (or set it to null) to turn it off.
All failing rules are reported, not just the first, so one fix-list covers everything.

Exits never come here. A halt file, a blocked symbol or a spent daily budget can stop new
entries but can never trap you in a position.

What the gate CAN'T see: your balance, open positions and realised P&L live on the
exchange. So the daily limit caps the worst-case loss you *commit to* each day
(sum of stop-loss risk on entries sent), not the P&L afterwards.
"""
import json
import os
import threading
import time
import uuid
from pathlib import Path

import futures

# The keys risk.json may contain. Anything else is rejected so a typo can't silently disable a rule.
LIMIT_KEYS = {
    "account_usdt": "your account size, needed to value %-of-wallet orders in USDT",
    "blocked_symbols": "list: never open these",
    "allowed_symbols": "list: open only these (empty or missing = any)",
    "max_leverage": "highest leverage an entry may use",
    "max_margin_usdt": "most margin one entry may commit",
    "max_notional_usdt": "largest position size (margin x leverage) one entry may open",
    "max_wallet_pct": "largest %-of-wallet entry (qty_in_percentage=1)",
    "require_stop_loss": "true: every entry must carry a stop-loss",
    "max_stop_loss_pct": "widest stop-loss allowed, as % price move from entry",
    "max_risk_usdt": "most one trade may lose if its stop is hit (notional x stop %)",
    "max_entries_per_day": "entries allowed per UTC day",
    "max_entries_per_symbol_per_day": "entries allowed per symbol per UTC day",
    "max_daily_risk_usdt": "total stop-loss risk allowed across all entries per UTC day (crypto + futures)",
    # futures (TradersPost alerts). Number = every contract, or {"MNQ": 5, "NQ": 1} per contract.
    "max_contracts": "futures: most contracts one entry may open",
    "max_stop_points": "futures: widest stop, in index points",
    "futures_session": "futures: true = no entries outside CME hours (Sun 18:00 to Fri 17:00 ET, 17-18 break)",
    "no_entry_minutes_before_close": "futures: no entries this many minutes before the 17:00 ET close",
    "futures_point_values": 'futures: extra or overriding $-per-point, e.g. {"CL": 1000}',
}
PER_ROOT_KEYS = {"max_contracts", "max_stop_points", "futures_point_values"}
SIZING = {"0": "base", "1": "percent", "2": "usdt"}


def load_limits(path: str | None) -> dict:
    """Read risk.json. Missing file = no limits. Unknown keys or bad values = hard error."""
    if not path or not Path(path).exists():
        return {}
    data = json.loads(Path(path).read_text())
    if not isinstance(data, dict):
        raise ValueError(f"{path} must be a JSON object")
    unknown = sorted(set(data) - set(LIMIT_KEYS) - {"_comment"})
    if unknown:
        raise ValueError(f"{path}: unknown key(s) {unknown}. Valid keys: {sorted(LIMIT_KEYS)}")
    limits = {k: v for k, v in data.items() if k in LIMIT_KEYS and v is not None}
    for k, v in limits.items():
        if k in ("blocked_symbols", "allowed_symbols"):
            if not isinstance(v, list):
                raise ValueError(f"{path}: {k} must be a list")
            limits[k] = [norm_symbol(s) for s in v]
        elif k in PER_ROOT_KEYS and isinstance(v, dict):
            if not v or not all(isinstance(n, (int, float)) and not isinstance(n, bool) and n > 0
                                for n in v.values()):
                raise ValueError(f"{path}: {k} values must be numbers greater than 0")
            limits[k] = {futures.root_symbol(r): n for r, n in v.items()}
        elif k == "futures_point_values":
            raise ValueError(f"{path}: {k} must be an object like {{\"CL\": 1000}}")
        elif k in ("require_stop_loss", "futures_session"):
            if not isinstance(v, bool):
                raise ValueError(f"{path}: {k} must be true or false")
        elif not isinstance(v, (int, float)) or isinstance(v, bool) or v <= 0:
            raise ValueError(f"{path}: {k} must be a number greater than 0, got {v!r}")
    return limits


def norm_symbol(s) -> str:
    """BTCUSDT.P, btcusdt, BYBIT:BTCUSDT.P -> BTCUSDT, so lists match however TradingView spells it."""
    s = str(s).strip().upper()
    s = s.split(":")[-1]
    s = s[:-2] if s.endswith(".P") else s
    return futures.root_symbol(s)  # MNQ1! / MNQZ26 -> MNQ; crypto symbols pass through


def _num(v) -> float | None:
    try:
        f = float(v)
        return f if f == f and f not in (float("inf"), float("-inf")) else None
    except (TypeError, ValueError):
        return None


def measure(payload: dict, limits: dict) -> dict:
    """Crypto (StrategyFactory) or futures (TradersPost) entry -> the numbers the rules need."""
    if futures.is_traderspost(payload):
        return futures.measure(payload, limits)
    return measure_crypto(payload, limits)


def measure_crypto(payload: dict, limits: dict) -> dict:
    """Work out leverage, margin, notional, stop % and USDT risk from a StrategyFactory entry.
    Fields that can't be worked out are None (and the rules that need them say so)."""
    side_lev = payload.get("buy_leverage") if str(payload.get("position")) == "0" else payload.get("sell_leverage")
    lev = _num(side_lev) or _num(payload.get("buy_leverage")) or _num(payload.get("sell_leverage")) or 1.0
    qty = _num(payload.get("qty"))
    price = _num(payload.get("signal_price"))
    sizing = SIZING.get(str(payload.get("qty_in_percentage", "2")), "usdt")

    margin = None
    wallet_pct = None
    if qty is not None:
        if sizing == "usdt":
            margin = qty
        elif sizing == "percent":
            wallet_pct = qty
            if limits.get("account_usdt"):
                margin = limits["account_usdt"] * qty / 100
        elif sizing == "base" and price:
            margin = qty * price / lev
    notional = margin * lev if margin is not None else None

    sl_raw = _num(payload.get("stop_loss_price"))
    has_sl = str(payload.get("force_tp")) == "1" and sl_raw is not None and sl_raw > 0
    sl_pct = None
    if has_sl:
        if str(payload.get("tp_type")) == "1":
            sl_pct = sl_raw
        elif price:
            sl_pct = abs(price - sl_raw) / price * 100
    risk = notional * sl_pct / 100 if notional is not None and sl_pct is not None else None
    return {"market": "crypto", "symbol": norm_symbol(payload.get("coin_pair", "")), "sizing": sizing, "leverage": lev,
            "margin_usdt": margin, "wallet_pct": wallet_pct, "notional_usdt": notional,
            "has_stop_loss": has_sl, "stop_loss_pct": sl_pct, "risk_usdt": risk}


def check_static(m: dict, limits: dict, now: float | None = None) -> list[str]:
    """Rules that depend on this one entry only."""
    if m.get("market") == "futures":
        return futures.check_static(m, limits, time.time() if now is None else now)
    r = []
    sym = m["symbol"]
    if sym in limits.get("blocked_symbols", []):
        r.append(f"blocked: {sym} is on blocked_symbols")
    allowed = limits.get("allowed_symbols")
    if allowed and sym not in allowed:
        r.append(f"not allowed: {sym} is not on allowed_symbols")
    if "max_leverage" in limits and m["leverage"] > limits["max_leverage"]:
        r.append(f"leverage {m['leverage']:g}x > max_leverage {limits['max_leverage']:g}x")
    if "max_wallet_pct" in limits and m["wallet_pct"] is not None and m["wallet_pct"] > limits["max_wallet_pct"]:
        r.append(f"size {m['wallet_pct']:g}% of wallet > max_wallet_pct {limits['max_wallet_pct']:g}%")

    def usd_rule(key, value, label):
        if key not in limits:
            return
        if value is None:
            r.append(f"{key}: can't value this {m['sizing']} order in USDT "
                     + ("(set account_usdt in risk.json)" if m["sizing"] == "percent"
                        else "(send signal_price, e.g. the builder's --include-signal-meta)"))
        elif value > limits[key] + 1e-9:
            r.append(f"{label} {value:,.2f} USDT > {key} {limits[key]:,.2f}")

    usd_rule("max_margin_usdt", m["margin_usdt"], "margin")
    usd_rule("max_notional_usdt", m["notional_usdt"], "position size")

    if limits.get("require_stop_loss") and not m["has_stop_loss"]:
        r.append("no stop-loss: require_stop_loss is on (builder: --sl)")
    if "max_stop_loss_pct" in limits and m["has_stop_loss"]:
        if m["stop_loss_pct"] is None:
            r.append("max_stop_loss_pct: absolute stop price needs signal_price to measure")
        elif m["stop_loss_pct"] > limits["max_stop_loss_pct"] + 1e-9:
            r.append(f"stop-loss {m['stop_loss_pct']:.2f}% > max_stop_loss_pct {limits['max_stop_loss_pct']:g}%")
    if "max_risk_usdt" in limits or "max_daily_risk_usdt" in limits:
        if m["risk_usdt"] is None:
            why = "no stop-loss, so the loss is unbounded" if not m["has_stop_loss"] else "size or stop can't be valued"
            r.append(f"risk can't be measured ({why})")
        elif "max_risk_usdt" in limits and m["risk_usdt"] > limits["max_risk_usdt"] + 1e-9:
            r.append(f"trade risk {m['risk_usdt']:,.2f} USDT > max_risk_usdt {limits['max_risk_usdt']:,.2f}")
    return r


class Ledger:
    """Per-UTC-day record of entries the gate let through, persisted to JSON so a restart
    doesn't reset the daily limits. Budget is RESERVED before Grok is asked (so two alerts at
    once can't both squeeze under a cap), then committed on a successful send or released."""

    def __init__(self, path: str, clock=time.time):
        self.path, self.clock, self.lock = Path(path), clock, threading.Lock()
        self.state = {"day": None, "entries": {}}
        if self.path.exists():
            try:
                self.state = json.loads(self.path.read_text())
            except (OSError, json.JSONDecodeError):
                pass  # unreadable ledger: start clean rather than crash the gate

    def _today(self) -> str:
        return time.strftime("%Y-%m-%d", time.gmtime(self.clock()))

    def _roll(self) -> None:
        if self.state.get("day") != self._today():
            self.state = {"day": self._today(), "entries": {}}

    def _save(self) -> None:
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(self.state, indent=2))
        os.replace(tmp, self.path)

    def totals(self) -> dict:
        with self.lock:
            self._roll()
            return self._totals()

    def _totals(self) -> dict:
        ents = self.state["entries"].values()
        per_symbol = {}
        for e in ents:
            per_symbol[e["symbol"]] = per_symbol.get(e["symbol"], 0) + 1
        return {"day": self.state["day"], "count": len(self.state["entries"]), "per_symbol": per_symbol,
                "risk_usdt": round(sum(e.get("risk_usdt") or 0 for e in ents), 6)}

    def reserve(self, m: dict, limits: dict) -> tuple[str | None, list[str]]:
        """Check the daily rules and, if they pass, hold a slot. Returns (reservation id, reasons)."""
        with self.lock:
            self._roll()
            t, r = self._totals(), []
            cap = limits.get("max_entries_per_day")
            if cap and t["count"] + 1 > cap:
                r.append(f"daily limit: {t['count']} entries already today, max_entries_per_day {cap:g}")
            cap = limits.get("max_entries_per_symbol_per_day")
            n = t["per_symbol"].get(m["symbol"], 0)
            if cap and n + 1 > cap:
                r.append(f"daily limit: {n} {m['symbol']} entries already today, "
                         f"max_entries_per_symbol_per_day {cap:g}")
            cap = limits.get("max_daily_risk_usdt")
            if cap and m["risk_usdt"] is not None and t["risk_usdt"] + m["risk_usdt"] > cap + 1e-9:
                r.append(f"daily risk: {t['risk_usdt']:,.2f} used + {m['risk_usdt']:,.2f} this trade "
                         f"> max_daily_risk_usdt {cap:,.2f}")
            if r:
                return None, r
            rid = uuid.uuid4().hex[:12]
            self.state["entries"][rid] = {"symbol": m["symbol"], "risk_usdt": m["risk_usdt"],
                                          "status": "reserved", "at": self.clock()}
            self._save()
            return rid, []

    def commit(self, rid: str) -> None:
        with self.lock:
            if rid in self.state["entries"]:
                self.state["entries"][rid]["status"] = "sent"
                self._save()

    def release(self, rid: str) -> None:
        with self.lock:
            if self.state["entries"].pop(rid, None) is not None:
                self._save()


def halted(halt_file: str | None) -> bool:
    """Kill switch: while this file exists, every new entry is blocked. `touch HALT` / `rm HALT`."""
    return bool(halt_file) and Path(halt_file).exists()


def describe(limits: dict) -> str:
    if not limits:
        return "no risk limits (create risk.json from risk.example.json)"
    return ", ".join(f"{k}={v}" for k, v in sorted(limits.items()))
