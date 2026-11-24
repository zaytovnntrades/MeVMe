---
name: nq-pine
description: Install, configure and troubleshoot the "NQ Setups, Long + Short (1:2 ladder)" Pine Script strategy for NQ/MNQ on TradingView, and read its Strategy Tester results. Use when the user asks for the NQ pine script, wants to paste it into TradingView, change its inputs, fix a Pine error, set up its alert, or understand its backtest.
---

# NQ Setups Pine strategy

The script is at `${CLAUDE_SKILL_DIR}/assets/nq_setups_strategy.pine` (Pine v6). Read it before answering questions about it; don't describe rules from memory. It places no orders: it backtests and, optionally, sends alerts for analysis.

## Install (give these steps)
1. TradingView → open an **MNQ1!** or **NQ1!** chart (or a specific contract such as NQZ2026) on **5 minutes**.
2. Pine Editor → New → delete the template → paste the whole file → **Save** → **Add to chart**.
3. Settings → Properties: set the real **commission per contract** from the user's Tradovate/MFF fee schedule. It's 0 in the file on purpose; never invent a fee.
4. Settings → Inputs worth checking:
   - **Directions**: Both / Long only / Short only
   - **Risk per trade ($)**: 250
   - **Min room (R)** 2, **TP1/2/3** 2/3/5, **Max full losses per day** 2
   - **Entry window** 0935-1130, **Flatten** 1555-1600 (New York)
   - **Webhook secret**: only if sending alerts to tvclaude, and only typed into TradingView, never into chat or a repo
5. Optional alert: Alert → condition **NQ Setups**, **"alert() function calls only"**, webhook URL `https://<their host>/webhook/tradingview`.

When the user wants the code to paste, print the file contents in one block exactly as stored.

## Reading the Strategy Tester
- Look at **trade count** first: under 100 trades proves nothing.
- Then net profit **after commission**, profit factor, max drawdown and largest losing streak.
- Compare max drawdown to the $1,000 trailing limit and the worst day to −$500.
- Export **List of trades** (CSV) and upload it to the tvclaude dashboard's **Lab** for a PASS / NOT YET verdict (100+ trades, +0.3R after fees, no breach).
- Backtests are history, not a forecast. Say that once, plainly.

## Common fixes
- "Could not find function or reference": they pasted only part of the file, or an older Pine version is selected. Paste the whole file; the first line must be `//@version=6`.
- No trades: chart isn't 5m, the symbol isn't NQ/MNQ, Directions is set to one side, or the session inputs don't match New York time.
- Size looks wrong: the script uses the symbol's own point value; on MNQ it sizes in MNQ, on NQ in NQ.
- Don't load the bullish and bearish scripts together with this one: each counts its own losses, so the daily stop could be hit twice.

When changing the script, keep the shared daily stop, the 2R room test and the paper-only behaviour intact, and show the user a diff.
