# Grok tools

Python standard library only. Nothing to install.

| File | What it does |
|---|---|
| `grok_client.py` | Talks to the Grok (xAI) API: `models`, `ask`, `respond`, `signal` |
| `grok_gate.py` | Webhook relay: Grok reviews TradingView **entries** before they reach StrategyFactory |
| `risk.py` | Risk limits checked in code before Grok: leverage, size, stop-loss, per-trade and daily risk, symbol lists, kill switch |
| `futures.py` | Nasdaq and other CME index futures through TradersPost: contract specs, contract-based risk, CME hours, reversal safety |
| `risk.example.json` | Starter limits for a $1,000 account. Copy it to `risk.json` |
| `test_grok_gate.py`, `test_risk.py`, `test_futures.py` | Offline tests (102): `python3 -m unittest test_grok_gate test_risk test_futures -v` |

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

## Futures (Nasdaq and other CME index futures)

One gate handles both markets. **The JSON shape decides the route:**

| Alert looks like | Market | Sent to |
|---|---|---|
| `{"exchange": "bybit", "coin_pair": ..., "position": ...}` | Crypto perpetuals | StrategyFactory |
| `{"ticker": ..., "action": ..., "sentiment": ...}` | Futures | TradersPost, which reaches Tradovate, NinjaTrader, TradeStation, IBKR and others |

```bash
export TRADERSPOST_WEBHOOK_URL="https://webhooks.traderspost.io/trading/webhook/..."   # from your TradersPost strategy; it's a secret
```

**TradingView alert message** for a strategy. Point the alert at the gate's URL, not at TradersPost:

```json
{
  "ticker": "{{ticker}}",
  "action": "{{strategy.order.action}}",
  "sentiment": "{{strategy.market_position}}",
  "prev_market_position": "{{strategy.prev_market_position}}",
  "quantity": "{{strategy.order.contracts}}",
  "price": "{{close}}",
  "time": "{{timenow}}",
  "stopLoss": {"type": "stop", "stopPrice": "{{plot(\"SL\")}}"},
  "gate_notes": "RSI {{plot(\"RSI\")}}, VWAP {{plot(\"VWAP\")}}"
}
```

The `{{plot("SL")}}` placeholder needs a plot named `SL` in your Pine script, and the same goes for the `gate_notes` plots. Check every field against TradersPost's own docs before going live: the gate forwards this JSON exactly as sent, minus `gate_notes`.

**How the gate reads a futures alert:**
- **Exits are never blocked:**
  - `action` is `exit` or `cancel`
  - `sentiment` is `flat`
  - a buy or sell against the current sentiment, like `sell` + `long`, which trims a long
  - anything it doesn't recognise
- **Entries:** `buy` + `long` and `sell` + `short`. These go through the same chain as crypto: kill switch, duplicate check, precheck, risk limits, daily ledger, then Grok.
- **Reversal safety:** a blocked entry that was a reversal, where `prev_market_position` is the opposite side, sends `{"ticker": ..., "action": "exit"}` to close the old position. A veto then means "flat", never "stuck in the trade the strategy wanted out of". Without `prev_market_position` the gate can't tell, so keep that field in the alert.

**Contract specs built in** (dollars per 1.00 point): NQ $20, MNQ $2, ES $50, MES $5, YM $5, MYM $0.50, RTY $50, M2K $5. Add others with `futures_point_values`, for example `{"CL": 1000}`. **Trade risk = contracts × stop points × $/point.** A 20-point stop costs $40 on 1 MNQ and $400 on 1 NQ.

**Futures limits in `risk.json`** (a number covers every contract; a dict sets one per contract):

| Key | Blocks an entry when |
|---|---|
| `max_contracts` | more contracts, for example `{"MNQ": 2, "MES": 2}` |
| `max_stop_points` | the stop is wider in points, for example `{"MNQ": 15, "MES": 6}` |
| `futures_session` | outside CME hours: Sunday 18:00 to Friday 17:00 ET, with a 17:00–18:00 break each day |
| `no_entry_minutes_before_close` | it's within N minutes of the 17:00 ET close |
| `futures_point_values` | not a rule: $/point for contracts not built in |

These rules apply to **both markets**: `allowed_symbols` and `blocked_symbols` (use roots like `MNQ`), `require_stop_loss`, `max_risk_usdt` (read as USD), the daily counts and `max_daily_risk_usdt`. **The daily risk budget is shared**, so a crypto loss and a futures loss come out of the same pot. The crypto-only rules (`max_leverage`, `max_margin_usdt`, `max_notional_usdt`, `max_wallet_pct`, `max_stop_loss_pct`) never touch futures. One MNQ contract is worth about $40k notional, which would trip any crypto-sized cap.

**When the gate refuses to guess, it blocks the entry instead:**
- `stopLoss.amount` (its exact meaning isn't verified here). Use `stopPrice`, or `percent` with `price`.
- A `quantityType` other than a fixed contract count.
- No `quantity`, which means TradersPost sizes the order itself.
- A contract with no known point value.

**Not handled:** CME holidays and early closes. Check the CME calendar or `touch HALT` on those days.

**Known gap on crypto:** a vetoed crypto reversal (`exit_existing_trade:"1"`) leaves the old position open, because StrategyFactory alerts don't carry the previous position. Send separate `close_long` / `close_short` alerts, which the builder makes and the gate never blocks.

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
