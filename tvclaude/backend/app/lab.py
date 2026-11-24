"""Lab: turn a TradingView Strategy Tester "List of trades" CSV export into honest numbers.

Every figure comes from the uploaded file plus the fee you enter. Nothing is estimated or filled in.

TradingView's export format has changed over time, so columns are matched by name:
  trade number  "Trade #" / "Trade number"
  type          "Type"  (rows like "Entry Long", "Exit Long")
  date/time     "Date/Time" / "Date and time" / "Date"
  profit        "Profit USD" / "Net P&L USD" / "Profit"   (not the % columns)
  contracts     "Contracts" / "Quantity" / "Position size (qty)" / "Size (qty)"
The P&L of a trade is read from its exit row. If the file has no exit rows, the row that carries a profit
is used, one per trade number.
"""
import csv
import io
import math
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .account import trading_day

ALIASES = {
    "trade": ("trade #", "trade number", "trade"),
    "type": ("type",),
    "time": ("date/time", "date and time", "date time", "date", "time"),
    "profit": ("net p&l", "profit", "p&l"),
    "contracts": ("contracts", "quantity", "position size (qty)", "size (qty)", "qty", "size"),
}


class LabError(ValueError):
    pass


@dataclass
class LabTrade:
    number: str
    closed_at: datetime
    contracts: float
    gross: float
    fees: float

    @property
    def net(self) -> float:
        return self.gross - self.fees


@dataclass
class LabResult:
    trades: int
    wins: int
    losses: int
    win_rate: float | None
    total_net: float
    total_fees: float
    avg_win: float | None
    avg_loss: float | None
    expectancy_usd: float | None
    expectancy_r: float | None
    profit_factor: float | None
    max_drawdown_usd: float
    max_consecutive_losses: int
    days_traded: int
    worst_day: dict | None
    days_past_daily_limit: int
    first_trade: str | None
    last_trade: str | None
    drawdown_rule: dict
    verdict: str
    checks: list[dict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def _num(text: str) -> float | None:
    t = (text or "").strip().replace("−", "-").replace(",", "").replace("USD", "").replace("$", "").strip()
    if t in ("", "-", "—"):
        return None
    try:
        return float(t)
    except ValueError:
        return None


def _find(headers: list[str], key: str) -> int | None:
    low = [h.strip().lower() for h in headers]
    for alias in ALIASES[key]:
        for i, h in enumerate(low):
            if "%" in h:
                continue
            if h == alias or h.startswith(alias + " ") or (key == "profit" and h.startswith(alias) and "cum" not in h):
                return i
    return None


def _when(text: str, tz: ZoneInfo) -> datetime:
    t = text.strip()
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M",
                "%m/%d/%Y %H:%M", "%b %d, %Y %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(t, fmt).replace(tzinfo=tz)
        except ValueError:
            continue
    try:
        d = datetime.fromisoformat(t)
        return d if d.tzinfo else d.replace(tzinfo=tz)
    except ValueError:
        raise LabError(f"can't read the date {text!r}; expected something like 2026-11-20 09:45")


def parse(csv_text: str, fee_per_contract: float = 0.0, chart_tz: str = "America/New_York") -> list[LabTrade]:
    """fee_per_contract = round-trip commission + fees per contract, subtracted from each trade."""
    if not csv_text or not csv_text.strip():
        raise LabError("the file is empty")
    text = csv_text.lstrip("﻿")
    try:
        dialect = csv.Sniffer().sniff(text.splitlines()[0], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    rows = list(csv.reader(io.StringIO(text), dialect))
    headers, body = rows[0], [r for r in rows[1:] if any(c.strip() for c in r)]
    col = {k: _find(headers, k) for k in ALIASES}
    missing = [k for k in ("time", "profit") if col[k] is None]
    if missing:
        raise LabError(f"couldn't find the {', '.join(missing)} column(s). Headers in your file: {headers}. "
                       "Export it from Strategy Tester > List of trades > Export.")
    tz = ZoneInfo(chart_tz)

    def cell(r, k):
        i = col[k]
        return r[i] if i is not None and i < len(r) else ""

    has_exit = col["type"] is not None and any("exit" in cell(r, "type").lower() for r in body)
    picked: dict[str, list[str]] = {}
    for n, r in enumerate(body):
        if has_exit and "exit" not in cell(r, "type").lower():
            continue
        if _num(cell(r, "profit")) is None:
            continue
        key = cell(r, "trade").strip() or str(n)
        picked.setdefault(key, r)           # first exit row per trade number
    if not picked:
        raise LabError("found no closed trades with a profit value in the file")
    trades = []
    for key, r in picked.items():
        qty = abs(_num(cell(r, "contracts")) or 1.0)
        trades.append(LabTrade(number=key, closed_at=_when(cell(r, "time"), tz), contracts=qty,
                               gross=_num(cell(r, "profit")), fees=round(fee_per_contract * qty, 2)))
    return sorted(trades, key=lambda t: t.closed_at)


def simulate_drawdown(trades: list[LabTrade], limit: float | None, mode: str, lock: float | None) -> dict:
    """Replays the trades through the account's drawdown rule and reports the first breach, if any."""
    if not limit:
        return {"limit": None, "breached": False, "note": "no drawdown limit configured"}
    cum = peak = eod_peak = 0.0
    day = None
    for t in trades:
        d = trading_day(t.closed_at)
        if day is not None and d != day:
            eod_peak = max(eod_peak, cum)        # yesterday closed at cum
        day = d
        cum += t.net
        peak = max(peak, cum)
        floor = {"static": -limit, "trailing": peak - limit, "eod_trailing": eod_peak - limit}[mode]
        if mode != "static" and lock is not None:
            floor = min(floor, lock)
        if cum <= floor:
            return {"limit": limit, "mode": mode, "breached": True, "breach_date": d.isoformat(),
                    "breach_trade": t.number, "balance_change_at_breach": round(cum, 2),
                    "floor_at_breach": round(floor, 2)}
    return {"limit": limit, "mode": mode, "breached": False, "final_change": round(cum, 2)}


def analyse(trades: list[LabTrade], *, risk_usd: float | None, daily_loss_limit: float | None,
            drawdown_limit: float | None, drawdown_mode: str, lock: float | None,
            min_trades: int = 100, min_expectancy_r: float = 0.3) -> LabResult:
    nets = [t.net for t in trades]
    wins = [x for x in nets if x > 0]
    losses = [x for x in nets if x < 0]
    n = len(nets)
    total = sum(nets)
    cum = peak = mdd = 0.0
    streak = best = 0
    for x in nets:
        cum += x
        peak = max(peak, cum)
        mdd = max(mdd, peak - cum)
        streak = streak + 1 if x < 0 else 0
        best = max(best, streak)
    by_day: dict[str, float] = {}
    for t in trades:
        k = trading_day(t.closed_at).isoformat()
        by_day[k] = by_day.get(k, 0.0) + t.net
    worst = min(by_day.items(), key=lambda kv: kv[1]) if by_day else None
    exp_usd = total / n if n else None
    exp_r = exp_usd / risk_usd if exp_usd is not None and risk_usd else None
    pf = (sum(wins) / -sum(losses)) if losses else (math.inf if wins else None)
    dd = simulate_drawdown(trades, drawdown_limit, drawdown_mode, lock)

    checks = [
        {"check": f"at least {min_trades} trades", "value": n, "pass": n >= min_trades},
        {"check": f"expectancy of +{min_expectancy_r}R or better after fees",
         "value": None if exp_r is None else round(exp_r, 3), "pass": exp_r is not None and exp_r >= min_expectancy_r},
        {"check": "drawdown limit never breached", "value": dd.get("breach_date"), "pass": not dd["breached"]},
    ]
    notes = []
    if not risk_usd:
        notes.append("R values need a risk per trade: set ACCOUNT_BALANCE and RISK_PERCENT")
    if all(t.fees == 0 for t in trades):
        notes.append("no fee entered: real commissions and fees will lower every number here")
    if n < min_trades:
        notes.append(f"{n} trades is too few to tell skill from luck; aim for {min_trades}+")
    verdict = "PASS" if all(c["pass"] for c in checks) else "NOT YET"
    r2 = lambda x: None if x is None else round(x, 2)  # noqa: E731
    return LabResult(
        trades=n, wins=len(wins), losses=len(losses), win_rate=r2(len(wins) / n * 100) if n else None,
        total_net=round(total, 2), total_fees=round(sum(t.fees for t in trades), 2),
        avg_win=r2(sum(wins) / len(wins)) if wins else None, avg_loss=r2(sum(losses) / len(losses)) if losses else None,
        expectancy_usd=r2(exp_usd), expectancy_r=None if exp_r is None else round(exp_r, 3),
        profit_factor=None if pf is None else (None if pf == math.inf else round(pf, 2)),
        max_drawdown_usd=round(mdd, 2), max_consecutive_losses=best, days_traded=len(by_day),
        worst_day={"day": worst[0], "net": round(worst[1], 2)} if worst else None,
        days_past_daily_limit=sum(1 for v in by_day.values() if daily_loss_limit and v <= -daily_loss_limit),
        first_trade=trades[0].closed_at.isoformat() if trades else None,
        last_trade=trades[-1].closed_at.isoformat() if trades else None,
        drawdown_rule=dd, verdict=verdict, checks=checks, notes=notes)
