---
name: tvclaude-setup
description: Deploy and configure tvclaude, the TradingView-to-Claude NQ/MNQ analysis system (FastAPI backend, Next.js dashboard, Shield, Lab, Coordinator reports to Telegram and Discord). Use when the user wants to run tvclaude, fill its .env, connect TradingView webhooks, set up Telegram or Discord reports, or fix a tvclaude startup or webhook problem.
---

# tvclaude setup

tvclaude lives in the user's MeVMe repo under `tvclaude/`. Its README is the source of truth: read `tvclaude/README.md` and `tvclaude/.env.example` before giving steps. It is analysis and paper trading only. There is no broker execution; adding any requires the user's explicit authorization.

## Secrets: the hard rule
- Secrets go **only** in `tvclaude/.env` (git-ignored) or the host's secret store. Never in chat, code, commits, the frontend or a Pine script.
- If the user pastes a key or webhook URL into chat, tell them to rotate it (for Discord: Server Settings → Integrations → Webhooks → delete, then create a new one).
- Before committing anything, check `git status` doesn't include `.env`.

## Steps
1. `cp tvclaude/.env.example tvclaude/.env`, then fill in:
   - `ANTHROPIC_API_KEY`, `WEBHOOK_SECRET` (long random), `DASHBOARD_TOKEN` (long random), `POSTGRES_PASSWORD`
   - `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID`: create a bot with @BotFather, message it once, read the chat id from `https://api.telegram.org/bot<token>/getUpdates`
   - `DISCORD_WEBHOOK_URL`: channel → Edit → Integrations → Webhooks → New
   - Account profile is preset for MFF Rapid 25K: `DRAWDOWN_MODE=eod_trailing`, `DAILY_LOSS_LIMIT=500`, `MAX_CONSECUTIVE_LOSSES=2`, `ENTRY_WINDOW=09:35-11:30`. Ask the user to confirm the drawdown lock level against their MFF dashboard.
   Generate random values with `python3 -c "import secrets; print(secrets.token_urlsafe(32))"` and have the user paste them into `.env` themselves.
2. Run: `cd tvclaude && docker compose up -d --build`. Dashboard on `:3000`, API docs on `:8000/docs`.
3. Public HTTPS: TradingView only posts to ports 80/443. Use Caddy/nginx with TLS and `TRUST_PROXY=true`, or for testing `cloudflared tunnel --url http://localhost:8000`.
4. TradingView: load the NQ Setups strategy (see the `nq-pine` skill), set the same `WEBHOOK_SECRET` in its inputs, and create one alert with "alert() function calls only" pointing at `https://<host>/webhook/tradingview`.
5. Check: send the sample payload in `tvclaude/backend/tests/payloads/`, watch it appear in the dashboard, then press **Send daily report** in the Coordinator panel and confirm Telegram and Discord both receive it.

## Troubleshooting
- 401 on webhook: secret in the Pine input doesn't match `WEBHOOK_SECRET`.
- Alert stored but analysis blocked: read the Shield bar (kill switch, entry window, daily stop, drawdown).
- No reports: check `DAILY_REPORT_TIME` (16:05 New York, weekdays) and that the bot token/webhook are set; reports are sent once per day.
- Run the backend tests with `cd tvclaude/backend && python -m pytest -q`.
