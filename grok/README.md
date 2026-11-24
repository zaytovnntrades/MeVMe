# Grok tools

Python standard library only. Nothing to install.

| File | What it does |
|---|---|
| `grok_client.py` | Talks to the Grok (xAI) API: `models`, `ask`, `respond`, `signal` |
| `grok_gate.py` | Webhook relay: Grok reviews TradingView **entries** before they reach StrategyFactory |
| `test_grok_gate.py` | Offline tests with a fake Grok and a fake StrategyFactory: `python3 -m unittest test_grok_gate -v` |

## How the gate works

```
TradingView alert ──► grok_gate  /tv/<GATE_TOKEN>
                        │  replies 200 at once (TradingView times out after a few seconds)
                        ├─ exit (close / reduce) ──────────────────────► forwarded immediately, never blocked
                        └─ entry (long / short) ─► Grok reviews ─┬─ approved ─► fastcloud /webhooks/<exchange>
                                                                 └─ blocked ──► logged only
```

- **veto** mode (default): an entry is blocked only when Grok leans the *opposite* way with confidence ≥ `GATE_MIN_CONFIDENCE` (default 70).
- **confirm** mode: an entry is allowed only when Grok *agrees* with confidence ≥ the threshold.
- **If Grok errors or times out**, entries are blocked (`GATE_ON_ERROR=block`). Set `allow` to trade without review instead.
- **Dry run by default.** Nothing trades until you start it with `GATE_LIVE=1`.
- `gate_notes` is read by Grok and stripped before forwarding, so StrategyFactory receives exactly what the builder generated.
- Every decision goes to `gate_log.jsonl`. Credentials are never logged.

## Setup, step by step

1. **Build the alerts** with `tradingview-alert-builder`, for example into `./my-strategy`.
2. **Create the gate secret:**
   ```bash
   export GATE_TOKEN="$(python3 -c 'import secrets;print(secrets.token_urlsafe(24))')"
   ```
3. **Make the gated copy.** Your alerts will now point at the gate:
   ```bash
   python3 grok_gate.py patch ./my-strategy --public-url https://YOUR-PUBLIC-HOST
   ```
   This writes `./my-strategy-gated/`, which contains the new `webhook_url.txt`, the entry JSONs with `gate_notes`, and a Pine file with `gateNotes` (RSI, close and EMA200).
4. **Run the gate** on a machine TradingView can reach (TradingView only sends webhooks to ports 80 and 443):
   ```bash
   export XAI_API_KEY="xai-..."
   python3 grok_gate.py serve          # dry run: watch the log for a few days
   GATE_LIVE=1 python3 grok_gate.py serve
   ```
   - **Quick test:** `cloudflared tunnel --url http://localhost:8080` gives you a temporary HTTPS URL.
   - **For 24/7 running:** a small VPS behind Caddy or nginx with HTTPS.
5. **Paste into TradingView:** each gated JSON goes in the alert **Message**, and the gated `webhook_url.txt` goes in **Webhook URL**.
6. **Test with tiny size first.** Fire one alert, read `gate_log.jsonl`, and check the exchange.

## What helps most

- **Give Grok real data.** It has no live market feed through this API, so it judges only what you send in `gate_notes`. Plain JSON alerts can only use built-in placeholders (`{{close}}`, `{{volume}}`, `{{interval}}`). The Pine `alert_message` route can send any indicator value, so use it.
- **Run a dry run for at least a week.** Compare the entries Grok would have blocked with how those trades turned out. If the vetoes don't beat chance, turn the gate off.
- **Keep your GATE_TOKEN secret.** The gate URL plus a payload can trigger trades, just like the original files.
