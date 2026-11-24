"""The nq-pine skill ships its own copy of the strategy; it must match the source."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


def test_pine_copy_matches_source():
    src = ROOT / "tvclaude/tradingview/nq_setups_strategy.pine"
    copy = ROOT / "plugins/nq-trading/skills/nq-pine/assets/nq_setups_strategy.pine"
    assert copy.read_text() == src.read_text(), "copy tvclaude/tradingview/nq_setups_strategy.pine into the nq-pine skill"
