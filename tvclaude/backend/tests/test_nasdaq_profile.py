"""The user's configuration: NQ/MNQ futures only, $25,000 account, $250 (1%) per trade,
$1,000 prop-firm drawdown, TP1 >= 2R with targets laddered to 5R."""
import json
from pathlib import Path

import pytest

from app import account
from app.db import JournalEntry
from app.pipeline import account_rules
from conftest import AUTH, FakeAnalyst, make_analysis, make_settings, payload, wait_for

SPECS = str(Path(__file__).parents[2] / "config" / "nasdaq_futures.json")
PROFILE = dict(account_balance=25_000, risk_percent=1.0, drawdown_limit=1_000, drawdown_mode="static",
               allowed_asset_classes=["future"], allowed_futures_roots=["NQ", "MNQ"], min_reward_risk=2.0,
               rr_target_index=1, max_target_r=5, target_ladder=[2, 3, 5], contract_specs_file=SPECS)

# NQ at 21000, 10-point stop -> R = 10: TP1 2R, TP2 3R, TP3 5R
NQ_SETUP = dict(symbol="NQ1!", entry_zone=[20998.0, 21000.0], stop_loss=20990.0,
                take_profit=[21020.0, 21030.0, 21050.0])


def send(c, **kw):
    r = c.post("/webhook/tradingview", content=json.dumps(payload("futures_scalp_mnq", **kw)))
    return r


def client(make_client, analysis=None, **kw):
    return make_client(analyst=FakeAnalyst(make_analysis(**{**NQ_SETUP, **(analysis or {})})), **{**PROFILE, **kw})


class TestInstrumentFilter:
    @pytest.mark.parametrize("ticker", ["CME_MINI:NQ1!", "CME_MINI:MNQ1!", "MNQZ26", "NQH27"])
    def test_nasdaq_futures_accepted(self, make_client, ticker):
        assert send(client(make_client), ticker=ticker).status_code == 202

    @pytest.mark.parametrize("ticker,asset_class,why", [
        ("NASDAQ:AAPL", None, "is a stock"), ("BYBIT:BTCUSDT.P", None, "is a crypto"),
        ("NASDAQ:QQQ", None, "is a stock"), ("CME_MINI:ES1!", None, "ES is not allowed"),
        ("CME_MINI:MES1!", None, "MES is not allowed"), ("SPY", "option", "is a option")])
    def test_everything_else_rejected(self, make_client, ticker, asset_class, why):
        kw = {"ticker": ticker} | ({"asset_class": asset_class} if asset_class else {})
        r = send(client(make_client), **kw)
        assert r.status_code == 422 and why in r.json()["detail"]["message"]


class TestSizingAndTargets:
    def test_ten_point_stop_mixes_nq_and_mnq(self, make_client):
        c = client(make_client)
        an = wait_for(c, send(c, ticker="CME_MINI:NQ1!", price=21000.0).json()["alert_id"])["analyses"][-1]
        risk = an["risk"]
        assert an["decision"] == "LONG_SETUP" and risk["verdict"] == "APPROVED"
        assert risk["contracts"] == {"NQ": 1, "MNQ": 2} and risk["max_loss"] == 240.0   # $200 + $40 <= $250
        assert risk["reward_risk"] == [2.0, 3.0, 5.0] and risk["potential_profit"] == [480.0, 720.0, 1200.0]
        assert an["result"]["risk_reward"] == "1:2 (TP1)"

    def test_wide_stop_uses_micros_only(self, make_client):
        c = client(make_client, {"stop_loss": 20975.0, "take_profit": [21050.0, 21075.0, 21125.0]})
        risk = wait_for(c, send(c, price=21000.0).json()["alert_id"])["analyses"][-1]["risk"]
        assert risk["contracts"] == {"MNQ": 5} and risk["max_loss"] == 250.0

    def test_tp1_under_2r_is_avoided(self, make_client):
        c = client(make_client, {"take_profit": [21015.0, 21030.0, 21050.0]})   # TP1 = 1.5R
        an = wait_for(c, send(c, price=21000.0).json()["alert_id"])["analyses"][-1]
        assert an["claude_decision"] == "LONG_SETUP" and an["decision"] == "AVOID"
        assert "1:1.5 at TP1" in an["risk"]["reasons"][0]

    def test_target_beyond_5r_flagged(self, make_client):
        c = client(make_client, {"take_profit": [21020.0, 21030.0, 21080.0]})   # TP3 = 8R
        risk = wait_for(c, send(c, price=21000.0).json()["alert_id"])["analyses"][-1]["risk"]
        assert risk["verdict"] == "APPROVED" and any("beyond the 5R plan" in n for n in risk["notes"])

    def test_claude_is_told_the_rules(self, make_client):
        fake = FakeAnalyst(make_analysis(**NQ_SETUP))
        c = make_client(analyst=fake, **PROFILE)
        wait_for(c, send(c).json()["alert_id"])
        rules = " ".join(fake.calls[0]["rules"])
        for s in ("NQ, MNQ futures only", "TP1 must be at least 2R", "TP1 near 2R, TP2 near 3R, TP3 near 5R",
                  "beyond 5R", "25,000", "1,000 static drawdown", "(250)"):
            assert s in rules, s


def closed(pnl, n):
    return JournalEntry(id=n, symbol="NQ1!", side="long", entry=21000, stop_loss=20990, take_profit=[],
                        quantity=12, multiplier=2, status="closed", pnl=pnl, created_at=None, closed_at=None)


class TestDrawdown:
    def test_static_buffer(self):
        st = account.compute([closed(-250, 1), closed(-250, 2), closed(500, 3)], 25_000, 1_000, "static")
        assert (st.realized_pnl, st.remaining, st.equity, st.breached) == (0.0, 1000.0, 25000.0, False)
        st = account.compute([closed(-750, 1)], 25_000, 1_000, "static")
        assert st.remaining == 250.0

    def test_trailing_floor_follows_peak(self):
        st = account.compute([closed(600, 1), closed(-300, 2)], 25_000, 1_000, "trailing")
        assert (st.peak_equity, st.drawdown_used, st.remaining) == (25600.0, 300.0, 700.0)
        st_static = account.compute([closed(600, 1), closed(-300, 2)], 25_000, 1_000, "static")
        assert st_static.remaining == 1300.0          # static: profits add buffer

    def test_open_trades_reserve_their_risk(self):
        open_trade = closed(None, 9)
        open_trade.status = "open"                     # 10 pts x 12 MNQ-eq x $2 = $240 at risk
        assert account.compute([open_trade], 25_000, 1_000, "static").remaining == 760.0

    def test_breach(self):
        assert account.compute([closed(-1000, 1)], 25_000, 1_000, "static").breached

    def test_new_setup_blocked_when_a_full_loss_would_breach(self, make_client):
        c = client(make_client)
        for i in range(3):   # three full losses: $750 gone, $250 left
            j = c.post("/api/journal", headers=AUTH, json={"symbol": "MNQ1!", "side": "long", "entry": 21000,
                                                            "stop_loss": 20975, "quantity": 5}).json()
            c.patch(f"/api/journal/{j['id']}", headers=AUTH, json={"status": "closed", "exit_price": 20975})
        acct = c.get("/api/account", headers=AUTH).json()
        assert acct["realized_pnl"] == -750.0 and acct["remaining"] == 250.0
        an = wait_for(c, send(c, price=21000.0).json()["alert_id"])["analyses"][-1]
        assert an["decision"] == "AVOID" and "drawdown limit" in an["risk"]["reasons"][0]

    def test_manual_journal_uses_contract_multiplier(self, make_client):
        c = client(make_client)
        assert c.post("/api/journal", headers=AUTH, json={"symbol": "NQ1!", "side": "long", "entry": 21000,
                      "stop_loss": 20990, "quantity": 1}).json()["multiplier"] == 20
        assert c.post("/api/journal", headers=AUTH, json={"symbol": "CL1!", "side": "long", "entry": 70,
                      "stop_loss": 69, "quantity": 1}).status_code == 422

    def test_risk_calculator_respects_drawdown(self, make_client):
        c = client(make_client)
        r = c.post("/api/risk/calculate", headers=AUTH, json={"side": "long", "entry": 21000, "stop_loss": 20990,
                   "take_profit": [21020], "asset_class": "future", "symbol": "NQ1!"}).json()
        assert r["contracts"] == {"NQ": 1, "MNQ": 2} and r["drawdown_remaining"] == 1000.0

    def test_status_reports_account(self, make_client):
        s = client(make_client).get("/api/status", headers=AUTH).json()
        assert s["account"]["remaining"] == 1000.0 and s["risk"]["risk_per_trade"] == 250.0
        assert s["instruments"]["futures_roots"] == ["NQ", "MNQ"]


class TestConfigGuards:
    def test_risk_that_would_blow_the_account_in_one_loss_refused(self, tmp_path):
        with pytest.raises(ValueError, match="drawdown_limit"):
            make_settings(tmp_path, **{**PROFILE, "risk_percent": 4.0, "allowed_risk_percents": [4.0]})

    def test_profile_is_valid(self, tmp_path):
        s = make_settings(tmp_path, **PROFILE)
        assert account_rules(s)

    def test_shipped_spec_file(self):
        data = json.loads(Path(SPECS).read_text())["futures"]
        assert data["NQ"]["multiplier"] == 20 and data["MNQ"]["multiplier"] == 2 and data["NQ"]["micro"] == "MNQ"


class TestPineTemplate:
    def test_template_output_shape_is_accepted(self, make_client):
        """tests/payloads/pine_template_nq.json mirrors exactly what tradingview/nq_claude_alert.pine emits."""
        c = client(make_client)
        r = c.post("/webhook/tradingview", content=json.dumps(payload("pine_template_nq")))
        assert r.status_code == 202, r.text
        assert (r.json()["asset_class"], r.json()["timeframe"], r.json()["strategy"]) == ("future", "5m", "scalping")

    def test_template_with_no_swings_yet(self, make_client):
        p = payload("pine_template_nq")
        p["structure"].update(support=[], resistance=[], swing_high=None, swing_low=None, fvg=[], liquidity_sweep=None)
        assert client(make_client).post("/webhook/tradingview", content=json.dumps(p)).status_code == 202

    def test_template_fields_match_schema(self):
        pine = (Path(__file__).parents[2] / "tradingview" / "nq_claude_alert.pine").read_text()
        for key in ('"secret"', '"ticker"', '"timeframe"', '"price"', '"indicators"', '"structure"', '"liquidity_sweep"',
                    '"swing_high"', '"volume_avg"', '"macd_hist"', '"asset_class":"future"'):
            assert key in pine, key


class TestPineScripts:
    """Every JSON key a Pine script writes must be one the webhook accepts (it rejects unknown fields)."""
    ROOT = Path(__file__).parents[2] / "tradingview"

    def allowed(self):
        from app.schemas import Structure, TradingViewAlert
        indicators = {"rsi", "ema9", "ema21", "ema50", "vwap", "macd", "macd_signal", "macd_hist", "volume",
                      "volume_avg", "atr"}
        fvg_bounds = {"low", "high"}
        return set(TradingViewAlert.model_fields) | set(Structure.model_fields) | indicators | fvg_bounds

    @pytest.mark.parametrize("script", ["nq_claude_alert.pine", "nq_bullish_strategy.pine"])
    def test_script_keys_are_accepted_by_the_schema(self, script):
        import re
        keys = set(re.findall(r'"([a-z_0-9]+)":', (self.ROOT / script).read_text()))
        assert {"secret", "ticker", "timeframe", "price", "indicators", "structure"} <= keys
        assert keys <= self.allowed(), keys - self.allowed()

    def test_bullish_strategy_alert_accepted_and_analysed(self, make_client):
        fake = FakeAnalyst(make_analysis(**NQ_SETUP))
        c = make_client(analyst=fake, **PROFILE)
        r = c.post("/webhook/tradingview", content=json.dumps(payload("pine_bullish_strategy")))
        assert r.status_code == 202, r.text
        wait_for(c, r.json()["alert_id"])
        assert "long 12" in fake.calls[0]["context"]["notes"]
