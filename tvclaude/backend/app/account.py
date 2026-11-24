"""Account drawdown, prop-firm style, computed from the paper journal (there is no broker connection).

static:   the account fails if equity falls `limit` below the starting balance. Profits add buffer.
trailing: the account fails if equity falls `limit` below its highest closed-equity point. Profits move the
          floor up, so the buffer never grows past `limit`.

Open and planned journal entries reserve their full stop-loss risk, so the buffer counts losses that
could still happen, not only realised ones.
"""
from dataclasses import asdict, dataclass

from sqlalchemy import select

from .db import Database, JournalEntry


@dataclass
class AccountState:
    starting_balance: float | None
    drawdown_limit: float | None
    mode: str
    realized_pnl: float
    equity: float | None
    peak_equity: float | None
    open_risk: float
    drawdown_used: float
    remaining: float | None          # buffer left after open risk; None = no limit configured
    breached: bool

    def to_dict(self) -> dict:
        return asdict(self)


def compute(entries: list[JournalEntry], starting_balance: float | None, limit: float | None,
            mode: str) -> AccountState:
    closed = sorted((j for j in entries if j.status == "closed" and j.pnl is not None),
                    key=lambda j: (j.closed_at or j.created_at, j.id))
    cum, peak = 0.0, 0.0
    for j in closed:
        cum += j.pnl
        peak = max(peak, cum)
    open_risk = sum(abs(j.entry - j.stop_loss) * (j.quantity or 0) * (j.multiplier or 1)
                    for j in entries if j.status in ("planned", "open"))
    used = (peak - cum) if mode == "trailing" else max(0.0, -cum)
    remaining = None
    if limit is not None:
        buffer = limit - used if mode == "trailing" else limit + cum
        remaining = buffer - open_risk
    equity = starting_balance + cum if starting_balance is not None else None
    return AccountState(
        starting_balance=starting_balance, drawdown_limit=limit, mode=mode, realized_pnl=round(cum, 2),
        equity=round(equity, 2) if equity is not None else None,
        peak_equity=round(starting_balance + peak, 2) if starting_balance is not None else None,
        open_risk=round(open_risk, 2), drawdown_used=round(used, 2),
        remaining=round(remaining, 2) if remaining is not None else None,
        breached=limit is not None and used >= limit)


def load(db: Database, starting_balance: float | None, limit: float | None, mode: str) -> AccountState:
    with db.Session() as ses:
        return compute(list(ses.scalars(select(JournalEntry))), starting_balance, limit, mode)
