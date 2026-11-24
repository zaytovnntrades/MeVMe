# MeVMe plugins

This repo is a personal Claude Code plugin marketplace.

```
/plugin marketplace add zaytovnntrades/MeVMe
/plugin install nq-trading@mevme
```

The repo is private, so Claude Code uses your own git access to fetch it.

## nq-trading

| Skill | What it does |
|---|---|
| `nq-risk` | Sizes an NQ/MNQ trade to $250, checks 2R room and drawdown, gives 2R/3R/5R targets and exit legs (`scripts/nq_size.py`) |
| `nq-replay-coach` | Runs a guided TradingView Bar Replay session, grades each trade A/B/C, ends with one drill |
| `nq-pine` | Installs and troubleshoots the NQ Setups Pine strategy (a copy lives in `assets/`) |
| `tvclaude-setup` | Deploys tvclaude: `.env`, Docker, TradingView webhook, Telegram and Discord reports |

Analysis and paper trading only. Nothing here places orders.

`skills/nq-pine/assets/nq_setups_strategy.pine` must match `tvclaude/tradingview/nq_setups_strategy.pine`; a backend test checks this. After editing the strategy, copy it over.
