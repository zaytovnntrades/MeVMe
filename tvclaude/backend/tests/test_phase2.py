"""Phase 2: Lab (Strategy Tester CSV), Journal's rules-followed + weekly review, and the coordinator's
daily report scheduling and delivery."""
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from app import lab, reports
from app.db import Database, JournalEntry
from conftest import AUTH, FakeWriter, payload, wait_for
from test_shield import SHIELD, TUE_10, captured, channels, et, trade

CSV = (Path(__file__).parent / "payloads" / "tv_list_of_trades.csv").read_text()
NY = ZoneInfo("America/New_York")


class TestLabParse:
    def test_classic_export(self):
        t = lab.parse(CSV, fee_per_contract=1.24)
        assert [x.number for x in t] == ["1", "2", "3", "4", "5"]
        assert [x.gross for x in t] == [480.0, -240.0, -250.0, -250.0, 1200.0]       # unicode minus handled
        assert t[0].contracts == 12 and t[0].fees == 14.88
        assert t[0].closed_at.isoformat() == "2026-11-16T10:05:00-05:00"             # exit row time

    def test_newer_header_names(self):
        csv = ("Trade number,Type,Date and time,Price USD,Position size (qty),Net P&L USD,Net P&L %\n"
               "1,Entry long,2026-11-16 09:45,21000,2,,\n1,Exit long,2026-11-16 10:00,21020,2,80,0.1\n")
        t = lab.parse(csv)
        assert len(t) == 1 and t[0].gross == 80 and t[0].contracts == 2

    def test_single_row_per_trade(self):
        t = lab.parse("Trade #,Date/Time,Profit USD\n1,2026-11-16 10:00,100\n2,2026-11-16 11:00,-50\n")
        assert [x.gross for x in t] == [100, -50]

    @pytest.mark.parametrize("text,msg", [("", "empty"), ("Trade #,Date/Time,Price\n1,2026-11-16 10:00,1\n", "profit"),
                                          ("Date/Time,Profit USD\nyesterday,5\n", "can't read the date")])
    def test_errors_are_plain(self, text, msg):
        with pytest.raises(lab.LabError, match=msg):
            lab.parse(text)


class TestLabAnalyse:
    def run(self, trades, **kw):
        base = dict(risk_usd=250, daily_loss_limit=500, drawdown_limit=1000, drawdown_mode="eod_trailing", lock=100)
        return lab.analyse(trades, **{**base, **kw})

    def test_numbers_match_the_file(self):
        r = self.run(lab.parse(CSV, 1.24))
        assert (r.trades, r.wins, r.losses, r.win_rate) == (5, 2, 3, 40.0)
        assert (r.total_net, r.total_fees, r.max_consecutive_losses) == (895.36, 44.64, 3)
        assert r.max_drawdown_usd == 762.32 and r.expectancy_r == 0.716
        assert r.worst_day == {"day": "2026-11-17", "net": -512.4} and r.days_past_daily_limit == 1
        assert r.verdict == "NOT YET" and [c["pass"] for c in r.checks] == [False, True, True]

    def test_no_fee_warning(self):
        assert any("no fee entered" in n for n in self.run(lab.parse(CSV)).notes)

    def test_eod_breach_detected(self):
        t = [lab.LabTrade("1", et(2026, 11, 16, 10), 1, 600, 0), lab.LabTrade("2", et(2026, 11, 17, 10), 1, -1100, 0)]
        dd = self.run(t).drawdown_rule
        assert dd["breached"] and dd["breach_date"] == "2026-11-17" and dd["floor_at_breach"] == -400

    def test_intraday_swing_does_not_breach_eod_rule(self):
        t = [lab.LabTrade("1", et(2026, 11, 16, 10), 1, 600, 0), lab.LabTrade("2", et(2026, 11, 16, 11), 1, -1100, 0)]
        assert not self.run(t).drawdown_rule["breached"]                    # same day: EOD floor still -1000, at -500
        assert self.run(t, drawdown_mode="trailing").drawdown_rule["breached"]   # intraday trailing would breach

    def test_pass_verdict(self):
        t = [lab.LabTrade(str(i), et(2026, 11, 2, 10) + timedelta(days=i // 3, minutes=i), 1,
                          500 if i % 2 else -250, 0) for i in range(120)]
        r = self.run(t)
        assert r.verdict == "PASS" and r.expectancy_r == 0.5


class TestLabApi:
    def test_upload_and_list(self, make_client):
        c = make_client(**SHIELD)
        r = c.post("/api/lab/analyze?fee_per_contract=1.24&filename=nq.csv", headers=AUTH, content=CSV)
        assert r.status_code == 200 and r.json()["total_net"] == 895.36 and r.json()["id"] == 1
        runs = c.get("/api/lab/runs", headers=AUTH).json()
        assert runs[0]["filename"] == "nq.csv" and runs[0]["params"]["fee_per_contract"] == 1.24

    def test_bad_file_and_auth(self, make_client):
        c = make_client(**SHIELD)
        r = c.post("/api/lab/analyze", headers=AUTH, content="a,b\n1,2\n")
        assert r.status_code == 422 and "Headers in your file" in r.json()["detail"]
        assert c.post("/api/lab/analyze", headers=AUTH, content=CSV, params={"chart_tz": "Mars/Base"}).status_code == 422
        assert c.post("/api/lab/analyze", content=CSV).status_code == 401


def facts(**over):
    f = {"day": "2026-11-24", "alerts": 0, "decisions": {}, "blocked_by_shield": {}, "setups": [],
         "alert_price_range": None, "paper_trades_closed": 0, "paper_pnl_today": 0, "paper_r_today": [],
         "rule_breaks_today": 0, "trades_not_rated": 0, "open_or_planned": [],
         "account": {"equity": 25000, "floor": 24000, "floor_locked": False, "drawdown_left": 1000,
                     "mode": "eod_trailing", "losing_streak_today": 0},
         "risk_per_trade": 250, "full_losses_left": 4, "kill_switch": {"on": False, "reason": None},
         "shield": {"max_consecutive_losses": 2}}
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(f.get(k), dict):
            f[k] = {**f[k], **v}
        else:
            f[k] = v
    return f


class TestOneDecision:
    def test_priority_order(self):
        assert "Kill switch is ON (FOMC)" in reports.one_decision(facts(kill_switch={"on": True, "reason": "FOMC"},
                                                                       account={"drawdown_left": 100}))
        assert "Only 400.00 of drawdown left" in reports.one_decision(facts(account={"drawdown_left": 400}, full_losses_left=1))
        assert "#7 MNQ1! (open)" in reports.one_decision(
            facts(open_or_planned=[{"id": 7, "symbol": "MNQ1!", "side": "long", "status": "open"}]))
        assert "Daily stop hit" in reports.one_decision(facts(account={"losing_streak_today": 2}))
        assert "rules followed" in reports.one_decision(facts(trades_not_rated=2))
        assert reports.one_decision(facts()) == "No decision needed today."

    def test_report_text(self):
        t = reports.format_daily(facts(alerts=3, decisions={"AVOID": 1, "LONG_SETUP": 1},
                                       blocked_by_shield={"outside the entry window 09": 1},
                                       alert_price_range=[21000.25, 21050.0]), "Quiet day.")
        assert t.startswith("[DAILY REPORT] 2026-11-24") and "Quiet day." in t and "from your alerts only" in t
        assert "DECISION: No decision needed today." in t and t.endswith("Nothing was sent to a broker.")

    def test_setup_line_is_readable(self):
        t = reports.format_daily(facts(setups=[{"symbol": "NQ1!", "decision": "LONG_SETUP", "entry": [20998.0, 21000.0],
                                                "stop": 20990.0, "targets": [21020.0, 21030.0, 21050.0],
                                                "size": {"NQ": 1, "MNQ": 2}, "risk_verdict": "APPROVED"}]), None)
        assert "entry 20,998.00 - 21,000.00 stop 20,990.00 targets 21,020.00 / 21,030.00 / 21,050.00 " \
               "size 1 NQ + 2 MNQ (APPROVED)" in t


def client(make_client, when, writer=None, **kw):
    return make_client(clock=lambda: when, writer=writer or FakeWriter(),
                       **{**SHIELD, "daily_report_time": "16:05", **kw})


class TestCoordinator:
    def test_preview_and_send_once(self, make_client):
        sent, tr = captured()
        c = client(make_client, et(2026, 11, 24, 16, 10), **channels(tr))
        r = c.post("/webhook/tradingview", content=json.dumps(payload("futures_scalp_mnq", price=21000.0)))
        assert r.status_code == 202
        p = c.get("/api/coordinator/daily", headers=AUTH).json()
        assert p["summary"] is None and p["facts"]["alerts"] == 1 and "DECISION:" in p["text"]
        r1 = c.post("/api/coordinator/daily/send", headers=AUTH).json()
        assert r1["sent"] and r1["channels"] == {"telegram": True, "discord": True}
        assert "Quiet session" in sent["discord"][-1] and sent["discord"][-1] == sent["telegram"][-1]
        r2 = c.post("/api/coordinator/daily/send", headers=AUTH).json()
        assert not r2["sent"] and "already sent" in r2["reason"]

    @pytest.mark.parametrize("when,expected", [
        (et(2026, 11, 24, 16, 4), []), (et(2026, 11, 24, 16, 6), ["daily"]),
        (et(2026, 11, 27, 16, 30), ["daily", "weekly"]),      # Friday
        (et(2026, 11, 28, 16, 30), [])])                      # Saturday
    def test_schedule(self, make_client, when, expected):
        import asyncio
        from app import coordinator
        c = client(make_client, when)
        s = c.app.state.svc
        assert asyncio.run(coordinator.tick(s)) == expected
        assert asyncio.run(coordinator.tick(s)) == []          # never twice

    def test_writer_failure_still_sends_facts(self, make_client):
        import asyncio
        from app import coordinator
        sent, tr = captured()
        c = client(make_client, et(2026, 11, 24, 16, 30), writer=FakeWriter(result=None), **channels(tr))
        assert asyncio.run(coordinator.tick(c.app.state.svc)) == ["daily"]
        assert sent["telegram"][0].startswith("[DAILY REPORT] 2026-11-24") and "DECISION:" in sent["telegram"][0]

    def test_no_schedule_without_time(self, make_client):
        import asyncio
        from app import coordinator
        c = make_client(clock=lambda: et(2026, 11, 24, 16, 30), **SHIELD)
        assert asyncio.run(coordinator.tick(c.app.state.svc)) == []

    def test_background_loop_sends_missed_report_on_startup(self, make_client):
        import time
        sent, tr = captured()
        c = client(make_client, et(2026, 11, 24, 18, 0), start_coordinator=True, **channels(tr))
        for _ in range(50):
            if sent["telegram"]:
                break
            time.sleep(0.05)
        assert len(sent["telegram"]) == 1 and sent["telegram"][0].startswith("[DAILY REPORT]")

    def test_unknown_kind(self, make_client):
        assert client(make_client, TUE_10).get("/api/coordinator/monthly", headers=AUTH).status_code == 404


class TestJournalAndWeekly:
    def test_rules_followed_and_weekly(self, make_client):
        w = FakeWriter()
        c = client(make_client, et(2026, 11, 27, 16, 30), writer=w)
        ids = []
        for exit_px, followed in ((21020, True), (20990, False), (21030, None)):
            j = c.post("/api/journal", headers=AUTH, json={"symbol": "MNQ1!", "side": "long", "entry": 21000,
                                                            "stop_loss": 20990, "quantity": 12}).json()
            body = {"status": "closed", "exit_price": exit_px}
            if followed is not None:
                body["rules_followed"] = followed
            ids.append(c.patch(f"/api/journal/{j['id']}", headers=AUTH, json=body).json())
        assert ids[0]["rules_followed"] is True and ids[1]["rules_followed"] is False
        with c.app.state.svc.db.Session() as ses:          # date the trades inside the review week
            for j in ses.query(JournalEntry):
                j.closed_at = et(2026, 11, 26, 10)
            ses.commit()
        r = c.get("/api/coordinator/weekly?summary=true", headers=AUTH).json()
        f = r["facts"]
        assert (f["paper_trades"], f["rules_followed"], f["rule_breaks"], f["not_rated"]) == (3, 1, 1, 1)
        assert f["avg_r"] == 1.333 and f["meets_target"] and f["win_rate_pct"] == 66.7     # (2 - 1 + 3) / 3
        assert "ONE CHANGE: Mark rules followed" in r["text"] and w.calls[-1][0] == "weekly"


def test_old_database_gets_new_column(tmp_path):
    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE journal (id INTEGER PRIMARY KEY, created_at DATETIME, analysis_id INTEGER, symbol VARCHAR(40),"
                " strategy VARCHAR(10), side VARCHAR(5), entry FLOAT, stop_loss FLOAT, take_profit JSON, quantity FLOAT,"
                " multiplier FLOAT, status VARCHAR(8), exit_price FLOAT, pnl FLOAT, r_multiple FLOAT, closed_at DATETIME,"
                " notes TEXT)")
    con.commit()
    con.close()
    db = Database(f"sqlite:///{path}")
    db.create_all()
    cols = [r[1] for r in sqlite3.connect(path).execute("PRAGMA table_info(journal)")]
    assert "rules_followed" in cols
    db.create_all()                                                      # second run is a no-op


def test_notify_test_reports_each_channel(monkeypatch, capsys, tmp_path):
    import asyncio
    from app import notify_test
    from conftest import make_settings

    sent = []

    async def fake_send(self, text):
        sent.append(type(self).__name__)
        return True

    monkeypatch.setattr(notify_test, "get_settings", lambda: make_settings(
        tmp_path, discord_webhook_url="https://discord.com/api/webhooks/1/abc",
        telegram_bot_token=None, telegram_chat_id=None))
    monkeypatch.setattr(notify_test.Discord, "send", fake_send)
    assert asyncio.run(notify_test.main()) == 0
    out = capsys.readouterr().out
    assert "discord: ok" in out and "telegram: not set" in out and "abc" not in out
    assert sent == ["Discord"]
