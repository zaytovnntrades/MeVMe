"""FastAPI application: TradingView webhook, dashboard API and WebSocket."""
import asyncio
import hmac
import json
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from sqlalchemy import func, select

from . import account, classify, shield, strategies
from .analyst import ClaudeAnalyst
from .config import Settings, get_settings
from .db import Alert, Analysis, Database, JournalEntry, utcnow
from .infra import Deduper, Discord, Hub, MemoryQueue, Notifier, RateLimiter, RedisQueue, Telegram
from .pipeline import Services, side_for, worker
from .risk import ContractSpecs, RiskInputs, RiskManager, futures_root
from .schemas import (AssetClass, HaltRequest, JournalCreate, JournalUpdate, RiskRequest, TradingViewAlert,
                      WebhookAck)
from .serialize import alert_dict, analysis_dict, journal_dict

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("tvclaude")


def create_app(settings: Settings | None = None, analyst=None, telegram: Telegram | None = None,
               discord: Discord | None = None, start_workers: bool = True, clock=None) -> FastAPI:
    settings = settings or get_settings()
    db = Database(settings.database_url)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        db.create_all()
        redis = None
        if settings.redis_url:
            import redis.asyncio as aioredis
            redis = aioredis.from_url(settings.redis_url, decode_responses=True)
        app.state.redis = redis
        svc = Services(
            settings=settings, db=db,
            analyst=analyst or ClaudeAnalyst(settings.anthropic_model, settings.analysis_effort,
                                             settings.analysis_timeout_s, api_key=settings.anthropic_api_key),
            risk=RiskManager(ContractSpecs.load(settings.contract_specs_file), settings.allowed_risk_percents,
                             settings.min_reward_risk, settings.rr_target_index, settings.max_target_r,
                             settings.allowed_futures_roots),
            queue=RedisQueue(redis) if redis else MemoryQueue(),
            hub=Hub(), clock=clock,
            notifier=Notifier(telegram or Telegram(settings.telegram_bot_token, settings.telegram_chat_id),
                              discord or Discord(settings.discord_webhook_url)))
        app.state.svc = svc
        app.state.limiter = RateLimiter(settings.rate_limit_per_minute, redis)
        app.state.dedup = Deduper(60, redis)
        tasks = [asyncio.create_task(worker(svc, i)) for i in range(settings.analysis_workers)] if start_workers else []
        app.state.workers = tasks
        log.info("ready: mode=%s model=%s workers=%d redis=%s", settings.trading_mode, settings.anthropic_model,
                 len(tasks), "on" if redis else "off")
        yield
        for t in tasks:
            t.cancel()
        if redis is not None:
            await redis.aclose()

    app = FastAPI(title="TradingView x Claude analyst", version="1.0.0", lifespan=lifespan,
                  docs_url="/docs", redoc_url=None)
    app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origins, allow_methods=["GET", "POST", "PATCH"],
                       allow_headers=["Authorization", "Content-Type"])
    app.state.settings = settings

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception):
        log.exception("unhandled error on %s %s", request.method, request.url.path)
        return JSONResponse({"detail": "internal error"}, status_code=500)

    # ------------------------------------------------------------------ auth helpers

    def client_ip(request: Request) -> str:
        if settings.trust_proxy and (xff := request.headers.get("x-forwarded-for")):
            return xff.split(",")[0].strip()
        return request.client.host if request.client else "unknown"

    def require_dashboard(authorization: str | None = Header(default=None)) -> None:
        token = (authorization or "").removeprefix("Bearer ").strip()
        if not token or not hmac.compare_digest(token, settings.dashboard_token):
            raise HTTPException(401, "missing or invalid dashboard token")

    def svc() -> Services:
        return app.state.svc

    # ------------------------------------------------------------------ webhook

    @app.post("/webhook/tradingview", status_code=202, response_model=WebhookAck)
    async def tradingview_webhook(request: Request, token: str | None = Query(default=None),
                                  x_webhook_secret: str | None = Header(default=None)):
        ip = client_ip(request)
        if settings.tradingview_ip_allowlist and ip not in settings.tradingview_ip_allowlist:
            raise HTTPException(403, "source address not allowed")
        if not await app.state.limiter.allow(f"webhook:{ip}"):
            raise HTTPException(429, "rate limit exceeded")
        body = await request.body()   # TradingView sends text/plain, so parse it ourselves
        if len(body) > settings.max_body_bytes:
            raise HTTPException(413, "payload too large")
        try:
            raw = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise HTTPException(400, "body must be JSON (check the TradingView alert message)")
        if not isinstance(raw, dict):
            raise HTTPException(400, "body must be a JSON object")
        supplied = x_webhook_secret or raw.get("secret") or token or ""
        if not hmac.compare_digest(str(supplied), settings.webhook_secret):
            log.warning("webhook auth failed from %s", ip)
            raise HTTPException(401, "invalid webhook secret")
        try:
            alert = TradingViewAlert.model_validate(raw)
        except ValidationError as e:
            errs = [{"field": ".".join(str(p) for p in err["loc"]), "error": err["msg"]} for err in e.errors()]
            raise HTTPException(422, {"message": "invalid alert payload", "errors": errs})
        try:
            tf = classify.normalise_timeframe(alert.timeframe)
            module = strategies.select(tf, alert.strategy)
        except ValueError as e:
            raise HTTPException(422, {"message": str(e)})
        stored = alert.model_dump(mode="json", exclude_none=True)   # `secret` is excluded by the model
        if not await app.state.dedup.first_time(stored):
            return WebhookAck(status="duplicate", alert_id=None)
        symbol, _ = classify.split_ticker(alert.ticker, alert.exchange)
        asset = classify.asset_class(alert).value
        if settings.allowed_asset_classes and asset not in settings.allowed_asset_classes:
            raise HTTPException(422, {"message": f"{symbol} is a {asset}; this system only analyses "
                                                 f"{', '.join(settings.allowed_asset_classes)}"})
        if asset == "future" and settings.allowed_futures_roots and futures_root(symbol) not in settings.allowed_futures_roots:
            raise HTTPException(422, {"message": f"{futures_root(symbol)} is not allowed; this system only analyses "
                                                 f"{', '.join(settings.allowed_futures_roots)} futures"})
        row = Alert(ticker=symbol, asset_class=asset, timeframe=tf,
                    strategy=module.name, price=alert.price, signal=alert.signal, payload=stored, client_ip=ip)

        def _insert() -> Alert:
            with svc().db.Session() as ses:
                ses.add(row)
                ses.commit()
                ses.refresh(row)
                return row

        saved = await asyncio.to_thread(_insert)
        await svc().queue.put(saved.id)
        await svc().hub.broadcast("alert_received", alert_dict(saved))
        return WebhookAck(status="accepted", alert_id=saved.id, asset_class=saved.asset_class,
                          timeframe=saved.timeframe, strategy=saved.strategy,
                          received_at=saved.received_at.replace(tzinfo=saved.received_at.tzinfo or timezone.utc))

    # ------------------------------------------------------------------ dashboard API

    @app.get("/healthz")
    async def healthz():
        return {"ok": True}

    @app.get("/api/status", dependencies=[Depends(require_dashboard)])
    async def status():
        s = svc()
        redis_state = "disabled"
        if app.state.redis is not None:
            try:
                await app.state.redis.ping()
                redis_state = "ok"
            except Exception:  # noqa: BLE001
                redis_state = "error"
        return {
            "mode": settings.trading_mode, "broker_execution": "not implemented",
            "database": "ok" if await asyncio.to_thread(s.db.ping) else "error", "redis": redis_state,
            "anthropic": {"model": settings.anthropic_model, "effort": settings.analysis_effort,
                          "api_key_set": bool(settings.anthropic_api_key or os.environ.get("ANTHROPIC_API_KEY"))},
            "telegram": s.notifier.status().get("telegram", "off"),
            "discord": s.notifier.status().get("discord", "off"),
            "shield": await asyncio.to_thread(shield_state),
            "queue_depth": await s.queue.depth(),
            "workers_running": sum(1 for t in app.state.workers if not t.done()),
            "account": (await asyncio.to_thread(account_state)).to_dict(),
            "instruments": {"asset_classes": settings.allowed_asset_classes or "all",
                            "futures_roots": settings.allowed_futures_roots or "all"},
            "risk": {"account_balance_set": settings.account_balance is not None,
                     "risk_per_trade": (settings.account_balance * settings.risk_percent / 100
                                        if settings.account_balance else None),
                     "target_ladder": settings.target_ladder, "max_target_r": settings.max_target_r,
                     "risk_percent": settings.risk_percent, "allowed": settings.allowed_risk_percents,
                     "min_reward_risk": settings.min_reward_risk, "rr_target": f"TP{settings.rr_target_index}"},
        }

    @app.get("/api/alerts", dependencies=[Depends(require_dashboard)])
    async def list_alerts(limit: int = Query(50, ge=1, le=500), ticker: str | None = None):
        def q():
            with svc().db.Session() as ses:
                stmt = select(Alert).order_by(Alert.id.desc()).limit(limit)
                if ticker:
                    stmt = stmt.where(Alert.ticker == ticker.upper())
                return [alert_dict(a) for a in ses.scalars(stmt)]
        return await asyncio.to_thread(q)

    @app.get("/api/alerts/{alert_id}", dependencies=[Depends(require_dashboard)])
    async def get_alert(alert_id: int):
        def q():
            with svc().db.Session() as ses:
                a = ses.get(Alert, alert_id)
                if a is None:
                    return None
                return {**alert_dict(a), "analyses": [analysis_dict(r) for r in a.analyses]}
        res = await asyncio.to_thread(q)
        if res is None:
            raise HTTPException(404, "alert not found")
        return res

    @app.get("/api/analyses", dependencies=[Depends(require_dashboard)])
    async def list_analyses(limit: int = Query(50, ge=1, le=500), decision: str | None = None,
                            ticker: str | None = None):
        def q():
            with svc().db.Session() as ses:
                stmt = select(Analysis, Alert).join(Alert).order_by(Analysis.id.desc()).limit(limit)
                if decision:
                    stmt = stmt.where(Analysis.decision == decision)
                if ticker:
                    stmt = stmt.where(Alert.ticker == ticker.upper())
                return [analysis_dict(r, a) for r, a in ses.execute(stmt)]
        return await asyncio.to_thread(q)

    def account_state():
        return svc().account_state()

    def shield_state() -> dict:
        s = svc()
        halt = shield.get_halt(s.db)
        acct = s.account_state()
        blocks = shield.precheck(now=s.now(), halted=halt.halted, halt_reason=halt.reason, acct=acct,
                                 risk_per_trade=s.risk_per_trade(), entry_window=settings.entry_window,
                                 daily_loss_limit=settings.daily_loss_limit,
                                 max_consecutive_losses=settings.max_consecutive_losses)
        warnings = []
        if settings.account_phase == "funded" and settings.drawdown_mode == "eod_trailing":
            warnings.append("funded accounts may use an INTRADAY trailing drawdown that counts open profit: "
                            "confirm your firm's funded rule before relying on eod_trailing")
        return {"halted": halt.halted, "halt_reason": halt.reason, "phase": settings.account_phase,
                "entry_window": settings.entry_window, "daily_loss_limit": settings.daily_loss_limit,
                "max_consecutive_losses": settings.max_consecutive_losses,
                "new_setups_allowed_now": not blocks, "would_block": [b.reason for b in blocks],
                "warnings": warnings}

    @app.get("/api/shield", dependencies=[Depends(require_dashboard)])
    async def get_shield():
        return await asyncio.to_thread(shield_state)

    @app.post("/api/shield/halt", dependencies=[Depends(require_dashboard)])
    async def halt(body: HaltRequest):
        await asyncio.to_thread(shield.set_halt, svc().db, True, body.reason or "stopped from the dashboard")
        st = await asyncio.to_thread(shield_state)
        await svc().hub.broadcast("shield_changed", st)
        await svc().notifier.send(f"[SHIELD] Kill switch ON: {st['halt_reason']}. Every new alert is blocked "
                                  "until you resume.")
        return st

    @app.post("/api/shield/resume", dependencies=[Depends(require_dashboard)])
    async def resume():
        await asyncio.to_thread(shield.set_halt, svc().db, False, None)
        st = await asyncio.to_thread(shield_state)
        await svc().hub.broadcast("shield_changed", st)
        await svc().notifier.send("[SHIELD] Kill switch OFF: new alerts are analysed again.")
        return st

    @app.get("/api/account", dependencies=[Depends(require_dashboard)])
    async def get_account():
        return (await asyncio.to_thread(account_state)).to_dict()

    @app.post("/api/risk/calculate", dependencies=[Depends(require_dashboard)])
    async def risk_calculate(req: RiskRequest):
        acct = await asyncio.to_thread(account_state)
        rep = svc().risk.evaluate(RiskInputs(
            side=req.side, entry=[req.entry], stop_loss=req.stop_loss, take_profit=req.take_profit,
            asset_class=req.asset_class, symbol=req.symbol or "",
            account_balance=req.account_balance if req.account_balance is not None else settings.account_balance,
            risk_percent=req.risk_percent if req.risk_percent is not None else settings.risk_percent,
            multiplier=req.multiplier, drawdown_remaining=acct.remaining))
        return rep.to_dict()

    # ------------------------------------------------------------------ paper journal

    @app.get("/api/journal", dependencies=[Depends(require_dashboard)])
    async def list_journal(limit: int = Query(100, ge=1, le=1000)):
        def q():
            with svc().db.Session() as ses:
                return [journal_dict(j) for j in ses.scalars(select(JournalEntry).order_by(JournalEntry.id.desc()).limit(limit))]
        return await asyncio.to_thread(q)

    @app.post("/api/journal", status_code=201, dependencies=[Depends(require_dashboard)])
    async def create_journal(body: JournalCreate):
        def q():
            with svc().db.Session() as ses:
                spec = svc().risk.specs.futures.get(futures_root(body.symbol or ""))
                fields = {"symbol": body.symbol, "side": body.side, "entry": body.entry, "stop_loss": body.stop_loss,
                          "take_profit": body.take_profit, "quantity": body.quantity,
                          "multiplier": body.multiplier or (spec or {}).get("multiplier") or 1.0, "strategy": None}
                if body.analysis_id is None and body.multiplier is None and not spec and settings.allowed_futures_roots:
                    raise HTTPException(422, f"no contract spec for {body.symbol}: pass multiplier ($ per point)")
                if body.analysis_id is not None:
                    an = ses.get(Analysis, body.analysis_id)
                    if an is None or not an.result:
                        raise HTTPException(404, "analysis not found or has no setup")
                    r, risk = an.result, an.risk or {}
                    fields = {"symbol": body.symbol or r["symbol"],
                              "side": body.side or side_for(r["decision"], r["market_bias"]) or risk.get("side"),
                              "entry": body.entry or risk.get("entry_reference") or (r["entry_zone"] or [None])[-1],
                              "stop_loss": body.stop_loss or r["stop_loss"],
                              "take_profit": body.take_profit or r["take_profit"],
                              "quantity": body.quantity or risk.get("quantity"),
                              "multiplier": risk.get("multiplier") or 1.0, "strategy": r["strategy"]}
                missing = [k for k in ("symbol", "side", "entry", "stop_loss") if not fields.get(k)]
                if missing:
                    raise HTTPException(422, f"journal entry needs {missing}")
                long = fields["side"] == "long"
                if (fields["stop_loss"] >= fields["entry"]) if long else (fields["stop_loss"] <= fields["entry"]):
                    raise HTTPException(422, "stop-loss is on the wrong side of entry")
                j = JournalEntry(analysis_id=body.analysis_id, notes=body.notes, **fields)
                ses.add(j)
                ses.commit()
                ses.refresh(j)
                return journal_dict(j)
        return await asyncio.to_thread(q)

    @app.patch("/api/journal/{entry_id}", dependencies=[Depends(require_dashboard)])
    async def update_journal(entry_id: int, body: JournalUpdate):
        def q():
            with svc().db.Session() as ses:
                j = ses.get(JournalEntry, entry_id)
                if j is None:
                    raise HTTPException(404, "journal entry not found")
                if body.notes is not None:
                    j.notes = body.notes
                if body.status:
                    if body.status == "closed" and body.exit_price is None and j.exit_price is None:
                        raise HTTPException(422, "closing an entry needs exit_price")
                    j.status = body.status
                if body.exit_price is not None:
                    j.exit_price = body.exit_price
                if j.status == "closed" and j.exit_price is not None:
                    sign = 1 if j.side == "long" else -1
                    move = (j.exit_price - j.entry) * sign
                    j.r_multiple = round(move / abs(j.entry - j.stop_loss), 3)
                    j.pnl = round(move * j.quantity * (j.multiplier or 1), 2) if j.quantity else None
                    j.closed_at = j.closed_at or utcnow()
                ses.commit()
                ses.refresh(j)
                return journal_dict(j)
        return await asyncio.to_thread(q)

    @app.get("/api/performance", dependencies=[Depends(require_dashboard)])
    async def performance(days: int = Query(90, ge=1, le=3650)):
        since = datetime.now(timezone.utc) - timedelta(days=days)

        def q():
            with svc().db.Session() as ses:
                out = {}
                rows = ses.execute(select(Alert.strategy, Analysis.decision, func.count(), func.avg(Analysis.latency_ms))
                                   .join(Alert).where(Analysis.created_at >= since)
                                   .group_by(Alert.strategy, Analysis.decision)).all()
                for strat, decision, n, lat in rows:
                    s = out.setdefault(strat, {"analyses": 0, "decisions": {}, "avg_latency_ms": None, "journal": {}})
                    s["analyses"] += n
                    s["decisions"][decision] = n
                    s["avg_latency_ms"] = round(lat) if lat else s["avg_latency_ms"]
                closed = ses.scalars(select(JournalEntry).where(JournalEntry.status == "closed",
                                                                JournalEntry.created_at >= since)).all()
                for strat in {j.strategy or "manual" for j in closed}:
                    js = [j for j in closed if (j.strategy or "manual") == strat and j.r_multiple is not None]
                    wins = [j for j in js if j.r_multiple > 0]
                    out.setdefault(strat, {"analyses": 0, "decisions": {}, "avg_latency_ms": None, "journal": {}})
                    out[strat]["journal"] = {
                        "closed_trades": len(js), "wins": len(wins),
                        "win_rate": round(len(wins) / len(js), 3) if js else None,
                        "avg_r": round(sum(j.r_multiple for j in js) / len(js), 3) if js else None,
                        "total_pnl": round(sum(j.pnl or 0 for j in js), 2)}
                return {"days": days, "by_strategy": out,
                        "note": "Paper results. Past performance does not predict future results."}
        return await asyncio.to_thread(q)

    @app.websocket("/ws")
    async def ws_endpoint(ws: WebSocket, token: str | None = Query(default=None)):
        if not token or not hmac.compare_digest(token, settings.dashboard_token):
            await ws.close(code=4401)
            return
        await ws.accept()
        await svc().hub.add(ws)
        try:
            while True:
                await ws.receive_text()   # keep-alive pings from the dashboard
        except WebSocketDisconnect:
            pass
        finally:
            svc().hub.remove(ws)

    return app


def app_factory() -> FastAPI:  # uvicorn app.main:app_factory --factory
    return create_app()
