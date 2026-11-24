# Grok tools

Python standard library only. Nothing to install.

| File | What it does |
|---|---|
| `grok_client.py` | Talks to the Grok (xAI) API: `models`, `ask`, `respond`, `signal` |
| `grok_gate.py` | Webhook relay: Grok reviews TradingView **entries** before they reach StrategyFactory |
| `risk.py` | Risk limits checked in code before Grok: leverage, size, stop-loss, per-trade and daily risk, symbol lists, kill switch |
| `risk.example.json` | Starter limits for a $1,000 account. Copy it to `risk.json` |
| `test_grok_gate.py`, `test_risk.py` | Offline tests (66): `python3 -m unittest test_grok_gate test_risk -v` |

## Switching provider: Grok or FreeLLMAPI

The client and the gate both read the same settings.

| Setting | xAI Grok (default) | FreeLLMAPI (free tiers) |
|---|---|---|
| `LLM_PROVIDER` | `xai` | `freellmapi` |
| Base URL | `https://api.x.ai/v1` | `http://localhost:3001/v1` |
| Key variable | `XAI_API_KEY` | `FREELLMAPI_API_KEY` (from the dashboard's Keys page) |
| Default model | `grok-4.7` (or `XAI_MODEL`) | `auto` (the router picks) |

`LLM_BASE_URL`, `LLM_API_KEY` and `LLM_MODEL` override any of these, for example FreeLLMAPI on another machine or a pinned model.

```bash
python3 grok_client.py provider                                  # what's active (never prints the key)
LLM_PROVIDER=freellmapi python3 grok_client.py models
LLM_PROVIDER=freellmapi LLM_MODEL=gemini-3.6-flash python3 grok_client.py ask "Explain funding rates"
```

**Side-by-side dry run:** run two gates on different ports and send the same alert to both, then compare the two logs.

```bash
GATE_PORT=8080 GATE_LOG=grok.jsonl python3 grok_gate.py serve
LLM_PROVIDER=freellmapi GATE_PORT=8081 GATE_LOG=free.jsonl python3 grok_gate.py serve
```

- Each decision logs `grok._reviewer.model`: the model that **actually** answered. With `auto`, FreeLLMAPI may use a different model on each request.
- The gate **refuses `GATE_LIVE=1` with FreeLLMAPI** unless you set `GATE_ALLOW_FREE_LLM_LIVE=1`. Free tiers change models and run out of quota during the day.
- JSON wrapped in ```` ```json ```` fences or surrounded by extra text is accepted, which free models often produce.

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
- **Code checks run before Grok** and need no API call: quantity must be above 0, the pair must have no unfilled `{{ticker}}`, the signal must be under `GATE_MAX_AGE` seconds old (epoch or `{{timenow}}` ISO time), and an identical entry inside `GATE_DEDUP_SEC` is dropped.
- **Exits are never deduped or reviewed.** If StrategyFactory has a network error or returns 5xx, they're retried up to 3 times.
- **Grok has `GATE_REVIEW_TIMEOUT` seconds** (default 15) to answer, so a slow answer can't approve a trade on stale data.
- **If Grok errors or times out**, entries are blocked (`GATE_ON_ERROR=block`). Set `allow` to trade without review instead.
- **Dry run by default.** Nothing trades until you start it with `GATE_LIVE=1`.
- `gate_notes` is read by Grok and stripped before forwarding, so StrategyFactory receives exactly what the builder generated.
- Every decision goes to `gate_log.jsonl`. Credentials are never logged.

## Risk limits (ported from trader-risk)

Every new entry passes these checks **before** Grok is asked, so a blocked trade costs no API call. Exits skip all of them, so you can always get out of a position.

```
entry ─► kill switch ─► duplicate ─► precheck ─► risk limits ─► daily ledger ─► Grok ─► send
exit  ─────────────────────────────────────────────────────────────────────────────────► send (retried)
```

```bash
cp risk.example.json risk.json     # then edit the numbers for your account
touch HALT                          # kill switch: block every new entry right now
rm HALT                             # resume
```

| Key | Blocks an entry when |
|---|---|
| `blocked_symbols` / `allowed_symbols` | the symbol is blocked, or not on the allowed list (`BTCUSDT.P` and `BYBIT:BTCUSDT` both count as `BTCUSDT`) |
| `max_leverage` | leverage is higher |
| `max_margin_usdt` | margin committed is higher |
| `max_notional_usdt` | position size (margin × leverage) is higher. **This is your real exposure.** |
| `max_wallet_pct` | a %-of-wallet order is bigger |
| `require_stop_loss` | there's no stop-loss (`--sl` in the builder) |
| `max_stop_loss_pct` | the stop is wider than this % from entry |
| `max_risk_usdt` | the loss if the stop is hit (position size × stop %) is higher |
| `max_entries_per_day` / `max_entries_per_symbol_per_day` | today's count (UTC) is used up |
| `max_daily_risk_usdt` | today's stop-loss risk plus this trade's would go over the cap |
| `account_usdt` | not a rule: lets the gate value %-of-wallet orders in USDT |

- **Every failing rule is listed**, not just the first, so one read gives you the whole fix-list. The details are in each log line under `risk`.
- **An order the gate can't value is blocked, not waved through.** A %-of-wallet order needs `account_usdt`, and a base-quantity order needs `signal_price`. The builder's `--include-signal-meta` sends `signal_price`.
- **A typo in `risk.json` stops the gate from starting**, so a misspelled key can't silently turn a rule off. A missing `risk.json` means no limits. Live mode then warns at start-up.
- **The daily ledger** is `gate_ledger.json`, or `gate_ledger.dry.json` in dry run so practice runs don't use up your live budget. It survives restarts and resets at UTC midnight.
  - Budget is **reserved before Grok is asked**, so two alerts arriving together can't both squeeze under a cap.
  - It's **released if Grok vetoes** the trade or StrategyFactory refuses it with a 4xx.
  - A timeout or 5xx **still counts**, because the order may have filled.
- **What the gate can't see:** your balance, open positions and profit or loss live on the exchange. `max_daily_risk_usdt` therefore caps the worst-case loss you *commit to* each day, not your actual losses. Keep a daily loss limit on the exchange too, if it offers one.

**Rule of thumb from the example:** risk about 2% of the account per trade and about 6% per day. On $1,000, that's $100 margin at 10x with a 2% stop, which risks $20, and a $60 daily cap, which allows three losing trades.

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
