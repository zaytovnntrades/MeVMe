"""Shield: MFF-style end-of-day trailing drawdown with lock, CME trading days, daily stop, losing streak,
entry window, kill switch, and Telegram + Discord delivery."""
import json
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx
import pytest

from app import account, shield
from app.db import JournalEntry
from conftest import AUTH, TOKEN, FakeAnalyst, make_analysis, make_settings, payload, wait_for
from test_nasdaq_profile import NQ_SETUP, PROFILE

NY = ZoneInfo("America/New_York")


def et(y, mo, d, h, mi=0) -> datetime:
    return datetime(y, mo, d, h, mi, tzinfo=NY).astimezone(timezone.utc)


TUE_10 = et(2026, 11, 24, 10)           # inside the 09:35-11:30 window
SHIELD = dict(PROFILE, drawdown_mode="eod_trailing", drawdown_lock_above_start=100, daily_loss_limit=500,
              max_consecutive_losses=2, entry_window="09:35-11:30")


def trade(pnl, when, r=None, n=[0]):
    n[0] += 1
    return JournalEntry(id=n[0], symbol="MNQ1!", side="long", entry=21000, stop_loss=20975, take_profit=[],
                        quantity=5, multiplier=2, status="closed", pnl=pnl,
                        r_multiple=r if r is not None else pnl / 250, closed_at=when, created_at=when)


class TestTradingDay:
    def test_cme_day_rolls_at_17_new_york(self):
        assert account.trading_day(et(2026, 11, 24, 16, 59)).isoformat() == "2026-11-24"
        assert account.trading_day(et(2026, 11, 24, 17, 0)).isoformat() == "2026-11-25"
        assert account.trading_day(et(2026, 11, 24, 23, 30)).isoformat() == "2026-11-25"

    def test_naive_timestamps_are_utc(self):
        assert account.trading_day(datetime(2026, 11, 24, 15, 0)).isoformat() == "2026-11-24"   # 10:00 ET


class TestEodTrailing:
    def calc(self, trades, now=TUE_10, lock=100):
        return account.compute(trades, 25_000, 1_000, "eod_trailing", lock, now)

    def test_fresh_account(self):
        st = self.calc([])
        assert (st.floor_balance, st.remaining, st.breached) == (24_000, 1_000, False)

    def test_floor_moves_only_on_completed_days(self):
        mon = et(2026, 11, 23, 10)
        st = self.calc([trade(600, mon)])
        assert st.peak_eod_balance == 25_600 and st.floor_balance == 24_600 and st.remaining == 1_000

    def test_todays_swings_do_not_move_the_floor(self):
        st = self.calc([trade(700, TUE_10 - timedelta(minutes=20)), trade(-700, TUE_10 - timedelta(minutes=5))])
        assert st.floor_balance == 24_000 and st.remaining == 1_000      # intraday peak of +700 ignored

    def test_lock_stops_the_floor_at_start_plus_100(self):
        st = self.calc([trade(1_500, et(2026, 11, 20, 10))])
        assert st.floor_balance == 25_100 and st.floor_locked and st.remaining == 1_400

    def test_without_lock_the_floor_keeps_trailing(self):
        st = self.calc([trade(1_500, et(2026, 11, 20, 10))], lock=None)
        assert st.floor_balance == 25_500 and not st.floor_locked

    def test_breach(self):
        st = self.calc([trade(600, et(2026, 11, 23, 10)), trade(-1_000, TUE_10 - timedelta(minutes=1))])
        assert st.breached and st.remaining <= 0

    def test_daily_stats(self):
        mon, t = et(2026, 11, 23, 10), TUE_10 - timedelta(minutes=30)
        st = self.calc([trade(-250, mon), trade(250, t), trade(-250, t + timedelta(minutes=5)),
                        trade(-250, t + timedelta(minutes=10))])
        assert (st.today_pnl, st.today_closed, st.losing_streak_today) == (-250, 3, 2)

    def test_breakeven_close_breaks_the_streak(self):
        t = TUE_10 - timedelta(minutes=30)
        st = self.calc([trade(-250, t), trade(-2, t + timedelta(minutes=5), r=-0.01)])
        assert st.losing_streak_today == 0


class TestPrecheck:
    acct0 = account.compute([], 25_000, 1_000, "eod_trailing", 100, TUE_10)

    def kinds(self, **kw):
        base = dict(now=TUE_10, halted=False, halt_reason=None, acct=self.acct0, risk_per_trade=250,
                    entry_window="09:35-11:30", daily_loss_limit=500, max_consecutive_losses=2)
        return [b.kind for b in shield.precheck(**{**base, **kw})]

    def test_clear(self):
        assert self.kinds() == []

    @pytest.mark.parametrize("when,ok", [(et(2026, 11, 24, 9, 34), False), (et(2026, 11, 24, 9, 35), True),
                                         (et(2026, 11, 24, 11, 29), True), (et(2026, 11, 24, 11, 30), False),
                                         (et(2026, 11, 28, 10, 0), False)])     # Saturday
    def test_entry_window(self, when, ok):
        assert ("session" not in self.kinds(now=when)) == ok

    def test_daily_stop(self):
        one_loss = account.compute([trade(-250, TUE_10 - timedelta(minutes=9))], 25_000, 1_000, "eod_trailing", 100, TUE_10)
        assert "daily_loss" not in self.kinds(acct=one_loss)          # one more loss lands exactly on -500: allowed
        two = account.compute([trade(-250, TUE_10 - timedelta(minutes=9), r=-1), trade(-250, TUE_10 - timedelta(minutes=5), r=-1)],
                              25_000, 1_000, "eod_trailing", 100, TUE_10)
        k = self.kinds(acct=two)
        assert "daily_loss" in k and "streak" in k

    def test_halt_and_drawdown(self):
        assert "halt" in self.kinds(halted=True, halt_reason="news")
        low = account.compute([trade(-800, et(2026, 11, 23, 10))], 25_000, 1_000, "eod_trailing", 100, TUE_10)
        assert "drawdown" in self.kinds(acct=low)                     # $200 left < $250 risk

    def test_bad_window_rejected(self):
        for bad in ("9:35", "11:30-09:35", "abc-def"):
            with pytest.raises(ValueError):
                shield.parse_window(bad)


def captured():
    sent = {"telegram": [], "discord": []}

    def handler(req):
        body = json.loads(req.content)
        if "telegram" in str(req.url):
            sent["telegram"].append(body["text"])
            return httpx.Response(200, json={"ok": True})
        sent["discord"].append(body["content"])
        return httpx.Response(204)
    return sent, httpx.MockTransport(handler)


def channels(transport):
    from app.infra import Discord, Telegram
    return dict(telegram=Telegram("123:ABC", "42", transport=transport),
                discord=Discord("https://discord.com/api/webhooks/1/abc", transport=transport))


def post(c, **kw):
    r = c.post("/webhook/tradingview", content=json.dumps(payload("futures_scalp_mnq", **kw)))
    assert r.status_code == 202, r.text
    return wait_for(c, r.json()["alert_id"])


class TestPipeline:
    def client(self, make_client, when=TUE_10, **kw):
        fake = FakeAnalyst(make_analysis(**NQ_SETUP))
        return make_client(analyst=fake, clock=lambda: when, **{**SHIELD, **kw}), fake

    def test_inside_window_is_analysed(self, make_client):
        c, fake = self.client(make_client)
        a = post(c, price=21000.0)
        assert a["status"] == "analyzed" and len(fake.calls) == 1

    def test_outside_window_blocked_without_calling_claude(self, make_client):
        sent, tr = captured()
        c, fake = self.client(make_client, when=et(2026, 11, 24, 0, 30), **channels(tr))
        a = post(c, price=21000.0)
        an = a["analyses"][-1]
        assert a["status"] == "blocked" and an["decision"] == "AVOID" and fake.calls == []
        assert "outside the entry window" in an["risk"]["reasons"][0]
        assert len(sent["telegram"]) == 1 and len(sent["discord"]) == 1
        assert sent["discord"][0].startswith("[SHIELD] Blocked MNQ1!")

    def test_same_block_announced_once_per_day(self, make_client):
        sent, tr = captured()
        c, _ = self.client(make_client, when=et(2026, 11, 24, 0, 30), **channels(tr))
        post(c, price=21000.0)
        post(c, price=21001.0)
        assert len(sent["telegram"]) == 1 and len(sent["discord"]) == 1

    def test_daily_stop_blocks_after_two_losses(self, make_client):
        c, fake = self.client(make_client)
        with c.app.state.svc.db.Session() as ses:
            ses.add_all([trade(-250, TUE_10 - timedelta(minutes=20), r=-1), trade(-250, TUE_10 - timedelta(minutes=10), r=-1)])
            ses.commit()
        a = post(c, price=21000.0)
        reasons = " ".join(a["analyses"][-1]["risk"]["reasons"])
        assert a["status"] == "blocked" and "daily stop" in reasons and "2 straight losses" in reasons
        assert fake.calls == []

    def test_kill_switch(self, make_client):
        sent, tr = captured()
        c, fake = self.client(make_client, **channels(tr))
        st = c.post("/api/shield/halt", headers=AUTH, json={"reason": "FOMC today"}).json()
        assert st["halted"] and st["halt_reason"] == "FOMC today" and not st["new_setups_allowed_now"]
        assert any("Kill switch ON: FOMC today" in m for m in sent["discord"])
        a = post(c, price=21000.0)
        assert a["status"] == "blocked" and "kill switch is ON" in a["analyses"][-1]["risk"]["reasons"][0]
        assert fake.calls == []
        st = c.post("/api/shield/resume", headers=AUTH).json()
        assert not st["halted"] and any("Kill switch OFF" in m for m in sent["telegram"])
        assert post(c, price=21000.5)["status"] == "analyzed"

    def test_kill_switch_survives_restart(self, make_client, tmp_path):
        c, _ = self.client(make_client)
        c.post("/api/shield/halt", headers=AUTH, json={})
        c2, _ = self.client(make_client)            # same database file, new app
        assert c2.get("/api/shield", headers=AUTH).json()["halted"]

    def test_kill_switch_needs_dashboard_token(self, make_client):
        c, _ = self.client(make_client)
        assert c.post("/api/shield/halt", json={}).status_code == 401

    def test_setup_alert_goes_to_both_channels(self, make_client):
        sent, tr = captured()
        c, _ = self.client(make_client, **channels(tr))
        post(c, price=21000.0)
        assert len(sent["telegram"]) == 1 and sent["telegram"] == sent["discord"]
        assert "[PAPER] NQ1!" in sent["discord"][0] or "[PAPER] MNQ1!" in sent["discord"][0]

    def test_one_channel_failing_does_not_stop_the_other(self, make_client):
        from app.infra import Discord, Telegram
        got = []

        def handler(req):
            if "telegram" in str(req.url):
                return httpx.Response(500)
            got.append(json.loads(req.content)["content"])
            return httpx.Response(204)
        tr = httpx.MockTransport(handler)
        c, _ = self.client(make_client, telegram=Telegram("1:A", "2", transport=tr),
                           discord=Discord("https://discord.com/api/webhooks/1/x", transport=tr))
        assert post(c, price=21000.0)["status"] == "analyzed" and len(got) == 1

    def test_status_reports_shield_and_channels(self, make_client):
        sent, tr = captured()
        c, _ = self.client(make_client, **channels(tr))
        s = c.get("/api/status", headers=AUTH).json()
        assert s["telegram"] == "configured" and s["discord"] == "configured"
        assert s["shield"]["entry_window"] == "09:35-11:30" and s["shield"]["new_setups_allowed_now"]
        assert s["account"]["floor_balance"] == 24_000 and s["account"]["mode"] == "eod_trailing"


class TestConfig:
    def test_funded_eod_warning(self, make_client):
        c = make_client(clock=lambda: TUE_10, **{**SHIELD, "account_phase": "funded"})
        assert "INTRADAY" in c.get("/api/shield", headers=AUTH).json()["warnings"][0]

    @pytest.mark.parametrize("kw", [{"drawdown_mode": "weekly"}, {"account_phase": "live"},
                                    {"entry_window": "11:30-09:35"},
                                    {"discord_webhook_url": "https://example.com/hook"}])
    def test_bad_settings_refused(self, tmp_path, kw):
        with pytest.raises(ValueError):
            make_settings(tmp_path, **kw)
