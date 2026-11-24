---
name: nq-risk
description: Size an NQ/MNQ futures trade to a fixed dollar risk and check it against the 1:2 minimum and the prop-firm limits. Use when the user gives an entry and stop (or asks "how many contracts", "what's my size", "is this 2R", "where are my targets") for NQ, MNQ, NQ1!, MNQ1! or Nasdaq futures.
---

# NQ/MNQ risk check

Every number comes from the script, never from mental math. Paper trading and analysis only: never place, change or suggest a live order.

## The user's defaults
- Risk per trade: **$250** (1% of a $25,000 MyFundedFutures Rapid 25K evaluation)
- Minimum **2R** to TP1; targets laddered at **2R / 3R / 5R**
- NQ = $20 per point, MNQ = $2 per point; whole NQ first, then MNQ
- Daily stop: −$500 or 2 full losses. Drawdown: $1,000 end-of-day trailing

Don't assume a different balance or contract spec. If the user states other values, use theirs.

## Steps
1. Get **side**, **entry** and **stop**. If either price is missing, ask; never guess a price.
2. Ask for the **next level** in the trade's direction (resistance for a long, support for a short) if it isn't given. Without it, the room test can't run: say so.
3. Run:
   ```bash
   python3 "${CLAUDE_SKILL_DIR}/scripts/nq_size.py" --side long --entry 21000 --stop 20990 --next-level 21035
   ```
   Options: `--risk 250`, `--min-r 2`, `--targets "2,3,5"`, `--micro-only`, `--drawdown-left <$>`, `--json`.
   Pass `--drawdown-left` whenever the user mentions how much drawdown is left.
4. Report the verdict first (**OK** or **BLOCK** and why), then size, max loss, the three targets and the exit legs. If the script prints a note (for example 1 NQ can't be split into thirds), pass it on.
5. On BLOCK, don't hunt for a tighter stop to make it fit. The stop belongs where the setup is wrong. Suggest waiting for a better location or skipping.

## Teach while you check
End with one short "why" the user can reuse, for example: "Room under 2R means the next level will likely stall price before TP1. That's the most common way a good-looking setup loses."
