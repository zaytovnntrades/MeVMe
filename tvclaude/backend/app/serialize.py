"""ORM rows -> JSON-safe dicts for the API and WebSocket."""
from datetime import datetime, timezone

from .db import Alert, Analysis, JournalEntry


def iso(dt: datetime | None) -> str | None:
    """Always an explicit UTC timestamp (SQLite drops tzinfo; browsers would read it as local time)."""
    if dt is None:
        return None
    return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).astimezone(timezone.utc).isoformat()


def alert_dict(a: Alert) -> dict:
    return {"id": a.id, "received_at": iso(a.received_at), "ticker": a.ticker,
            "asset_class": a.asset_class, "timeframe": a.timeframe, "strategy": a.strategy, "price": a.price,
            "signal": a.signal, "status": a.status, "payload": a.payload}


def analysis_dict(r: Analysis, alert: Alert | None = None) -> dict:
    d = {"id": r.id, "alert_id": r.alert_id, "created_at": iso(r.created_at),
         "model": r.model, "decision": r.decision, "claude_decision": r.claude_decision,
         "market_bias": r.market_bias, "confidence": r.confidence, "setup_quality": r.setup_quality,
         "result": r.result, "risk": r.risk, "warnings": r.warnings or [], "error_kind": r.error_kind,
         "error": r.error, "latency_ms": r.latency_ms, "input_tokens": r.input_tokens,
         "output_tokens": r.output_tokens}
    if alert is not None:
        d["alert"] = {k: v for k, v in alert_dict(alert).items() if k != "payload"}
    return d


def journal_dict(j: JournalEntry) -> dict:
    return {"id": j.id, "created_at": iso(j.created_at),
            "analysis_id": j.analysis_id, "symbol": j.symbol, "strategy": j.strategy, "side": j.side,
            "entry": j.entry, "stop_loss": j.stop_loss, "take_profit": j.take_profit, "quantity": j.quantity,
            "multiplier": j.multiplier, "status": j.status, "exit_price": j.exit_price, "pnl": j.pnl,
            "r_multiple": j.r_multiple, "closed_at": iso(j.closed_at),
            "notes": j.notes, "rules_followed": j.rules_followed}
