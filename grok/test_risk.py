"""Tests for risk.py and its wiring into grok_gate.process().

    python3 -m unittest test_risk -v
"""
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path

import grok_gate
import risk

HERE = Path(__file__).resolve().parent
ENTRY = {"exchange": "bybit", "api_key": "ENC_K", "api_secret": "ENC_S", "coin_pair": "BTCUSDT.P",
         "position": "0", "qty": "100", "qty_in_percentage": "2", "buy_leverage": "10", "sell_leverage": "10",
         "force_tp": "1", "tp_type": "1", "tp_3_size": "100", "tp_3_price": "4", "stop_loss_price": "2"}
EXIT = {"exchange": "bybit", "coin_pair": "BTCUSDT.P", "mode": "reduce_by_percent", "position": "3",
        "reduce_percent": "100"}
LIMITS = risk.load_limits(str(HERE / "risk.example.json"))


class Measure(unittest.TestCase):
    def test_usdt_sizing(self):
        m = risk.measure(ENTRY, {})
        self.assertEqual((m["symbol"], m["sizing"], m["leverage"]), ("BTCUSDT", "usdt", 10))
        self.assertAlmostEqual(m["margin_usdt"], 100)
        self.assertAlmostEqual(m["notional_usdt"], 1000)
        self.assertAlmostEqual(m["risk_usdt"], 20)  # 1000 x 2%

    def test_short_uses_sell_leverage(self):
        m = risk.measure({**ENTRY, "position": "1", "buy_leverage": "3", "sell_leverage": "20"}, {})
        self.assertEqual(m["leverage"], 20)

    def test_percent_sizing_needs_account(self):
        p = {**ENTRY, "qty_in_percentage": "1", "qty": "5"}
        self.assertIsNone(risk.measure(p, {})["margin_usdt"])
        m = risk.measure(p, {"account_usdt": 2000})
        self.assertAlmostEqual(m["margin_usdt"], 100)
        self.assertEqual(m["wallet_pct"], 5)

    def test_base_sizing_needs_price(self):
        p = {**ENTRY, "qty_in_percentage": "0", "qty": "0.01"}
        self.assertIsNone(risk.measure(p, {})["notional_usdt"])
        self.assertAlmostEqual(risk.measure({**p, "signal_price": "100000"}, {})["notional_usdt"], 1000)

    def test_absolute_stop_converted_with_price(self):
        p = {**ENTRY, "tp_type": "0", "stop_loss_price": "98000"}
        self.assertIsNone(risk.measure(p, {})["stop_loss_pct"])
        self.assertAlmostEqual(risk.measure({**p, "signal_price": "100000"}, {})["stop_loss_pct"], 2)

    def test_stop_needs_force_tp(self):
        self.assertFalse(risk.measure({**ENTRY, "force_tp": "0"}, {})["has_stop_loss"])  # SF ignores it

    def test_symbol_normalised(self):
        for s in ("BTCUSDT.P", "btcusdt", "BYBIT:BTCUSDT.P", " BTCUSDT "):
            self.assertEqual(risk.norm_symbol(s), "BTCUSDT")


class Static(unittest.TestCase):
    def reasons(self, payload, limits=LIMITS):
        return risk.check_static(risk.measure(payload, limits), limits)

    def test_example_max_trade_passes(self):
        self.assertEqual(self.reasons(ENTRY), [])

    def test_no_limits_passes_anything(self):
        self.assertEqual(self.reasons({**ENTRY, "buy_leverage": "125", "force_tp": "0"}, {}), [])

    def test_every_failing_rule_reported(self):
        r = self.reasons({**ENTRY, "coin_pair": "DOGEUSDT.P", "qty": "500", "buy_leverage": "50",
                          "stop_loss_price": "5"})
        text = " | ".join(r)
        for word in ("not allowed", "leverage 50x", "margin 500.00", "position size 25,000.00",
                     "stop-loss 5.00%", "trade risk 1,250.00"):
            self.assertIn(word, text)

    def test_blocked_list_wins(self):
        self.assertIn("blocked", self.reasons(ENTRY, {**LIMITS, "blocked_symbols": ["BTCUSDT"]})[0])

    def test_missing_stop_loss(self):
        text = " | ".join(self.reasons({**ENTRY, "force_tp": "0"}))
        self.assertIn("no stop-loss", text)
        self.assertIn("loss is unbounded", text)

    def test_unvaluable_orders_blocked_not_waved_through(self):
        r = self.reasons({**ENTRY, "qty_in_percentage": "0", "qty": "0.01"}, {"max_notional_usdt": 1000})
        self.assertIn("signal_price", r[0])
        r = self.reasons({**ENTRY, "qty_in_percentage": "1", "qty": "5"}, {"max_margin_usdt": 100})
        self.assertIn("account_usdt", r[0])

    def test_wallet_pct(self):
        r = self.reasons({**ENTRY, "qty_in_percentage": "1", "qty": "15"}, {"max_wallet_pct": 10})
        self.assertIn("15% of wallet", r[0])


class CryptoRewardRisk(unittest.TestCase):
    LIM = {"min_reward_risk": 2}

    def reasons(self, payload, limits=None):
        limits = self.LIM if limits is None else limits
        return risk.check_static(risk.measure(payload, limits), limits)

    def test_builder_tp4_sl2_is_1_to_2(self):
        self.assertAlmostEqual(risk.measure(ENTRY, {})["reward_risk"], 2)
        self.assertEqual(self.reasons(ENTRY), [])

    def test_tighter_target_blocked(self):
        self.assertIn("1:1.50", self.reasons({**ENTRY, "tp_3_price": "3"})[0])

    def test_multi_tp_uses_size_weighted_average(self):
        multi = {**ENTRY, "multi_tp": "1", "tp_1_size": "40", "tp_1_price": "2", "tp_2_size": "30",
                 "tp_2_price": "4", "tp_3_size": "30", "tp_3_price": "6"}
        self.assertAlmostEqual(risk.measure(multi, {})["reward_risk"], 1.9)  # (0.4x2+0.3x4+0.3x6)/2
        self.assertIn("1:1.90", self.reasons(multi)[0])
        self.assertEqual(self.reasons({**multi, "tp_1_price": "3"}), [])  # 4.2 / 2 = 2.1

    def test_single_tp_ignores_unsent_legs(self):
        self.assertAlmostEqual(risk.measure({**ENTRY, "tp_1_size": "50", "tp_1_price": "0.5"}, {})["reward_risk"], 2)

    def test_absolute_prices(self):
        p = {**ENTRY, "tp_type": "0", "signal_price": "100000", "stop_loss_price": "99000", "tp_3_price": "102000"}
        self.assertAlmostEqual(risk.measure(p, {})["reward_risk"], 2)
        bad = {**p, "stop_loss_price": "101000", "tp_3_price": "98000"}
        text = " | ".join(self.reasons(bad, {}))
        self.assertIn("stop-loss 101000 is on the wrong side", text)
        self.assertIn("take-profit 98000 is on the wrong side", text)

    def test_absolute_without_price_cant_be_measured(self):
        p = {**ENTRY, "tp_type": "0", "stop_loss_price": "99000", "tp_3_price": "102000"}
        self.assertIn("can't be measured", self.reasons(p)[0])

    def test_no_take_profit(self):
        self.assertIn("no take-profit", self.reasons({**ENTRY, "tp_3_size": "0"})[0])


class LoadLimits(unittest.TestCase):
    def write(self, data):
        f = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        json.dump(data, f)
        f.close()
        self.addCleanup(os.unlink, f.name)
        return f.name

    def test_missing_file_means_no_limits(self):
        self.assertEqual(risk.load_limits("/nonexistent/risk.json"), {})

    def test_typo_is_an_error_not_a_silent_off(self):
        with self.assertRaisesRegex(ValueError, "max_leverge"):
            risk.load_limits(self.write({"max_leverge": 5}))

    def test_bad_values_rejected(self):
        for bad in ({"max_leverage": 0}, {"max_leverage": "10"}, {"max_risk_usdt": -1},
                    {"require_stop_loss": "yes"}, {"blocked_symbols": "BTC"}, {"max_leverage": True}):
            with self.assertRaises(ValueError, msg=bad):
                risk.load_limits(self.write(bad))

    def test_null_turns_rule_off(self):
        self.assertEqual(risk.load_limits(self.write({"max_leverage": None})), {})

    def test_example_file_is_valid(self):
        self.assertEqual(LIMITS["max_risk_usdt"], 20)


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = str(Path(self.tmp.name) / "ledger.json")
        self.now = [1_800_000_000.0]
        self.ledger = risk.Ledger(self.path, clock=lambda: self.now[0])
        self.m = risk.measure(ENTRY, LIMITS)  # 20 USDT risk

    def test_daily_entry_cap(self):
        lim = {"max_entries_per_day": 2}
        for _ in range(2):
            self.assertTrue(self.ledger.reserve(self.m, lim)[0])
        rid, r = self.ledger.reserve(self.m, lim)
        self.assertIsNone(rid)
        self.assertIn("2 entries already today", r[0])

    def test_per_symbol_cap(self):
        lim = {"max_entries_per_symbol_per_day": 1}
        self.assertTrue(self.ledger.reserve(self.m, lim)[0])
        self.assertIsNone(self.ledger.reserve(self.m, lim)[0])
        eth = risk.measure({**ENTRY, "coin_pair": "ETHUSDT.P"}, LIMITS)
        self.assertTrue(self.ledger.reserve(eth, lim)[0])

    def test_daily_risk_budget(self):
        lim = {"max_daily_risk_usdt": 60}
        for _ in range(3):
            self.assertTrue(self.ledger.reserve(self.m, lim)[0])
        rid, r = self.ledger.reserve(self.m, lim)
        self.assertIsNone(rid)
        self.assertIn("60.00 used + 20.00", r[0])

    def test_release_gives_budget_back(self):
        lim = {"max_entries_per_day": 1}
        rid, _ = self.ledger.reserve(self.m, lim)
        self.ledger.release(rid)
        self.assertTrue(self.ledger.reserve(self.m, lim)[0])

    def test_resets_at_utc_midnight(self):
        lim = {"max_entries_per_day": 1}
        self.assertTrue(self.ledger.reserve(self.m, lim)[0])
        self.assertIsNone(self.ledger.reserve(self.m, lim)[0])
        self.now[0] += 86400
        self.assertTrue(self.ledger.reserve(self.m, lim)[0])

    def test_survives_restart(self):
        rid, _ = self.ledger.reserve(self.m, {})
        self.ledger.commit(rid)
        again = risk.Ledger(self.path, clock=lambda: self.now[0])
        self.assertEqual(again.totals()["count"], 1)
        self.assertIsNone(again.reserve(self.m, {"max_entries_per_day": 1})[0])

    def test_corrupt_file_starts_clean(self):
        Path(self.path).write_text("{not json")
        self.assertEqual(risk.Ledger(self.path, clock=lambda: self.now[0]).totals()["count"], 0)

    def test_concurrent_reservations_cannot_overshoot(self):
        lim, got = {"max_entries_per_day": 5}, []
        threads = [threading.Thread(target=lambda: got.append(self.ledger.reserve(self.m, lim)[0]))
                   for _ in range(40)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len([g for g in got if g]), 5)


class GateWiring(unittest.TestCase):
    """process() end to end with stub reviewer and forwarder."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        d = Path(self.tmp.name)
        os.environ["GATE_LOG"] = str(d / "log.jsonl")
        self.halt = d / "HALT"
        self.cfg = {"token": "x" * 20, "live": True, "mode": "veto", "min_conf": 70, "on_error": "block",
                    "max_age": 60, "limits": LIMITS, "ledger": risk.Ledger(str(d / "ledger.json")),
                    "halt_file": str(self.halt)}
        self.reviews, self.sent, self.verdict, self.forward_status = [], [], {"bias": "long", "confidence": 80}, 200

    def analyzer(self, payload, direction):
        self.reviews.append(payload)
        return self.verdict

    def forwarder(self, payload, attempts=1):
        self.sent.append(payload)
        return f"HTTP {self.forward_status}: ok"

    def run_one(self, payload):
        return grok_gate.process(payload, self.cfg, self.analyzer, self.forwarder)

    def test_passing_entry_is_reviewed_sent_and_counted(self):
        e = self.run_one(ENTRY)
        self.assertTrue(e["approved"])
        self.assertEqual(e["risk"]["risk_usdt"], 20)
        self.assertEqual(e["risk"]["day"]["count"], 1)

    def test_risk_block_skips_grok_and_lists_all_reasons(self):
        e = self.run_one({**ENTRY, "buy_leverage": "50", "force_tp": "0"})
        self.assertFalse(e["approved"])
        self.assertIn("leverage 50x", e["reason"])
        self.assertIn("no stop-loss", e["reason"])
        self.assertEqual(self.reviews + self.sent, [])

    def test_daily_budget_stops_the_fourth_trade(self):
        results = [self.run_one({**ENTRY, "coin_pair": c}) for c in
                   ("BTCUSDT.P", "ETHUSDT.P", "SOLUSDT.P", "BTCUSDT.P")]
        self.assertEqual([r["approved"] for r in results], [True, True, True, False])
        self.assertIn("max_daily_risk_usdt", results[3]["reason"])

    def test_grok_veto_releases_the_budget(self):
        self.verdict = {"bias": "short", "confidence": 90}
        for _ in range(5):
            self.assertFalse(self.run_one(ENTRY)["approved"])
        self.verdict = {"bias": "long", "confidence": 80}
        self.assertTrue(self.run_one(ENTRY)["approved"])
        self.assertEqual(self.cfg["ledger"].totals()["count"], 1)

    def test_refused_send_not_counted_but_5xx_is(self):
        self.forward_status = 400
        self.run_one(ENTRY)
        self.assertEqual(self.cfg["ledger"].totals()["count"], 0)
        self.forward_status = 502  # may have filled: count it
        self.run_one(ENTRY)
        self.assertEqual(self.cfg["ledger"].totals()["count"], 1)

    def test_halt_blocks_entries_but_never_exits(self):
        self.halt.touch()
        e = self.run_one(ENTRY)
        self.assertFalse(e["approved"])
        self.assertIn("halted", e["reason"])
        self.assertTrue(self.run_one(EXIT)["approved"])
        self.assertEqual(len(self.sent), 1)
        self.halt.unlink()
        self.assertTrue(self.run_one(ENTRY)["approved"])

    def test_exits_ignore_spent_budget(self):
        self.cfg["limits"] = {**LIMITS, "max_entries_per_day": 1}
        self.run_one(ENTRY)
        self.assertFalse(self.run_one({**ENTRY, "coin_pair": "ETHUSDT.P"})["approved"])
        self.assertTrue(self.run_one(EXIT)["approved"])

    def test_without_limits_or_ledger_gate_works_as_before(self):
        self.cfg.pop("limits"), self.cfg.pop("ledger"), self.cfg.pop("halt_file")
        self.assertTrue(self.run_one({**ENTRY, "buy_leverage": "100", "force_tp": "0"})["approved"])


if __name__ == "__main__":
    unittest.main()
