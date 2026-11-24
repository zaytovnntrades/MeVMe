"""Background workflow for one alert:
Claude analysis -> guard -> risk manager -> save -> dashboard update -> Telegram."""
import asyncio
import logging
from dataclasses import dataclass

from . import account, strategies
from .analyst import AnalysisError, guard
from .config import Settings
from .db import Alert, Analysis, Database
from .infra import Hub, Telegram, format_telegram
from .risk import RiskInputs, RiskManager
from .schemas import AssetClass
from .serialize import analysis_dict, iso

log = logging.getLogger("tvclaude.pipeline")
ACTIONABLE = {"LONG_SETUP", "SHORT_SETUP", "WAIT_FOR_CONFIRMATION"}


@dataclass
class Services:
    settings: Settings
    db: Database
    analyst: object          # ClaudeAnalyst or a test double with the same analyze() signature
    risk: RiskManager
    queue: object
    hub: Hub
    telegram: Telegram


def claude_context(alert: Alert) -> dict:
    p = dict(alert.payload)
    p.pop("secret", None)
    return {**p, "symbol": alert.ticker, "asset_class": alert.asset_class, "timeframe": alert.timeframe,
            "strategy": alert.strategy, "received_at": iso(alert.received_at)}


def account_rules(st: Settings) -> list[str]:
    """Operator rules Claude is told about. The risk manager enforces them in code regardless."""
    rules = []
    if st.allowed_futures_roots:
        rules.append(f"Instruments: {', '.join(st.allowed_futures_roots)} futures only.")
    i = st.rr_target_index
    rules.append(f"TP{i} must be at least {st.min_reward_risk:g}R from the entry (R = entry-to-stop distance).")
    if st.target_ladder:
        ladder = ", ".join(f"TP{n + 1} near {r:g}R" for n, r in enumerate(st.target_ladder))
        rules.append(f"Ladder targets where supplied structure allows: {ladder}. If no level supports TP{i} "
                     f"at {st.min_reward_risk:g}R, do not force one: decide WAIT or AVOID.")
    if st.max_target_r:
        rules.append(f"Do not place targets beyond {st.max_target_r:g}R.")
    if st.account_balance and st.drawdown_limit:
        rules.append(f"Account: {st.account_balance:,.0f} with a {st.drawdown_limit:,.0f} {st.drawdown_mode} "
                     f"drawdown limit; each trade risks {st.risk_percent:g}% "
                     f"({st.account_balance * st.risk_percent / 100:,.0f}). Capital preservation comes first.")
    return rules


def side_for(decision: str, bias: str) -> str | None:
    if decision == "LONG_SETUP" or (decision == "WAIT_FOR_CONFIRMATION" and bias == "BULLISH"):
        return "long"
    if decision == "SHORT_SETUP" or (decision == "WAIT_FOR_CONFIRMATION" and bias == "BEARISH"):
        return "short"
    return None


async def process(alert_id: int, s: Services) -> Analysis:
    alert = await asyncio.to_thread(_load_and_mark, s.db, alert_id)
    await s.hub.broadcast("analysis_started", {"alert_id": alert_id})
    module = strategies.MODULES[alert.strategy]
    payload = alert.payload
    missing = module.missing_inputs(payload.get("indicators") or {}, bool(payload.get("structure")),
                                    "volume" in (payload.get("indicators") or {}))
    row = Analysis(alert_id=alert_id, decision="WAIT", warnings=[])
    try:
        raw, meta = await s.analyst.analyze(claude_context(alert), module, s.settings.min_reward_risk, missing,
                                            rules=account_rules(s.settings))
        row.model, row.latency_ms = meta.model, meta.latency_ms
        row.input_tokens, row.output_tokens = meta.input_tokens, meta.output_tokens
        row.claude_decision = raw.decision
        g = guard(raw, symbol=alert.ticker, asset_class=alert.asset_class, timeframe=alert.timeframe,
                  strategy=module, price=alert.price)
        a, warnings = g.analysis, list(g.warnings)
        if meta.served_by_fallback:
            warnings.append(f"answered by fallback model {meta.model} after a decline")

        risk_report = None
        side = side_for(a.decision, a.market_bias)
        if side and a.entry_zone and a.stop_loss is not None:
            opt = payload.get("option") or {}
            acct = await asyncio.to_thread(account.load, s.db, s.settings.account_balance,
                                           s.settings.drawdown_limit, s.settings.drawdown_mode)
            rep = s.risk.evaluate(RiskInputs(
                side=side, entry=list(a.entry_zone), stop_loss=a.stop_loss, take_profit=list(a.take_profit),
                asset_class=AssetClass(alert.asset_class), symbol=alert.ticker,
                account_balance=s.settings.account_balance, risk_percent=s.settings.risk_percent,
                multiplier=opt.get("multiplier"), option_premium=opt.get("ask") or opt.get("last"),
                drawdown_remaining=acct.remaining))
            risk_report = rep.to_dict()
            basis = int(rep.reward_risk_basis[2:]) - 1 if rep.reward_risk_basis else None
            if basis is not None:
                a = a.model_copy(update={"risk_reward": f"1:{rep.reward_risk[basis]:g} ({rep.reward_risk_basis})"})
            if rep.verdict == "REJECTED" and a.decision in ACTIONABLE:
                warnings.append(f"risk manager rejected {a.decision}: " + "; ".join(rep.reasons))
                a = a.model_copy(update={"decision": "AVOID"})
        row.result, row.risk, row.warnings = a.model_dump(), risk_report, warnings
        row.decision, row.market_bias = a.decision, a.market_bias
        row.confidence, row.setup_quality = a.confidence, a.setup_quality
        status = "analyzed"
    except AnalysisError as e:
        row.error_kind, row.error = e.kind, str(e)
        row.warnings = ["no analysis: decision defaults to WAIT"]
        status = "failed"
        log.warning("analysis failed for alert %s: %s (%s)", alert_id, e.kind, e)
    except Exception as e:  # noqa: BLE001 - one bad alert must not kill the worker
        row.error_kind, row.error = "internal", f"{type(e).__name__}: {e}"
        row.warnings = ["no analysis: decision defaults to WAIT"]
        status = "failed"
        log.exception("unexpected failure analysing alert %s", alert_id)

    saved = await asyncio.to_thread(_save, s.db, row, status)
    data = analysis_dict(saved, alert)
    await s.hub.broadcast("analysis_completed" if status == "analyzed" else "analysis_failed", data)
    if status == "analyzed" and saved.decision in s.settings.notify_decisions and s.telegram.enabled:
        await s.telegram.send(format_telegram(saved.result, saved.risk))
    return saved


def _load_and_mark(db: Database, alert_id: int) -> Alert:
    with db.Session() as ses:
        alert = ses.get(Alert, alert_id)
        if alert is None:
            raise LookupError(f"alert {alert_id} not found")
        alert.status = "analyzing"
        ses.commit()
        return alert


def _save(db: Database, row: Analysis, status: str) -> Analysis:
    with db.Session() as ses:
        ses.add(row)
        alert = ses.get(Alert, row.alert_id)
        alert.status = status
        ses.commit()
        ses.refresh(row)
        return row


async def worker(s: Services, n: int) -> None:
    log.info("analysis worker %d started", n)
    while True:
        alert_id = await s.queue.get()
        try:
            await process(alert_id, s)
        except LookupError as e:
            log.warning("%s", e)
        except Exception:  # noqa: BLE001
            log.exception("worker %d crashed on alert %s; continuing", n, alert_id)
