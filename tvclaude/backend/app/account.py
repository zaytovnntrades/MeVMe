"""Account drawdown and daily stats, prop-firm style, computed from the paper journal (no broker connection).

Drawdown modes (the floor is the balance at which the account fails):
  static:       floor = starting balance - limit. Profits add buffer.
  trailing:     floor = highest closed-trade equity - limit (approximates an intraday trailing rule from
                closed trades only; the journal has no tick-by-tick open equity).
  eod_trailing: floor = highest END-OF-DAY balance - limit, moving only after a trading day closes
                (MyFundedFutures-style). Today's swings don't move it.
  lock_above_start: trailing floors stop rising once they reach starting balance + this amount
                (MyFundedFutures Builder 25K: $100, so the floor never goes above $25,100).

Trading days follow CME: a day runs 18:00 to 17:00 New York time, so a trade closed at 17:30 ET belongs to
the next trading day.

Open and planned journal entries reserve their full stop-loss risk, so the buffer counts losses that could
still happen, not only realised ones.
"""
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select

from .db import Database, JournalEntry

NY = ZoneInfo("America/New_York")
MODES = ("static", "trailing", "eod_trailing")
FULL_LOSS_R = -0.5          # a close at -0.5R or worse counts as a loss for the losing-streak rule


def trading_day(dt: datetime) -> date:
    """CME trading day of a timestamp (naive timestamps are UTC, as the database stores them)."""
    aware = dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    return (aware.astimezone(NY) + timedelta(hours=7)).date()    # 17:00 ET rolls to the next day


@dataclass
class AccountState:
    starting_balance: float | None
    drawdown_limit: float | None
    mode: str
    lock_above_start: float | None
    realized_pnl: float
    equity: float | None
    peak_equity: float | None          # highest closed-trade equity
    peak_eod_balance: float | None     # highest balance at a completed day's close
    floor_balance: float | None        # the account fails at or below this balance
    floor_locked: bool
    open_risk: float
    drawdown_used: float
    remaining: float | None            # buffer left after open risk; None = no limit configured
    breached: bool
    trading_day: str
    today_pnl: float
    today_closed: int
    losing_streak_today: int

    def to_dict(self) -> dict:
        return asdict(self)


def compute(entries: list[JournalEntry], starting_balance: float | None, limit: float | None, mode: str,
            lock_above_start: float | None = None, now: datetime | None = None) -> AccountState:
    if mode not in MODES:
        raise ValueError(f"drawdown mode must be one of {MODES}")
    now = now or datetime.now(timezone.utc)
    today = trading_day(now)

    def when(j):        # entries without timestamps (e.g. built in memory) count as closed now
        t = j.closed_at or j.created_at or now
        return t if t.tzinfo else t.replace(tzinfo=timezone.utc)

    closed = sorted((j for j in entries if j.status == "closed" and j.pnl is not None),
                    key=lambda j: (when(j), j.id or 0))

    cum, peak, eod_peak = 0.0, 0.0, 0.0
    day_end: dict[date, float] = {}
    for j in closed:
        cum += j.pnl
        peak = max(peak, cum)
        day_end[trading_day(when(j))] = cum     # balance after the day's last close
    for d, bal in day_end.items():
        if d < today:                                                 # only COMPLETED days move an EOD floor
            eod_peak = max(eod_peak, bal)

    floor = None
    locked = False
    if limit is not None:
        floor = {"static": -limit, "trailing": peak - limit, "eod_trailing": eod_peak - limit}[mode]
        if mode != "static" and lock_above_start is not None and floor >= lock_above_start:
            floor, locked = lock_above_start, True

    open_risk = sum(abs(j.entry - j.stop_loss) * (j.quantity or 0) * (j.multiplier or 1)
                    for j in entries if j.status in ("planned", "open"))
    todays = [j for j in closed if trading_day(when(j)) == today]
    streak = 0
    for j in reversed(todays):
        if j.r_multiple is not None and j.r_multiple <= FULL_LOSS_R:
            streak += 1
        else:
            break

    remaining = None if floor is None else (cum - floor) - open_risk
    used = 0.0 if floor is None else max(0.0, limit - (cum - floor))
    sb = starting_balance

    def bal(x):
        return round(sb + x, 2) if sb is not None else None

    return AccountState(
        starting_balance=sb, drawdown_limit=limit, mode=mode, lock_above_start=lock_above_start,
        realized_pnl=round(cum, 2), equity=bal(cum), peak_equity=bal(peak), peak_eod_balance=bal(eod_peak),
        floor_balance=bal(floor) if floor is not None else None, floor_locked=locked,
        open_risk=round(open_risk, 2), drawdown_used=round(used, 2),
        remaining=round(remaining, 2) if remaining is not None else None,
        breached=floor is not None and cum <= floor,
        trading_day=today.isoformat(), today_pnl=round(sum(j.pnl for j in todays), 2),
        today_closed=len(todays), losing_streak_today=streak)


def load(db: Database, starting_balance: float | None, limit: float | None, mode: str,
         lock_above_start: float | None = None, now: datetime | None = None) -> AccountState:
    with db.Session() as ses:
        return compute(list(ses.scalars(select(JournalEntry))), starting_balance, limit, mode,
                       lock_above_start, now)
