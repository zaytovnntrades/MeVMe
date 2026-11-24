"""Tests for futures.py (TradersPost / CME index futures) and its wiring into the gate.

    python3 -m unittest test_futures -v
"""
import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

import futures
import grok_gate
import risk

HERE = Path(__file__).resolve().parent
ET = futures.ET
LIMITS = risk.load_limits(str(HERE / "risk.example.json"))


def at(y, mo, d, h, mi=0) -> float:
    return datetime(y, mo, d, h, mi, tzinfo=ET).timestamp()


TUE_10AM = at(2026, 11, 24, 10)  # Tuesday, regular session
ENTRY = {"ticker": "CME_MINI:MNQ1!", "action": "buy", "sentiment": "long", "quantity": 1,
         "price": 21000, "stopLoss": {"type": "stop", "stopPrice": 20990},
         "takeProfit": {"limitPrice": 21030}}
EXIT = {"ticker": "MNQ1!", "action": "sell", "sentiment": "flat"}


class Parsing(unittest.TestCase):
    def test_root_symbol(self):
        for t, root in (("CME_MINI:MNQ1!", "MNQ"), ("MNQ1!", "MNQ"), ("mnq", "MNQ"), ("MNQZ2026", "MNQ"),
                        ("MNQZ26", "MNQ"), ("NQH27", "NQ"), ("M2KZ26", "M2K"), ("M2K1!", "M2K"),
                        ("ES1!", "ES"), ("MYMU26", "MYM"), ("AAPL", "AAPL")):
            self.assertEqual(futures.root_symbol(t), root, t)

    def test_crypto_symbols_unchanged(self):
        self.assertEqual(risk.norm_symbol("BYBIT:BTCUSDT.P"), "BTCUSDT")

    def test_detects_traderspost(self):
        self.assertTrue(futures.is_traderspost(ENTRY))
        self.assertFalse(futures.is_traderspost({"exchange": "bybit", "coin_pair": "BTCUSDT.P", "position": "0"}))


class Classify(unittest.TestCase):
    def c(self, **kw):
        return futures.classify({"ticker": "MNQ1!", **kw})

    def test_entries(self):
        self.assertEqual(self.c(action="buy", sentiment="long"), ("entry", "long"))
        self.assertEqual(self.c(action="sell", sentiment="short"), ("entry", "short"))
        self.assertEqual(self.c(action="buy"), ("entry", "long"))
        self.assertEqual(self.c(action="add", sentiment="short"), ("entry", "short"))

    def test_exits_are_never_entries(self):
        for kw in ({"action": "exit"}, {"action": "cancel"}, {"action": "sell", "sentiment": "flat"},
                   {"action": "buy", "sentiment": "flat"},
                   {"action": "sell", "sentiment": "long"},   # trimming a long
                   {"action": "buy", "sentiment": "short"},   # covering part of a short
                   {"action": "add"}, {"action": "something_new"}):
            self.assertEqual(self.c(**kw), ("exit", None), kw)

    def test_gate_uses_futures_classifier(self):
        self.assertEqual(grok_gate.classify(EXIT), ("exit", None))
        self.assertEqual(grok_gate.classify(ENTRY), ("entry", "long"))


class Measure(unittest.TestCase):
    def test_stop_price(self):
        m = risk.measure({**ENTRY, "quantity": 2}, {})
        self.assertEqual((m["market"], m["symbol"], m["point_value"], m["contracts"]), ("futures", "MNQ", 2, 2))
        self.assertAlmostEqual(m["stop_points"], 10)
        self.assertAlmostEqual(m["risk_usdt"], 40)  # 2 x 10 pts x $2
        self.assertAlmostEqual(m["notional_usdt"], 84000)

    def test_nq_is_ten_times_mnq(self):
        self.assertAlmostEqual(risk.measure({**ENTRY, "ticker": "NQ1!"}, {})["risk_usdt"], 200)

    def test_stop_percent(self):
        m = risk.measure({**ENTRY, "stopLoss": {"type": "stop", "percent": 0.1}}, {})
        self.assertAlmostEqual(m["stop_points"], 21)

    def test_stop_amount_is_not_guessed(self):
        m = risk.measure({**ENTRY, "stopLoss": {"type": "stop", "amount": 10}}, {})
        self.assertTrue(m["has_stop_loss"])
        self.assertIsNone(m["stop_points"])
        self.assertIsNone(m["risk_usdt"])

    def test_non_contract_quantity_type_not_counted(self):
        self.assertIsNone(risk.measure({**ENTRY, "quantityType": "dollar_amount"}, {})["contracts"])

    def test_custom_point_value(self):
        m = risk.measure({**ENTRY, "ticker": "CL1!", "price": 70, "stopLoss": {"stopPrice": 69.5}},
                         {"futures_point_values": {"CL": 1000}})
        self.assertAlmostEqual(m["risk_usdt"], 500)


class Session(unittest.TestCase):
    def block(self, ts, before=0, root="MNQ"):
        return futures.session_block(root, datetime.fromtimestamp(ts, ET), before)

    def test_open_hours(self):
        for ts in (TUE_10AM, at(2026, 11, 29, 18, 0), at(2026, 11, 24, 2, 0), at(2026, 11, 27, 16, 59)):
            self.assertIsNone(self.block(ts), datetime.fromtimestamp(ts, ET))

    def test_closed(self):
        self.assertIn("daily break", self.block(at(2026, 11, 24, 17, 30)))
        self.assertIn("weekend", self.block(at(2026, 11, 27, 17, 0)))   # Friday close
        self.assertIn("weekend", self.block(at(2026, 11, 28, 12, 0)))   # Saturday
        self.assertIn("weekend", self.block(at(2026, 11, 29, 17, 59)))  # Sunday before open

    def test_minutes_before_close(self):
        self.assertIn("too close", self.block(at(2026, 11, 24, 16, 50), before=15))
        self.assertIsNone(self.block(at(2026, 11, 24, 16, 40), before=15))

    def test_unknown_products_get_no_session_rule(self):
        self.assertIsNone(self.block(at(2026, 11, 28, 12, 0), root="CL"))


class Rules(unittest.TestCase):
    def reasons(self, payload, limits=LIMITS, now=TUE_10AM):
        return risk.check_static(risk.measure(payload, limits), limits, now)

    def test_example_trade_passes(self):
        self.assertEqual(self.reasons(ENTRY), [])

    def test_nq_not_allowed_in_example(self):
        self.assertIn("not allowed", self.reasons({**ENTRY, "ticker": "NQ1!"})[0])

    def test_all_failures_listed(self):
        text = " | ".join(self.reasons({**ENTRY, "quantity": 5, "stopLoss": {"stopPrice": 20970}},
                                       now=at(2026, 11, 24, 17, 15)))
        for word in ("daily break", "5 contracts > max_contracts 2", "stop 30 pts > max_stop_points 15",
                     "trade risk $300.00"):
            self.assertIn(word, text)

    def test_no_stop_blocked(self):
        text = " | ".join(self.reasons({k: v for k, v in ENTRY.items() if k != "stopLoss"}))
        self.assertIn("no stop-loss", text)
        self.assertIn("unbounded", text)

    def test_amount_stop_blocked_when_risk_limits_on(self):
        text = " | ".join(self.reasons({**ENTRY, "stopLoss": {"amount": 10}}))
        self.assertIn("can't measure the stop", text)
        self.assertIn("risk can't be measured", text)

    def test_broker_sized_order_blocked_when_contract_cap_on(self):
        self.assertIn("can't count contracts", self.reasons({k: v for k, v in ENTRY.items() if k != "quantity"})[0])

    def test_per_contract_and_flat_caps(self):
        lim = risk.load_limits(None) or {"max_contracts": 3}
        self.assertEqual(self.reasons({**ENTRY, "quantity": 3}, lim), [])
        self.assertTrue(self.reasons({**ENTRY, "quantity": 4}, lim))

    def test_crypto_only_rules_dont_touch_futures(self):
        # max_leverage / max_notional_usdt are crypto rules; an MNQ contract's $42k notional must not trip them
        self.assertEqual(self.reasons(ENTRY, {"max_leverage": 5, "max_notional_usdt": 1000}), [])


class RewardRisk(unittest.TestCase):
    LIM = {"min_reward_risk": 2}

    def reasons(self, payload, limits=None):
        limits = self.LIM if limits is None else limits
        return risk.check_static(risk.measure(payload, limits), limits, TUE_10AM)

    def test_exactly_1_to_2_passes(self):
        self.assertEqual(self.reasons({**ENTRY, "takeProfit": {"limitPrice": 21020}}), [])

    def test_1_to_1_5_blocked(self):
        r = self.reasons({**ENTRY, "takeProfit": {"limitPrice": 21015}})
        self.assertIn("reward:risk 1:1.50 < min_reward_risk 1:2", r[0])

    def test_short_side(self):
        short = {**ENTRY, "action": "sell", "sentiment": "short", "stopLoss": {"stopPrice": 21010},
                 "takeProfit": {"limitPrice": 20980}}
        self.assertEqual(self.reasons(short), [])
        self.assertTrue(self.reasons({**short, "takeProfit": {"limitPrice": 20990}}))

    def test_percent_target(self):
        m = risk.measure({**ENTRY, "takeProfit": {"percent": 0.1}}, {})  # 21 pts vs 10-pt stop
        self.assertAlmostEqual(m["reward_risk"], 2.1)

    def test_no_target_or_unverified_amount_blocked(self):
        no_tp = {k: v for k, v in ENTRY.items() if k != "takeProfit"}
        self.assertIn("no take-profit", self.reasons(no_tp)[0])
        self.assertIn("unknown", self.reasons({**ENTRY, "takeProfit": {"amount": 30}})[0])

    def test_no_stop_blocked(self):
        no_sl = {k: v for k, v in ENTRY.items() if k != "stopLoss"}
        self.assertIn("without a stop-loss", self.reasons(no_sl)[0])

    def test_wrong_side_levels_always_blocked(self):
        r = self.reasons({**ENTRY, "stopLoss": {"stopPrice": 21010}, "takeProfit": {"limitPrice": 20970}}, {})
        text = " | ".join(r)
        self.assertIn("stop-loss 21010 is on the wrong side", text)
        self.assertIn("take-profit 20970 is on the wrong side", text)

    def test_rule_off_by_default(self):
        self.assertEqual(self.reasons({k: v for k, v in ENTRY.items() if k != "takeProfit"}, {}), [])


class LoadLimits(unittest.TestCase):
    def load(self, data):
        f = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        import json
        json.dump(data, f)
        f.close()
        self.addCleanup(os.unlink, f.name)
        return risk.load_limits(f.name)

    def test_per_root_dict_normalised(self):
        self.assertEqual(self.load({"max_contracts": {"MNQ1!": 2}})["max_contracts"], {"MNQ": 2})

    def test_bad_per_root_values(self):
        for bad in ({"max_contracts": {"MNQ": 0}}, {"max_contracts": {}}, {"futures_point_values": 5},
                    {"futures_session": "yes"}, {"max_stop_points": {"MNQ": "10"}}):
            with self.assertRaises(ValueError, msg=bad):
                self.load(bad)


class GateWiring(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        d = Path(self.tmp.name)
        os.environ["GATE_LOG"] = str(d / "log.jsonl")
        self.cfg = {"token": "x" * 20, "live": True, "mode": "veto", "min_conf": 70, "on_error": "block",
                    "max_age": 60, "limits": {k: v for k, v in LIMITS.items() if k != "futures_session"},
                    "ledger": risk.Ledger(str(d / "ledger.json")), "halt_file": str(d / "HALT")}
        self.sent, self.reviews, self.verdict = [], [], {"bias": "long", "confidence": 80}

    def analyzer(self, payload, direction):
        self.reviews.append((payload, direction))
        return self.verdict

    def forwarder(self, payload, attempts=1):
        self.sent.append((payload, attempts))
        return "HTTP 200: ok"

    def run_one(self, payload):
        return grok_gate.process(payload, self.cfg, self.analyzer, self.forwarder)

    def test_entry_reviewed_with_futures_prompt_and_sent(self):
        e = self.run_one({**ENTRY, "gate_notes": "RSI 61"})
        self.assertTrue(e["approved"])
        self.assertEqual(e["exchange"], "traderspost")
        self.assertEqual(e["risk"]["risk_usdt"], 20)
        prompt = grok_gate.build_prompt(ENTRY, "long")
        self.assertIn("MNQ: $2 per point", prompt)
        self.assertIn("(futures)", prompt)

    def test_exit_skips_everything_and_retries(self):
        Path(self.cfg["halt_file"]).touch()
        e = self.run_one(EXIT)
        self.assertTrue(e["approved"])
        self.assertEqual(self.sent, [(EXIT, 3)])
        self.assertEqual(self.reviews, [])

    def test_blocked_reversal_flattens_old_position(self):
        self.verdict = {"bias": "long", "confidence": 90}  # vetoes the new short
        rev = {**ENTRY, "action": "sell", "sentiment": "short", "prev_market_position": "long",
               "stopLoss": {"stopPrice": 21010}}
        e = self.run_one(rev)
        self.assertFalse(e["approved"])
        self.assertEqual(self.sent, [({"ticker": "CME_MINI:MNQ1!", "action": "exit"}, 3)])
        self.assertTrue(e["flatten"].startswith("HTTP 200"))

    def test_blocked_fresh_entry_does_not_flatten(self):
        self.verdict = {"bias": "short", "confidence": 90}
        self.run_one({**ENTRY, "prev_market_position": "flat"})
        self.assertEqual(self.sent, [])

    def test_duplicate_reversal_does_not_flatten_twice(self):
        self.verdict = {"bias": "long", "confidence": 90}
        rev = {**ENTRY, "action": "sell", "sentiment": "short", "prev_market_position": "long",
               "stopLoss": {"stopPrice": 21010}}
        dd = grok_gate.Deduper(60)
        grok_gate.process(rev, self.cfg, self.analyzer, self.forwarder, dd)
        grok_gate.process(rev, self.cfg, self.analyzer, self.forwarder, dd)
        self.assertEqual(len(self.sent), 1)

    def test_one_daily_budget_across_crypto_and_futures(self):
        crypto = {"exchange": "bybit", "coin_pair": "BTCUSDT.P", "position": "0", "qty": "100",
                  "qty_in_percentage": "2", "buy_leverage": "10", "force_tp": "1", "tp_type": "1",
                  "stop_loss_price": "2", "tp_3_size": "100", "tp_3_price": "4"}  # $20 risk, 1:2
        results = [self.run_one(p) for p in (crypto, {**ENTRY, "ticker": "MES1!", "price": 6000,
                   "stopLoss": {"stopPrice": 5996}}, ENTRY, {**ENTRY, "comment": "second signal"})]
        self.assertEqual([r["approved"] for r in results], [True, True, True, False])
        self.assertIn("max_daily_risk_usdt", results[3]["reason"])

    def test_routing(self):
        os.environ.pop("TRADERSPOST_WEBHOOK_URL", None)
        self.assertIsNone(grok_gate.destination(ENTRY)[0])
        os.environ["TRADERSPOST_WEBHOOK_URL"] = "http://insecure.example/x"
        self.assertIsNone(grok_gate.destination(ENTRY)[0])
        os.environ["TRADERSPOST_WEBHOOK_URL"] = "https://webhooks.traderspost.io/trading/webhook/abc/secret"
        self.addCleanup(os.environ.pop, "TRADERSPOST_WEBHOOK_URL", None)
        self.assertEqual(grok_gate.destination(ENTRY), (os.environ["TRADERSPOST_WEBHOOK_URL"], "traderspost"))
        self.assertTrue(grok_gate.destination({"exchange": "bybit"})[0].endswith("/webhooks/bybit"))

    def test_unrouted_futures_exit_is_logged_not_crashed(self):
        os.environ.pop("TRADERSPOST_WEBHOOK_URL", None)
        self.assertIn("TRADERSPOST_WEBHOOK_URL", grok_gate.forward(EXIT, attempts=1))

    def test_placeholder_ticker_blocked(self):
        e = self.run_one({**ENTRY, "ticker": "{{ticker}}"})
        self.assertIn("placeholder", e["reason"])

    def test_session_rule_applies_through_gate(self):
        self.cfg["limits"] = {**self.cfg["limits"], "futures_session": True}
        orig = grok_gate.time.time
        grok_gate.time.time = lambda: at(2026, 11, 28, 12, 0)  # Saturday
        self.addCleanup(setattr, grok_gate.time, "time", orig)
        e = self.run_one(ENTRY)
        self.assertIn("weekend", e["reason"])
        self.assertEqual(self.reviews, [])


if __name__ == "__main__":
    unittest.main()
