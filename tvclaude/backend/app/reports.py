"""Coordinator: the daily report (after 16:00 New York) and Journal's weekly review.

All figures come from the database. The "one decision" is picked by fixed rules, in priority order, so it
is never invented. Claude only adds a short written summary on top; if it fails, the report still goes out.
There is no market data feed here, so "what the market did" is limited to the prices in your own alerts.
"""
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import select

from .account import NY, AccountState, trading_day
from .db import Alert, Analysis, JournalEntry, ReportLog

FULL_LOSS_R = -0.5


def _ny_date(dt: datetime) -> date:
    return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).astimezone(NY).date()


def daily_facts(ses, day: date, acct: AccountState, halted: bool, halt_reason: str | None,
                risk_per_trade: float | None, shield_cfg: dict) -> dict:
    rows = ses.execute(select(Alert, Analysis).outerjoin(Analysis, Analysis.alert_id == Alert.id)).all()
    todays = [(a, an) for a, an in rows if _ny_date(a.received_at) == day]
    decisions: dict[str, int] = {}
    blocked: dict[str, int] = {}
    setups = []
    prices = [a.price for a, _ in todays if a.price]
    for a, an in todays:
        if an is None:
            continue
        decisions[an.decision] = decisions.get(an.decision, 0) + 1
        if a.status == "blocked":
            for r in (an.risk or {}).get("reasons", []):
                key = r.split(":")[0]
                blocked[key] = blocked.get(key, 0) + 1
        if an.decision in ("LONG_SETUP", "SHORT_SETUP") and an.result:
            rk = an.risk or {}
            setups.append({"symbol": a.ticker, "decision": an.decision, "entry": an.result.get("entry_zone"),
                           "stop": an.result.get("stop_loss"), "targets": an.result.get("take_profit"),
                           "size": rk.get("contracts") or rk.get("quantity"), "risk_verdict": rk.get("verdict")})
    journal = list(ses.scalars(select(JournalEntry)))
    closed_today = [j for j in journal if j.status == "closed" and j.closed_at and trading_day(j.closed_at) == day]
    pending = [j for j in journal if j.status in ("planned", "open")]
    unrated = [j for j in closed_today if j.rules_followed is None]
    return {
        "day": day.isoformat(),
        "alerts": len(todays), "decisions": decisions, "blocked_by_shield": blocked, "setups": setups,
        "alert_price_range": [min(prices), max(prices)] if prices else None,
        "paper_trades_closed": len(closed_today),
        "paper_pnl_today": round(sum(j.pnl or 0 for j in closed_today), 2),
        "paper_r_today": [j.r_multiple for j in closed_today],
        "rule_breaks_today": sum(1 for j in closed_today if j.rules_followed is False),
        "trades_not_rated": len(unrated),
        "open_or_planned": [{"id": j.id, "symbol": j.symbol, "side": j.side, "status": j.status} for j in pending],
        "account": {"equity": acct.equity, "floor": acct.floor_balance, "floor_locked": acct.floor_locked,
                    "drawdown_left": acct.remaining, "mode": acct.mode, "losing_streak_today": acct.losing_streak_today},
        "risk_per_trade": risk_per_trade,
        "full_losses_left": (int(acct.remaining // risk_per_trade) if acct.remaining is not None and risk_per_trade
                             and acct.remaining > 0 else 0 if acct.remaining is not None else None),
        "kill_switch": {"on": halted, "reason": halt_reason},
        "shield": shield_cfg,
    }


def one_decision(f: dict) -> str:
    """The single most important thing for you to decide, by fixed priority."""
    risk = f.get("risk_per_trade") or 0
    left = f["account"]["drawdown_left"]
    if f["kill_switch"]["on"]:
        return (f"Kill switch is ON ({f['kill_switch']['reason'] or 'no reason given'}). Resume for tomorrow's "
                "session, or keep it off?")
    if left is not None and risk and left < 2 * risk:
        return (f"Only {left:,.2f} of drawdown left ({f['full_losses_left']} full loss(es)). Cut risk to 0.5% "
                f"({risk / 2:,.0f}) or pause trading until you decide?")
    if f["open_or_planned"]:
        ids = ", ".join(f"#{j['id']} {j['symbol']} ({j['status']})" for j in f["open_or_planned"][:5])
        return f"Paper trades still open or planned: {ids}. Close them with an exit price or mark them skipped."
    if f["account"]["losing_streak_today"] >= (f["shield"].get("max_consecutive_losses") or 99) or \
            ("daily stop" in " ".join(f["blocked_by_shield"])):
        return "Daily stop hit. Review today's losses in the journal before tomorrow: rule break or normal loss?"
    if f["trades_not_rated"]:
        return f"Mark 'rules followed?' on {f['trades_not_rated']} of today's trades so the weekly review is honest."
    return "No decision needed today."


def format_daily(f: dict, summary: str | None) -> str:
    a = f["account"]
    lines = [f"[DAILY REPORT] {f['day']} (paper trading)"]
    if summary:
        lines.append(summary)
    pr = f["alert_price_range"]
    lines.append(f"Alerts: {f['alerts']}" + (f" (prices {pr[0]:,.2f} to {pr[1]:,.2f}, from your alerts only)" if pr else ""))
    if f["decisions"]:
        lines.append("Decisions: " + ", ".join(f"{k} {v}" for k, v in sorted(f["decisions"].items())))
    if f["blocked_by_shield"]:
        lines.append("Shield blocked: " + ", ".join(f"{k} ({v})" for k, v in f["blocked_by_shield"].items()))
    for s in f["setups"][:5]:
        size = s["size"]
        size = " + ".join(f"{v} {k}" for k, v in size.items()) if isinstance(size, dict) else size
        entry = " - ".join(f"{x:,.2f}" for x in s["entry"] or [])
        targets = " / ".join(f"{x:,.2f}" for x in s["targets"] or [])
        stop = f"{s['stop']:,.2f}" if s["stop"] is not None else "none"
        lines.append(f"Setup: {s['decision']} {s['symbol']} entry {entry} stop {stop} "
                     f"targets {targets} size {size} ({s['risk_verdict']})")
    lines.append(f"Paper trades closed: {f['paper_trades_closed']}, P&L {f['paper_pnl_today']:,.2f}"
                 + (f", R {f['paper_r_today']}" if f['paper_r_today'] else ""))
    if a["drawdown_left"] is not None:
        lines.append(f"Drawdown left: {a['drawdown_left']:,.2f} ({f['full_losses_left']} full losses), "
                     f"floor {a['floor']:,.2f}{' locked' if a['floor_locked'] else ''} ({a['mode']})")
    lines.append(f"DECISION: {one_decision(f)}")
    lines.append("Nothing was sent to a broker.")
    return "\n".join(lines)


def weekly_facts(ses, end_day: date, risk_per_trade: float | None, min_expectancy_r: float = 0.3) -> dict:
    start = end_day - timedelta(days=6)
    journal = [j for j in ses.scalars(select(JournalEntry))
               if j.status == "closed" and j.closed_at and start <= trading_day(j.closed_at) <= end_day]
    rs = [j.r_multiple for j in journal if j.r_multiple is not None]
    wins = [r for r in rs if r > 0]
    analyses = [(a, an) for a, an in ses.execute(select(Alert, Analysis).join(Analysis, Analysis.alert_id == Alert.id))
                if start <= _ny_date(a.received_at) <= end_day]
    by_decision: dict[str, int] = {}
    for _, an in analyses:
        by_decision[an.decision] = by_decision.get(an.decision, 0) + 1
    exp = sum(rs) / len(rs) if rs else None
    return {
        "week": f"{start.isoformat()} to {end_day.isoformat()}",
        "paper_trades": len(journal), "wins": len(wins), "losses": sum(1 for r in rs if r <= FULL_LOSS_R),
        "win_rate_pct": round(len(wins) / len(rs) * 100, 1) if rs else None,
        "avg_r": None if exp is None else round(exp, 3),
        "target_avg_r": min_expectancy_r,
        "meets_target": exp is not None and exp >= min_expectancy_r,
        "net_pnl": round(sum(j.pnl or 0 for j in journal), 2),
        "best_r": max(rs) if rs else None, "worst_r": min(rs) if rs else None,
        "rules_followed": sum(1 for j in journal if j.rules_followed is True),
        "rule_breaks": sum(1 for j in journal if j.rules_followed is False),
        "not_rated": sum(1 for j in journal if j.rules_followed is None),
        "alerts_by_decision": by_decision,
        "avoid_calls": by_decision.get("AVOID", 0),
        "avoid_note": "AVOID outcomes can't be scored here (no market data after the alert): replay a few to check",
        "risk_per_trade": risk_per_trade,
    }


def format_weekly(f: dict, w: dict | None) -> str:
    lines = [f"[WEEKLY REVIEW] {f['week']} (paper trading)"]
    if w:
        lines.append(w.get("headline", ""))
    lines.append(f"Trades {f['paper_trades']} | win rate {f['win_rate_pct']}% | avg {f['avg_r']}R "
                 f"(target +{f['target_avg_r']}R: {'met' if f['meets_target'] else 'not met'}) | net {f['net_pnl']:,.2f}")
    lines.append(f"Rules followed {f['rules_followed']}, broken {f['rule_breaks']}, not rated {f['not_rated']}")
    lines.append(f"Alerts: {', '.join(f'{k} {v}' for k, v in sorted(f['alerts_by_decision'].items())) or 'none'}")
    if w:
        for x in w.get("what_worked", [])[:3]:
            lines.append(f"+ {x}")
        for x in w.get("what_to_fix", [])[:3]:
            lines.append(f"- {x}")
        if w.get("one_change"):
            lines.append(f"ONE CHANGE: {w['one_change']}")
    lines.append(f"Note: {f['avoid_note']}.")
    return "\n".join(lines)


def already_sent(ses, kind: str, day: date) -> bool:
    return ses.scalar(select(ReportLog.id).where(ReportLog.kind == kind, ReportLog.day == day.isoformat())) is not None


def log_sent(ses, kind: str, day: date, text: str) -> None:
    ses.add(ReportLog(kind=kind, day=day.isoformat(), text=text))
    ses.commit()
