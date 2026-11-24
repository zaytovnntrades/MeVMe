"""Shield: the checks that can stop the system. They run BEFORE Claude is called, so a blocked alert costs
no API call, and again inside the risk manager for each proposed trade.

Blocks a new setup when:
  halt        the kill switch is on (dashboard button or POST /api/shield/halt)
  session     the alert arrives outside the entry window (default 09:35-11:30 New York, Mon-Fri)
  daily_loss  a full loss on one more trade would take today's loss past DAILY_LOSS_LIMIT
  streak      MAX_CONSECUTIVE_LOSSES full losses in a row today
  drawdown    the account floor is breached, or one more full loss would reach it
"""
from dataclasses import dataclass
from datetime import datetime, time, timezone

from .account import NY, AccountState
from sqlalchemy.exc import IntegrityError

from .db import Database, ShieldState, utcnow


@dataclass
class Block:
    kind: str
    reason: str


def parse_window(window: str) -> tuple[time, time]:
    """'09:35-11:30' -> (09:35, 11:30). Raises ValueError on anything else."""
    try:
        a, b = window.split("-")
        start, end = time.fromisoformat(a.strip()), time.fromisoformat(b.strip())
    except ValueError as e:
        raise ValueError(f"entry window must look like 09:35-11:30, got {window!r}") from e
    if start >= end:
        raise ValueError(f"entry window start must be before its end: {window!r}")
    return start, end


def in_window(now: datetime, window: str) -> bool:
    start, end = parse_window(window)
    et = (now if now.tzinfo else now.replace(tzinfo=timezone.utc)).astimezone(NY)
    return et.weekday() < 5 and start <= et.time() < end


def precheck(*, now: datetime, halted: bool, halt_reason: str | None, acct: AccountState,
             risk_per_trade: float | None, entry_window: str | None, daily_loss_limit: float | None,
             max_consecutive_losses: int | None) -> list[Block]:
    out: list[Block] = []
    if halted:
        out.append(Block("halt", f"kill switch is ON{': ' + halt_reason if halt_reason else ''}"))
    if entry_window and not in_window(now, entry_window):
        et = (now if now.tzinfo else now.replace(tzinfo=timezone.utc)).astimezone(NY)
        out.append(Block("session", f"outside the entry window {entry_window} New York "
                                    f"(it is {et:%a %H:%M} ET)"))
    if daily_loss_limit:
        worst = acct.today_pnl - (risk_per_trade or 0)
        if acct.today_pnl <= -daily_loss_limit or worst < -daily_loss_limit - 1e-9:
            out.append(Block("daily_loss", f"daily stop: today's P&L is {acct.today_pnl:,.2f}; one more full loss "
                                           f"({risk_per_trade or 0:,.0f}) would pass the {daily_loss_limit:,.0f} daily limit"))
    if max_consecutive_losses and acct.losing_streak_today >= max_consecutive_losses:
        out.append(Block("streak", f"{acct.losing_streak_today} straight losses today "
                                   f"(limit {max_consecutive_losses}): done for the day"))
    if acct.remaining is not None and (acct.breached or (risk_per_trade and risk_per_trade >= acct.remaining)):
        out.append(Block("drawdown", "account floor breached" if acct.breached else
                         f"one more full loss ({risk_per_trade:,.0f}) would reach the drawdown floor "
                         f"({acct.remaining:,.2f} left after open risk)"))
    return out


# ---------------------------------------------------------------- kill switch state


def get_halt(db: Database) -> ShieldState:
    with db.Session() as ses:
        row = ses.get(ShieldState, 1)
        if row is not None:
            return row
        try:
            row = ShieldState(id=1, halted=False, reason=None)
            ses.add(row)
            ses.commit()
            ses.refresh(row)
            return row
        except IntegrityError:          # another request created it at the same moment
            ses.rollback()
            return ses.get(ShieldState, 1)


def set_halt(db: Database, halted: bool, reason: str | None) -> ShieldState:
    get_halt(db)                        # make sure the row exists first
    with db.Session() as ses:
        row = ses.get(ShieldState, 1)
        row.halted, row.reason, row.updated_at = halted, (reason if halted else None), utcnow()
        ses.add(row)
        ses.commit()
        ses.refresh(row)
        return row
