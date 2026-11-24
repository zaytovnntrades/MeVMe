"""Coordinator: the only voice you hear. Sends one daily report after DAILY_REPORT_TIME (New York, weekdays)
and the weekly review on WEEKLY_REVIEW_DAY, each once, to Telegram and Discord. Never acts on a trade."""
import asyncio
import logging
from datetime import datetime, time, timezone

from . import reports, shield
from .account import NY

log = logging.getLogger("tvclaude.coordinator")
DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


async def build_daily(s, with_summary: bool) -> dict:
    day = s.now().astimezone(NY).date()

    def facts():
        acct = s.account_state()
        halt = shield.get_halt(s.db)
        cfg = {"entry_window": s.settings.entry_window, "daily_loss_limit": s.settings.daily_loss_limit,
               "max_consecutive_losses": s.settings.max_consecutive_losses}
        with s.db.Session() as ses:
            return reports.daily_facts(ses, day, acct, halt.halted, halt.reason, s.risk_per_trade(), cfg)

    f = await asyncio.to_thread(facts)
    w = await s.writer.write("daily", f) if (with_summary and s.writer) else None
    summary = w.get("summary") if w else None
    return {"day": day.isoformat(), "facts": f, "decision": reports.one_decision(f), "summary": summary,
            "text": reports.format_daily(f, summary)}


async def build_weekly(s, with_summary: bool) -> dict:
    day = s.now().astimezone(NY).date()

    def facts():
        with s.db.Session() as ses:
            return reports.weekly_facts(ses, day, s.risk_per_trade())

    f = await asyncio.to_thread(facts)
    w = await s.writer.write("weekly", f) if (with_summary and s.writer and f["paper_trades"]) else None
    return {"day": day.isoformat(), "facts": f, "review": w, "text": reports.format_weekly(f, w)}


async def send(s, kind: str, report: dict) -> dict:
    """Send and log once per kind per New York date. Returns what each channel said."""
    day = datetime.fromisoformat(report["day"]).date()

    def check_and_log():
        with s.db.Session() as ses:
            if reports.already_sent(ses, kind, day):
                return False
            reports.log_sent(ses, kind, day, report["text"])
            return True

    if not await asyncio.to_thread(check_and_log):
        return {"sent": False, "reason": f"{kind} report for {report['day']} was already sent"}
    channels = await s.notifier.send(report["text"]) if s.notifier.enabled else {}
    return {"sent": True, "channels": channels}


def due(s, kind: str) -> bool:
    st = s.settings
    if not st.daily_report_time:
        return False
    now = s.now().astimezone(NY)
    at = time.fromisoformat(st.daily_report_time)
    if now.weekday() >= 5 or now.time() < at:
        return False
    return kind == "daily" or DAYS[now.weekday()] == st.weekly_review_day


async def tick(s) -> list[str]:
    """One scheduler step; safe to call any time. Returns the kinds it sent."""
    sent = []
    for kind, build in (("daily", build_daily), ("weekly", build_weekly)):
        if due(s, kind):
            day = s.now().astimezone(NY).date()

            def done():
                with s.db.Session() as ses:
                    return reports.already_sent(ses, kind, day)
            if await asyncio.to_thread(done):
                continue
            r = await build(s, with_summary=True)
            if (await send(s, kind, r))["sent"]:
                sent.append(kind)
    return sent


async def loop(s, every_s: float = 60) -> None:
    log.info("coordinator started (daily report %s New York, weekly on %s)",
             s.settings.daily_report_time or "off", s.settings.weekly_review_day)
    while True:
        try:
            await tick(s)
        except Exception:  # noqa: BLE001 - a failed report must never stop the loop
            log.exception("coordinator tick failed")
        await asyncio.sleep(every_s)
