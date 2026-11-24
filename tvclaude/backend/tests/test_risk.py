"""Risk manager: sizing, stop distance, P&L, reward:risk, and refusing to assume missing inputs."""
import json

import pytest

from app.risk import ContractSpecs, RiskInputs, RiskManager, futures_root
from app.schemas import AssetClass

SPECS = ContractSpecs(futures={"MNQ": {"multiplier": 2}, "ES": {"multiplier": 50}},
                      options={"default_multiplier": 100}, crypto={"BTCUSDT": {"qty_step": 0.001}})


def rm(min_rr=2.0, idx=2):
    return RiskManager(SPECS, [0.5, 1.0, 2.0], min_rr, idx)


def inp(**kw):
    base = dict(side="long", entry=[100.0], stop_loss=98.0, take_profit=[102.0, 104.0, 106.0],
                asset_class=AssetClass.stock, symbol="AAPL", account_balance=10_000, risk_percent=1.0)
    base.update(kw)
    return RiskInputs(**base)


class TestSizing:
    def test_stock_shares(self):
        r = rm().evaluate(inp())
        assert r.verdict == "APPROVED"
        assert (r.risk_amount, r.quantity, r.unit, r.max_loss) == (100.0, 50, "shares", 100.0)
        assert r.reward_risk == [1.0, 2.0, 3.0] and r.reward_risk_basis == "TP2"
        assert r.potential_profit == [100.0, 200.0, 300.0]

    @pytest.mark.parametrize("pct,qty", [(0.5, 25), (1.0, 50), (2.0, 100)])
    def test_configurable_risk_percent(self, pct, qty):
        assert rm().evaluate(inp(risk_percent=pct)).quantity == qty

    def test_disallowed_risk_percent_rejected(self):
        r = rm().evaluate(inp(risk_percent=5.0))
        assert r.verdict == "REJECTED" and "not an allowed setting" in r.reasons[0]

    def test_futures_contracts_use_spec_multiplier(self):
        # 10-pt stop x $2/pt = $20 per MNQ contract; $100 risk -> 5 contracts
        r = rm().evaluate(inp(asset_class=AssetClass.future, symbol="CME_MINI:MNQ1!", entry=[21000],
                              stop_loss=20990, take_profit=[21010, 21020]))
        assert (r.multiplier, r.quantity, r.unit, r.max_loss) == (2.0, 5, "contracts", 100.0)

    def test_crypto_rounds_down_to_qty_step(self):
        r = rm().evaluate(inp(asset_class=AssetClass.crypto, symbol="BTCUSDT", entry=[97250], stop_loss=96250,
                              take_profit=[99250]))
        assert r.quantity == 0.1 and r.max_loss == 100.0

    def test_long_option_sized_on_premium(self):
        r = rm().evaluate(inp(asset_class=AssetClass.option, symbol="SPY", entry=[598], stop_loss=594,
                              take_profit=[606], multiplier=100, option_premium=0.40))
        assert r.quantity == 2 and r.max_loss == 80.0      # 2 x $40 premium
        assert r.potential_profit == [] and any("not estimated" in n for n in r.notes)

    def test_short_side(self):
        r = rm().evaluate(inp(side="short", entry=[100], stop_loss=102, take_profit=[98, 96]))
        assert r.verdict == "APPROVED" and r.reward_risk == [1.0, 2.0] and r.quantity == 50

    def test_worst_edge_of_zone(self):
        r = rm().evaluate(inp(entry=[99.0, 100.0], stop_loss=98.0))
        assert r.entry_reference == 100.0          # top of the zone for a long
        r = rm().evaluate(inp(side="short", entry=[100.0, 101.0], stop_loss=102.0, take_profit=[97, 94]))
        assert r.entry_reference == 100.0          # bottom of the zone for a short

    def test_stop_too_wide_rounds_to_zero(self):
        r = rm().evaluate(inp(asset_class=AssetClass.future, symbol="ES1!", entry=[6000], stop_loss=5990,
                              take_profit=[6020], account_balance=1_000))   # $500/contract vs $10 risk
        assert r.verdict == "REJECTED" and "rounds to 0" in r.reasons[-1]


class TestRules:
    def test_reward_risk_below_minimum(self):
        r = rm().evaluate(inp(take_profit=[101.0, 103.0]))
        assert r.verdict == "REJECTED" and "1:1.5 at TP2" in r.reasons[0]

    def test_basis_falls_back_to_last_target(self):
        r = rm().evaluate(inp(take_profit=[104.0]))
        assert r.verdict == "APPROVED" and r.reward_risk_basis == "TP1"

    def test_spec_example_payload_is_rejected_at_tp2(self):
        # The brief's example: entry 20000-20010, SL 19980, TP 20030/20050/20080.
        # Worst long fill is 20010 -> risk 30; TP2 is 40 away -> 1:1.33.
        r = rm().evaluate(inp(asset_class=AssetClass.future, symbol="MNQ1!", entry=[20000, 20010],
                              stop_loss=19980, take_profit=[20030, 20050, 20080]))
        assert r.verdict == "REJECTED" and r.reward_risk == [0.67, 1.33, 2.33]

    @pytest.mark.parametrize("kw,msg", [
        ({"stop_loss": 101.0}, "wrong side"),
        ({"stop_loss": None}, "unbounded"),
        ({"take_profit": []}, "no take-profit"),
        ({"take_profit": [99.0, 104.0]}, "wrong side of entry"),
        ({"entry": []}, "no entry"),
    ])
    def test_rejections(self, kw, msg):
        r = rm().evaluate(inp(**kw))
        assert r.verdict == "REJECTED" and any(msg in x for x in r.reasons)


class TestNeverAssume:
    def test_missing_balance_needs_input(self):
        r = rm().evaluate(inp(account_balance=None))
        assert r.verdict == "NEEDS_INPUT" and r.quantity is None and "balance" in r.reasons[0]
        assert r.reward_risk == [1.0, 2.0, 3.0]      # R:R is still reported

    def test_missing_contract_spec_needs_input(self):
        r = rm().evaluate(inp(asset_class=AssetClass.future, symbol="CL1!", entry=[70], stop_loss=69, take_profit=[72]))
        assert r.verdict == "NEEDS_INPUT" and "no contract spec for CL" in r.reasons[0] and r.quantity is None

    def test_missing_option_multiplier_needs_input(self):
        r = RiskManager(ContractSpecs(), [1.0], 2, 2).evaluate(
            inp(asset_class=AssetClass.option, symbol="SPY", entry=[598], stop_loss=594, take_profit=[606]))
        assert r.verdict == "NEEDS_INPUT" and "multiplier unknown" in r.reasons[0]

    def test_rejection_beats_needs_input(self):
        r = rm().evaluate(inp(account_balance=None, take_profit=[101.0]))
        assert r.verdict == "REJECTED"


class TestSpecs:
    def test_futures_root(self):
        for s, root in (("CME_MINI:MNQ1!", "MNQ"), ("MNQZ26", "MNQ"), ("ESH2027", "ES"), ("M2KZ26", "M2K")):
            assert futures_root(s) == root

    def test_bad_spec_file(self, tmp_path):
        p = tmp_path / "s.json"
        p.write_text(json.dumps({"futures": {"MNQ": {"multiplier": 0}}}))
        with pytest.raises(ValueError, match="multiplier"):
            ContractSpecs.load(str(p))

    def test_no_file_means_no_specs(self):
        assert ContractSpecs.load(None).futures == {}
