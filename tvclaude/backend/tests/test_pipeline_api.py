"""End-to-end workflow (webhook -> analysis -> risk -> save -> WebSocket -> Telegram) and dashboard API."""
import json

import httpx
import pytest

from app.analyst import AnalysisError
from app.infra import Telegram
from conftest import AUTH, TOKEN, FakeAnalyst, make_analysis, payload, wait_for


def send(c, name, **kw):
    r = c.post("/webhook/tradingview", content=json.dumps(payload(name, **kw)))
    assert r.status_code == 202, r.text
    return r.json()["alert_id"]


def latest(alert):
    return alert["analyses"][-1]


class TestWorkflow:
    def test_approved_setup_is_sized(self, make_client):
        fake = FakeAnalyst()
        c = make_client(analyst=fake)
        an = latest(wait_for(c, send(c, "futures_scalp_mnq")))
        assert an["decision"] == "LONG_SETUP" and an["error"] is None
        risk = an["risk"]
        # worst fill 21000, stop 20990 -> 10 pts x $2 = $20/contract; 1% of $10k = $100 -> 5 contracts
        assert (risk["verdict"], risk["quantity"], risk["max_loss"]) == ("APPROVED", 5, 100.0)
        assert an["result"]["risk_reward"] == "1:2 (TP2)"
        call = fake.calls[0]
        assert call["module"] == "scalping" and call["min_rr"] == 2.0
        assert "secret" not in call["context"] and call["context"]["price"] == 21000.25

    def test_risk_rejection_overrides_claude(self, make_client):
        c = make_client(analyst=FakeAnalyst(make_analysis(take_profit=[21005.0, 21012.0])))
        an = latest(wait_for(c, send(c, "futures_scalp_mnq")))
        assert an["claude_decision"] == "LONG_SETUP" and an["decision"] == "AVOID"
        assert an["risk"]["verdict"] == "REJECTED" and any("rejected LONG_SETUP" in w for w in an["warnings"])

    def test_missing_balance_keeps_setup_but_no_size(self, make_client):
        c = make_client(account_balance=None)
        an = latest(wait_for(c, send(c, "futures_scalp_mnq")))
        assert an["decision"] == "LONG_SETUP" and an["risk"]["verdict"] == "NEEDS_INPUT"
        assert an["risk"]["quantity"] is None

    def test_no_price_alert_never_gets_levels(self, make_client):
        c = make_client()
        an = latest(wait_for(c, send(c, "no_price")))
        assert an["decision"] == "WAIT" and an["result"]["entry_zone"] == [] and an["risk"] is None

    def test_missing_inputs_passed_to_claude(self, make_client):
        fake = FakeAnalyst()
        c = make_client(analyst=fake)
        wait_for(c, send(c, "no_price"))
        assert "vwap" in fake.calls[0]["missing"] and "structure" in fake.calls[0]["missing"]

    def test_analysis_failure_recorded_as_wait(self, make_client):
        c = make_client(analyst=FakeAnalyst(error=AnalysisError("refusal", "declined")))
        a = wait_for(c, send(c, "stock_day_aapl"))
        assert a["status"] == "failed"
        an = latest(a)
        assert an["decision"] == "WAIT" and an["error_kind"] == "refusal"

    def test_worker_survives_unexpected_errors(self, make_client):
        class Boom(FakeAnalyst):
            async def analyze(self, *a, **kw):
                raise RuntimeError("kaboom")
        c = make_client(analyst=Boom())
        assert latest(wait_for(c, send(c, "stock_day_aapl")))["error_kind"] == "internal"
        assert latest(wait_for(c, send(c, "stock_day_aapl", signal="again")))["error_kind"] == "internal"

    def test_websocket_receives_events(self, make_client):
        c = make_client()
        with c.websocket_connect(f"/ws?token={TOKEN}") as ws:
            send(c, "futures_scalp_mnq")
            events = [json.loads(ws.receive_text())["event"] for _ in range(3)]
        assert events == ["alert_received", "analysis_started", "analysis_completed"]

    def test_websocket_requires_token(self, make_client):
        c = make_client()
        with pytest.raises(Exception):
            with c.websocket_connect("/ws?token=wrong") as ws:
                ws.receive_text()

    def test_telegram_only_for_actionable_decisions(self, make_client):
        sent = []

        def handler(req):
            sent.append(json.loads(req.content))
            return httpx.Response(200, json={"ok": True})
        tg = Telegram("123:ABC", "42", transport=httpx.MockTransport(handler))
        c = make_client(telegram=tg)
        wait_for(c, send(c, "futures_scalp_mnq"))
        assert len(sent) == 1 and "[PAPER] MNQ1!" in sent[0]["text"] and "No order was placed" in sent[0]["text"]
        c2 = make_client(analyst=FakeAnalyst(make_analysis(decision="WAIT")), telegram=tg)
        wait_for(c2, send(c2, "futures_scalp_mnq"))
        assert len(sent) == 1

    def test_telegram_failure_does_not_break_pipeline(self, make_client):
        tg = Telegram("123:ABC", "42", transport=httpx.MockTransport(lambda r: httpx.Response(500)))
        c = make_client(telegram=tg)
        assert wait_for(c, send(c, "futures_scalp_mnq"))["status"] == "analyzed"


class TestDashboardApi:
    @pytest.mark.parametrize("path", ["/api/status", "/api/alerts", "/api/analyses", "/api/journal", "/api/performance"])
    def test_requires_token(self, make_client, path):
        c = make_client()
        assert c.get(path).status_code == 401
        assert c.get(path, headers={"Authorization": "Bearer nope"}).status_code == 401

    def test_status(self, make_client):
        s = make_client().get("/api/status", headers=AUTH).json()
        assert s["mode"] == "paper" and s["broker_execution"] == "not implemented"
        assert s["database"] == "ok" and s["redis"] == "disabled" and s["workers_running"] == 1
        assert s["risk"]["min_reward_risk"] == 2.0 and "api_key_set" in s["anthropic"]
        assert "key" not in json.dumps(s).replace("api_key_set", "")   # no key material ever returned

    def test_risk_calculator(self, make_client):
        c = make_client()
        r = c.post("/api/risk/calculate", headers=AUTH, json={
            "side": "long", "entry": 21000, "stop_loss": 20990, "take_profit": [21010, 21020],
            "asset_class": "future", "symbol": "MNQ1!", "risk_percent": 0.5}).json()
        assert (r["verdict"], r["quantity"], r["risk_amount"]) == ("APPROVED", 2, 50.0)

    def test_risk_calculator_validates(self, make_client):
        c = make_client()
        assert c.post("/api/risk/calculate", headers=AUTH, json={"side": "up", "entry": 1, "stop_loss": 1,
                                                                 "asset_class": "stock"}).status_code == 422

    def test_journal_from_analysis_then_close(self, make_client):
        c = make_client()
        an = latest(wait_for(c, send(c, "futures_scalp_mnq")))
        j = c.post("/api/journal", headers=AUTH, json={"analysis_id": an["id"]}).json()
        assert (j["side"], j["entry"], j["stop_loss"], j["quantity"], j["multiplier"]) == ("long", 21000, 20990, 5, 2.0)
        closed = c.patch(f"/api/journal/{j['id']}", headers=AUTH, json={"status": "closed", "exit_price": 21020}).json()
        assert closed["r_multiple"] == 2.0 and closed["pnl"] == 200.0     # 20 pts x 5 x $2
        perf = c.get("/api/performance", headers=AUTH).json()["by_strategy"]["scalping"]
        assert perf["journal"] == {"closed_trades": 1, "wins": 1, "win_rate": 1.0, "avg_r": 2.0, "total_pnl": 200.0}
        assert perf["decisions"]["LONG_SETUP"] == 1

    def test_journal_rules(self, make_client):
        c = make_client()
        bad = c.post("/api/journal", headers=AUTH, json={"symbol": "AAPL", "side": "long", "entry": 100, "stop_loss": 101})
        assert bad.status_code == 422
        ok = c.post("/api/journal", headers=AUTH, json={"symbol": "AAPL", "side": "short", "entry": 100,
                                                        "stop_loss": 101, "quantity": 10}).json()
        r = c.patch(f"/api/journal/{ok['id']}", headers=AUTH, json={"status": "closed"})
        assert r.status_code == 422                      # closing needs an exit price
        done = c.patch(f"/api/journal/{ok['id']}", headers=AUTH, json={"status": "closed", "exit_price": 101}).json()
        assert done["r_multiple"] == -1.0 and done["pnl"] == -10.0

    def test_filters_and_404(self, make_client):
        c = make_client()
        wait_for(c, send(c, "futures_scalp_mnq"))
        wait_for(c, send(c, "stock_day_aapl"))
        assert [a["ticker"] for a in c.get("/api/alerts?ticker=aapl", headers=AUTH).json()] == ["AAPL"]
        assert all(a["decision"] == "LONG_SETUP" for a in c.get("/api/analyses?decision=LONG_SETUP", headers=AUTH).json())
        assert c.get("/api/alerts/9999", headers=AUTH).status_code == 404


class TestSettingsSafety:
    def test_live_mode_refused(self, tmp_path):
        from conftest import make_settings
        with pytest.raises(ValueError, match="paper"):
            make_settings(tmp_path, trading_mode="live")

    def test_risk_percent_must_be_allowed(self, tmp_path):
        from conftest import make_settings
        with pytest.raises(ValueError, match="allowed"):
            make_settings(tmp_path, risk_percent=3.0)

    def test_short_secrets_refused(self, tmp_path):
        from conftest import make_settings
        with pytest.raises(ValueError):
            make_settings(tmp_path, webhook_secret="short")


class TestEnvFile:
    def test_example_env_loads_with_blanks(self, tmp_path, monkeypatch):
        """The shipped .env.example, with only the required values filled, must parse; blanks mean 'not set'."""
        from pathlib import Path
        from app.config import Settings
        text = (Path(__file__).parents[2] / ".env.example").read_text()
        text = text.replace("WEBHOOK_SECRET=\n", "WEBHOOK_SECRET=" + "w" * 20 + "\n")
        text = text.replace("DASHBOARD_TOKEN=\n", "DASHBOARD_TOKEN=" + "d" * 20 + "\n")
        env = tmp_path / ".env"
        env.write_text(text)
        for k in ("ACCOUNT_BALANCE", "TELEGRAM_BOT_TOKEN", "ANTHROPIC_API_KEY", "WEBHOOK_SECRET", "DASHBOARD_TOKEN",
                  "CONTRACT_SPECS_FILE", "DRAWDOWN_LIMIT", "DRAWDOWN_MODE", "DISCORD_WEBHOOK_URL",
                  "ENTRY_WINDOW", "DAILY_LOSS_LIMIT"):
            monkeypatch.delenv(k, raising=False)
        s = Settings(_env_file=str(env))
        assert s.telegram_bot_token is None and not s.telegram_enabled and s.anthropic_api_key is None
        assert (s.account_balance, s.risk_percent, s.drawdown_limit, s.drawdown_mode) == (25000, 1.0, 1000, "eod_trailing")
        assert (s.drawdown_lock_above_start, s.account_phase, s.daily_loss_limit) == (100, "evaluation", 500)
        assert (s.max_consecutive_losses, s.entry_window, s.discord_webhook_url) == (2, "09:35-11:30", None)
        assert s.allowed_futures_roots == ["NQ", "MNQ"] and s.allowed_asset_classes == ["future"]
        assert (s.min_reward_risk, s.rr_target_index, s.target_ladder, s.max_target_r) == (2.0, 1, [2, 3, 5], 5)
        assert s.contract_specs_file == "../config/nasdaq_futures.json"   # inline comment stripped
        assert s.allowed_risk_percents == [0.5, 1.0, 2.0]
        assert s.notify_decisions == ["LONG_SETUP", "SHORT_SETUP"] and s.trading_mode == "paper"

    def test_api_key_from_settings_reaches_sdk(self):
        from app.analyst import ClaudeAnalyst
        a = ClaudeAnalyst("claude-opus-5-5", "medium", 30, api_key="sk-ant-test-not-real")
        assert a.client.api_key == "sk-ant-test-not-real"
