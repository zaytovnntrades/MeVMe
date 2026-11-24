"""Offline tests for grok_gate.py: fake Grok + fake StrategyFactory on localhost.

    python3 -m unittest test_grok_gate -v
"""
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
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
        self.grok_served_model = None
        self.grok_fenced = False
        self.grok_requests, self.forwarded = [], []
        self.forward_statuses = []  # popped per forward; empty = 200
        fakes = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                if self.path == "/v1/chat/completions":
                    fakes.grok_requests.append((self.headers.get("Authorization"), body))
                    content = json.dumps(fakes.grok_reply)
                    if fakes.grok_fenced:
                        content = "```json\n" + content + "\n```"
                    resp = {"choices": [{"message": {"content": content}}]}
                    if fakes.grok_served_model:
                        resp["model"] = fakes.grok_served_model
                    out = json.dumps(resp)
                    code = fakes.grok_status
                else:
                    fakes.forwarded.append((self.path, body))
                    code = fakes.forward_statuses.pop(0) if fakes.forward_statuses else 200
                    out = '{"success":true}' if code == 200 else '{"error":"down"}'
                self.send_response(code)
                self.end_headers()
                self.wfile.write(out.encode())

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.srv.server_address[1]}"


HERE = Path(__file__).resolve().parent


def run_py(code: str, **env) -> subprocess.CompletedProcess:
    base = {k: v for k, v in os.environ.items()
            if not k.startswith(("LLM_", "XAI_", "FREELLMAPI_", "GATE_"))}
    return subprocess.run([sys.executable, "-B", "-c", code], cwd=HERE, env={**base, **env},
                          capture_output=True, text=True, timeout=20)


class ProviderSwitch(unittest.TestCase):
    SHOW = "import grok_client as g; print(g.PROVIDER, g.BASE_URL, g.KEY_ENV, g.DEFAULT_MODEL)"

    def test_default_is_xai(self):
        self.assertEqual(run_py(self.SHOW).stdout.split(),
                         ["xai", "https://api.x.ai/v1", "XAI_API_KEY", "grok-4.7"])

    def test_freellmapi_preset(self):
        self.assertEqual(run_py(self.SHOW, LLM_PROVIDER="freellmapi").stdout.split(),
                         ["freellmapi", "http://localhost:3001/v1", "FREELLMAPI_API_KEY", "auto"])

    def test_overrides_win(self):
        out = run_py(self.SHOW, LLM_PROVIDER="freellmapi", LLM_BASE_URL="http://10.0.0.5:3001/v1/",
                     LLM_API_KEY="k", LLM_MODEL="gemini-3.6-flash").stdout.split()
        self.assertEqual(out, ["freellmapi", "http://10.0.0.5:3001/v1", "LLM_API_KEY", "gemini-3.6-flash"])

    def test_xai_model_still_honoured(self):
        self.assertEqual(run_py(self.SHOW, XAI_MODEL="grok-5").stdout.split()[-1], "grok-5")

    def test_unknown_provider_rejected(self):
        r = run_py(self.SHOW, LLM_PROVIDER="openai")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("LLM_PROVIDER must be one of", r.stderr)

    def test_missing_key_message_names_right_variable(self):
        r = run_py("import grok_client as g; g._key()", LLM_PROVIDER="freellmapi")
        self.assertIn("FREELLMAPI_API_KEY", r.stderr)

    def test_provider_command_never_prints_key(self):
        r = subprocess.run([sys.executable, "-B", "grok_client.py", "provider"], cwd=HERE,
                           env={**os.environ, "LLM_PROVIDER": "xai", "XAI_API_KEY": "xai-supersecret"},
                           capture_output=True, text=True, timeout=20)
        self.assertIn("XAI_API_KEY (set)", r.stdout)
        self.assertNotIn("supersecret", r.stdout)

    def test_live_gate_refused_on_free_tier(self):
        r = run_py("import grok_gate; grok_gate.serve()", LLM_PROVIDER="freellmapi",
                   FREELLMAPI_API_KEY="k", GATE_TOKEN=TOKEN, GATE_LIVE="1")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("Refusing LIVE mode", r.stderr)

    def test_parse_json_reply_tolerates_fences(self):
        p = grok_client.parse_json_reply
        self.assertEqual(p('{"bias":"long"}'), {"bias": "long"})
        self.assertEqual(p('```json\n{"bias":"short","confidence":80}\n```'), {"bias": "short", "confidence": 80})
        self.assertEqual(p('Sure! {"bias":"neutral"} Hope that helps.'), {"bias": "neutral"})
        with self.assertRaises(ValueError):
            p("no json here")


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

    def test_signal_age_formats(self):
        now = 1_800_000_000.0
        self.assertAlmostEqual(grok_gate.signal_age(str(now - 5), now), 5)
        self.assertAlmostEqual(grok_gate.signal_age(int((now - 5) * 1000), now), 5)  # milliseconds
        iso = datetime.fromtimestamp(now - 90, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")  # {{timenow}}
        self.assertAlmostEqual(grok_gate.signal_age(iso, now), 90)
        self.assertIsNone(grok_gate.signal_age("garbage", now))
        self.assertIsNone(grok_gate.signal_age(None, now))

    def test_precheck(self):
        now = time.time()
        self.assertIsNone(grok_gate.precheck(LONG, now, 60))
        self.assertIn("qty", grok_gate.precheck({**LONG, "qty": "0"}, now, 60))
        self.assertIn("qty", grok_gate.precheck({**LONG, "qty": "abc"}, now, 60))
        self.assertIn("placeholder", grok_gate.precheck({**LONG, "coin_pair": "{{ticker}}"}, now, 60))
        self.assertIn("stale", grok_gate.precheck({**LONG, "time": str(now - 120)}, now, 60))
        self.assertIsNone(grok_gate.precheck({**LONG, "time": str(now - 10)}, now, 60))

    def test_deduper_window(self):
        d = grok_gate.Deduper(60)
        self.assertFalse(d.is_dup(LONG, 1000))
        self.assertTrue(d.is_dup(dict(LONG), 1030))
        self.assertFalse(d.is_dup(LONG, 1100))  # window passed
        self.assertFalse(d.is_dup({**LONG, "time": "x"}, 1101))

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

    def test_builder_close_never_reaches_grok(self):
        """Bug in the pasted gate: reduce_by_percent closes were sent to review."""
        gate = self.start_gate(CFG)
        self.post(f"{gate}/tv/{TOKEN}", {**CLOSE_LONG, "position": "4"})
        self.logs(1)
        self.assertEqual(self.fakes.grok_requests, [])

    def test_exit_retried_when_strategyfactory_errors(self):
        """Bug in the pasted gate: a close was dropped when the executor returned 5xx."""
        self.fakes.forward_statuses = [502, 503]
        orig = grok_gate.time.sleep
        grok_gate.time.sleep = lambda s: None
        self.addCleanup(setattr, grok_gate.time, "sleep", orig)
        gate = self.start_gate(CFG)
        self.post(f"{gate}/tv/{TOKEN}", CLOSE_LONG)
        log = self.logs(1)[0]
        self.assertEqual(len(self.fakes.forwarded), 3)
        self.assertTrue(log["forward"].startswith("HTTP 200"))

    def test_exit_not_retried_on_4xx(self):
        self.fakes.forward_statuses = [400]
        gate = self.start_gate(CFG)
        self.post(f"{gate}/tv/{TOKEN}", CLOSE_LONG)
        self.assertTrue(self.logs(1)[0]["forward"].startswith("HTTP 400"))
        self.assertEqual(len(self.fakes.forwarded), 1)

    def test_iso_timenow_fresh_entry_is_reviewed(self):
        """Bug in the pasted gate: {{timenow}} ISO strings crashed the review and blocked every entry."""
        gate = self.start_gate(CFG)
        iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.post(f"{gate}/tv/{TOKEN}", {**LONG, "time": iso})
        self.assertTrue(self.logs(1)[0]["approved"])
        self.assertEqual(len(self.fakes.grok_requests), 1)

    def test_stale_entry_blocked_without_calling_grok(self):
        gate = self.start_gate(CFG)
        self.post(f"{gate}/tv/{TOKEN}", {**LONG, "time": str(time.time() - 600)})
        log = self.logs(1)[0]
        self.assertFalse(log["approved"])
        self.assertIn("stale", log["reason"])
        self.assertEqual(self.fakes.grok_requests + self.fakes.forwarded, [])

    def test_duplicate_entry_dropped_but_duplicate_exit_forwarded(self):
        gate = self.start_gate(CFG)
        for _ in range(2):
            self.post(f"{gate}/tv/{TOKEN}", LONG)
        logs = self.logs(2)
        self.assertEqual(sorted(x["approved"] for x in logs), [False, True])
        for _ in range(2):
            self.post(f"{gate}/tv/{TOKEN}", CLOSE_LONG)
        self.logs(4)
        self.assertEqual([p for p, _ in self.fakes.forwarded].count("/webhooks/bybit"), 3)

    def test_gate_token_never_forwarded(self):
        """Bug in the pasted gate: the token travelled in the body and was forwarded to the executor."""
        gate = self.start_gate(CFG)
        self.post(f"{gate}/tv/{TOKEN}", LONG)
        self.logs(1)
        self.assertNotIn(TOKEN, json.dumps(self.fakes.forwarded))

    def test_router_served_model_and_fenced_reply_logged(self):
        self.fakes.grok_served_model = "groq/llama-4-scout"
        self.fakes.grok_fenced = True
        gate = self.start_gate(CFG)
        self.post(f"{gate}/tv/{TOKEN}", LONG)
        log = self.logs(1)[0]
        self.assertTrue(log["approved"])
        self.assertEqual(log["grok"]["_reviewer"]["model"], "groq/llama-4-scout")

    def test_log_never_contains_credentials(self):
        gate = self.start_gate(CFG)
        self.post(f"{gate}/tv/{TOKEN}", LONG)
        self.logs(1)
        text = Path(os.environ["GATE_LOG"]).read_text()
        self.assertNotIn("ENC_K", text)
        self.assertNotIn("ENC_S", text)


if __name__ == "__main__":
    unittest.main()
