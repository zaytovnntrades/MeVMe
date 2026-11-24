"""Offline tests for grok_gate.py: fake Grok + fake StrategyFactory on localhost.

    python3 -m unittest test_grok_gate -v
"""
import json
import os
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import grok_client
import grok_gate

TOKEN = "test-token-0123456789abcdef"
LONG = {"exchange": "bybit", "api_key": "ENC_K", "api_secret": "ENC_S", "secret_key": "ENC_S",
        "passphrase": "", "encryptor": "2", "coin_pair": "BTCUSDT.P", "position": "0", "qty": "10",
        "buy_leverage": "5", "sell_leverage": "5", "force_tp": "1", "tp_type": "1",
        "tp_3_size": "100", "tp_3_price": "4", "stop_loss_price": "2",
        "gate_notes": "RSI14 72, close 97250, EMA200 91000"}
CLOSE_LONG = {"exchange": "bybit", "api_key": "ENC_K", "api_secret": "ENC_S", "coin_pair": "BTCUSDT.P",
              "mode": "reduce_by_percent", "position": "3", "reduce_percent": "100"}
CFG = {"token": TOKEN, "live": True, "mode": "veto", "min_conf": 70, "on_error": "block"}


class Fakes:
    """Fake xAI and fake fastcloud servers on random localhost ports."""

    def __init__(self):
        self.grok_reply = {"bias": "long", "confidence": 80, "reasons": ["trend"], "invalidation": "x",
                           "risk_note": "y"}
        self.grok_status = 200
        self.grok_requests, self.forwarded = [], []
        fakes = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                if self.path == "/v1/chat/completions":
                    fakes.grok_requests.append((self.headers.get("Authorization"), body))
                    out = json.dumps({"choices": [{"message": {"content": json.dumps(fakes.grok_reply)}}]})
                    code = fakes.grok_status
                else:
                    fakes.forwarded.append((self.path, body))
                    out, code = '{"success":true}', 200
                self.send_response(code)
                self.end_headers()
                self.wfile.write(out.encode())

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.srv.server_address[1]}"


class LogicTests(unittest.TestCase):
    def test_classify(self):
        self.assertEqual(grok_gate.classify(LONG), ("entry", "long"))
        self.assertEqual(grok_gate.classify({**LONG, "position": "1"}), ("entry", "short"))
        self.assertEqual(grok_gate.classify(CLOSE_LONG), ("exit", None))
        self.assertEqual(grok_gate.classify({"position": "4"}), ("exit", None))

    def test_veto_mode(self):
        d = grok_gate.decide
        self.assertTrue(d({"bias": "long", "confidence": 90}, "long", "veto", 70)[0])
        self.assertTrue(d({"bias": "neutral", "confidence": 90}, "long", "veto", 70)[0])
        self.assertTrue(d({"bias": "short", "confidence": 60}, "long", "veto", 70)[0])
        self.assertFalse(d({"bias": "short", "confidence": 75}, "long", "veto", 70)[0])

    def test_confirm_mode(self):
        d = grok_gate.decide
        self.assertTrue(d({"bias": "short", "confidence": 70}, "short", "confirm", 70)[0])
        self.assertFalse(d({"bias": "neutral", "confidence": 99}, "short", "confirm", 70)[0])
        self.assertFalse(d({"bias": "short", "confidence": 69}, "short", "confirm", 70)[0])
        self.assertFalse(d({"bias": "short", "confidence": "junk"}, "short", "confirm", 70)[0])

    def test_strip_and_redact(self):
        self.assertNotIn("gate_notes", grok_gate.forward_body(LONG))
        self.assertEqual(grok_gate.forward_body(LONG)["api_key"], "ENC_K")
        r = grok_gate.redact(LONG)
        self.assertEqual((r["api_key"], r["api_secret"], r["secret_key"]), ("***", "***", "***"))


class EndToEnd(unittest.TestCase):
    def setUp(self):
        self.fakes = Fakes()
        self._orig = (grok_client.BASE_URL, grok_gate.FORWARD_BASE, os.environ.get("XAI_API_KEY"))
        grok_client.BASE_URL = self.fakes.base + "/v1"
        grok_gate.FORWARD_BASE = self.fakes.base
        os.environ["XAI_API_KEY"] = "xai-test"
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["GATE_LOG"] = str(Path(self.tmp.name) / "log.jsonl")

    def tearDown(self):
        grok_client.BASE_URL, grok_gate.FORWARD_BASE, key = self._orig
        os.environ.pop("XAI_API_KEY") if key is None else os.environ.__setitem__("XAI_API_KEY", key)
        self.fakes.srv.shutdown()
        self.fakes.srv.server_close()
        self.tmp.cleanup()

    def start_gate(self, cfg):
        gate = ThreadingHTTPServer(("127.0.0.1", 0), grok_gate.make_handler(cfg))
        threading.Thread(target=gate.serve_forever, daemon=True).start()
        self.addCleanup(gate.server_close)
        self.addCleanup(gate.shutdown)
        return f"http://127.0.0.1:{gate.server_address[1]}"

    def post(self, url, body):
        req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                     headers={"Content-Type": "text/plain"})  # what TradingView sends
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status
        except urllib.error.HTTPError as e:
            return e.code

    def logs(self, n, timeout=5):
        p, end = Path(os.environ["GATE_LOG"]), time.time() + timeout
        while time.time() < end:
            if p.exists() and len(p.read_text().splitlines()) >= n:
                return [json.loads(x) for x in p.read_text().splitlines()]
            time.sleep(0.05)
        self.fail("timed out waiting for gate log")

    def test_agreeing_entry_is_forwarded_without_gate_fields(self):
        gate = self.start_gate(CFG)
        self.assertEqual(self.post(f"{gate}/tv/{TOKEN}", LONG), 200)
        log = self.logs(1)[0]
        self.assertTrue(log["approved"])
        path, body = self.fakes.forwarded[0]
        self.assertEqual(path, "/webhooks/bybit")
        self.assertNotIn("gate_notes", body)
        self.assertEqual(body["api_key"], "ENC_K")
        auth, grok_body = self.fakes.grok_requests[0]
        self.assertEqual(auth, "Bearer xai-test")
        self.assertIn("RSI14 72", grok_body["messages"][1]["content"])
        self.assertIn("OPEN LONG", grok_body["messages"][1]["content"])

    def test_opposing_entry_is_blocked(self):
        self.fakes.grok_reply = {"bias": "short", "confidence": 85}
        gate = self.start_gate(CFG)
        self.post(f"{gate}/tv/{TOKEN}", LONG)
        self.assertFalse(self.logs(1)[0]["approved"])
        self.assertEqual(self.fakes.forwarded, [])

    def test_exit_skips_grok_even_if_grok_is_down(self):
        self.fakes.grok_status = 500
        gate = self.start_gate(CFG)
        self.post(f"{gate}/tv/{TOKEN}", CLOSE_LONG)
        self.assertTrue(self.logs(1)[0]["approved"])
        self.assertEqual(self.fakes.grok_requests, [])
        self.assertEqual(self.fakes.forwarded[0][0], "/webhooks/bybit")

    def test_grok_error_blocks_entry_by_default_and_allows_if_configured(self):
        self.fakes.grok_status = 500
        gate = self.start_gate(CFG)
        self.post(f"{gate}/tv/{TOKEN}", LONG)
        self.assertFalse(self.logs(1)[0]["approved"])
        gate2 = self.start_gate({**CFG, "on_error": "allow"})
        self.post(f"{gate2}/tv/{TOKEN}", LONG)
        self.assertTrue(self.logs(2)[1]["approved"])
        self.assertEqual(len(self.fakes.forwarded), 1)

    def test_dry_run_forwards_nothing(self):
        gate = self.start_gate({**CFG, "live": False})
        self.post(f"{gate}/tv/{TOKEN}", LONG)
        log = self.logs(1)[0]
        self.assertTrue(log["approved"])
        self.assertEqual(log["forward"], "dry run: not forwarded")
        self.assertEqual(self.fakes.forwarded, [])

    def test_bad_token_and_bad_body_rejected(self):
        gate = self.start_gate(CFG)
        self.assertEqual(self.post(f"{gate}/tv/wrong-token-xxxxxxxxxxxx", LONG), 404)
        self.assertEqual(self.post(f"{gate}/tv/{TOKEN}", ["not", "an", "object"]), 400)
        time.sleep(0.3)
        self.assertEqual(self.fakes.grok_requests + self.fakes.forwarded, [])

    def test_log_never_contains_credentials(self):
        gate = self.start_gate(CFG)
        self.post(f"{gate}/tv/{TOKEN}", LONG)
        self.logs(1)
        text = Path(os.environ["GATE_LOG"]).read_text()
        self.assertNotIn("ENC_K", text)
        self.assertNotIn("ENC_S", text)


if __name__ == "__main__":
    unittest.main()
