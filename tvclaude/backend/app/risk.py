"""Risk manager: position sizing, stop distance, P&L and reward:risk. Deterministic code, no AI.

It never assumes an account balance or a contract specification. When an input is missing the
verdict is NEEDS_INPUT and no size is produced. When a rule is broken the verdict is REJECTED.

Contract specs come from CONTRACT_SPECS_FILE, e.g.:
    {"futures": {"NQ": {"multiplier": 20, "micro": "MNQ"}, "MNQ": {"multiplier": 2, "full": "NQ"}},
     "options": {"default_multiplier": 100},
     "crypto":  {"BTCUSDT": {"qty_step": 0.001}}}
"""
import json
import math
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .schemas import AssetClass

MONTHS = "FGHJKMNQUVXZ"


def futures_root(symbol: str) -> str:
    """CME_MINI:MNQ1! / MNQZ26 / MNQZ2026 -> MNQ."""
    s = symbol.upper().split(":")[-1]
    s = re.sub(r"\d+!$", "", s)
    m = re.fullmatch(rf"([A-Z0-9]+?)[{MONTHS}]\d{{1,4}}", s)
    return m.group(1) if m else s


@dataclass
class ContractSpecs:
    futures: dict[str, dict] = field(default_factory=dict)
    options: dict = field(default_factory=dict)
    crypto: dict[str, dict] = field(default_factory=dict)

    @classmethod
    def load(cls, path: str | None) -> "ContractSpecs":
        if not path:
            return cls()
        data = json.loads(Path(path).read_text())
        specs = cls(futures={k.upper(): v for k, v in data.get("futures", {}).items()},
                    options=data.get("options", {}),
                    crypto={k.upper(): v for k, v in data.get("crypto", {}).items()})
        for root, spec in specs.futures.items():
            if not isinstance(spec.get("multiplier"), (int, float)) or spec["multiplier"] <= 0:
                raise ValueError(f"contract spec for {root} needs a multiplier > 0")
        return specs


@dataclass
class RiskInputs:
    side: str                     # "long" | "short"
    entry: list[float]            # one price or a [low, high] zone
    stop_loss: float | None
    take_profit: list[float]
    asset_class: AssetClass
    symbol: str
    account_balance: float | None
    risk_percent: float
    multiplier: float | None = None   # explicit override (e.g. option contract data)
    option_premium: float | None = None
    drawdown_remaining: float | None = None   # account drawdown left after open risk; None = no limit


@dataclass
class RiskReport:
    verdict: str = "APPROVED"     # APPROVED | REJECTED | NEEDS_INPUT
    reasons: list[str] = field(default_factory=list)
    side: str | None = None
    entry_reference: float | None = None
    stop_loss: float | None = None
    stop_distance: float | None = None
    stop_distance_pct: float | None = None
    reward_risk: list[float] = field(default_factory=list)   # one per take-profit
    reward_risk_basis: str | None = None
    min_reward_risk: float | None = None
    risk_percent: float | None = None
    risk_amount: float | None = None
    multiplier: float | None = None
    quantity: float | None = None
    unit: str | None = None
    max_loss: float | None = None
    potential_profit: list[float] = field(default_factory=list)
    contracts: dict[str, int] = field(default_factory=dict)   # e.g. {"NQ": 1, "MNQ": 2}
    drawdown_remaining: float | None = None
    notes: list[str] = field(default_factory=list)

    def reject(self, why: str) -> None:
        self.verdict = "REJECTED"
        self.reasons.append(why)

    def need(self, why: str) -> None:
        if self.verdict != "REJECTED":
            self.verdict = "NEEDS_INPUT"
        self.reasons.append(why)

    def to_dict(self) -> dict:
        return asdict(self)


class RiskManager:
    def __init__(self, specs: ContractSpecs, allowed_risk_percents: list[float], min_reward_risk: float,
                 rr_target_index: int, max_target_r: float | None = None, allowed_roots: list[str] | None = None):
        self.specs = specs
        self.allowed = allowed_risk_percents
        self.min_rr = min_reward_risk
        self.rr_index = rr_target_index
        self.max_target_r = max_target_r
        self.allowed_roots = set(allowed_roots or [])

    def _root_ok(self, root: str) -> bool:
        return not self.allowed_roots or root in self.allowed_roots

    def _family(self, root: str) -> tuple[str | None, str | None]:
        """(full-size root, micro root) for a contract that has both, e.g. NQ/MNQ; only those allowed."""
        spec = self.specs.futures.get(root, {})
        full = root if spec.get("micro") else spec.get("full")
        micro = spec.get("micro") or (root if spec.get("full") else None)
        if not (full and micro and full in self.specs.futures and micro in self.specs.futures):
            return None, None
        return (full if self._root_ok(full) else None), (micro if self._root_ok(micro) else None)

    def _multiplier(self, inp: RiskInputs, rep: RiskReport) -> tuple[float | None, str]:
        if inp.asset_class == AssetClass.stock:
            return 1.0, "shares"
        if inp.asset_class == AssetClass.crypto:
            return 1.0, "units"
        if inp.multiplier:
            return inp.multiplier, "contracts"
        if inp.asset_class == AssetClass.future:
            spec = self.specs.futures.get(futures_root(inp.symbol))
            if not spec:
                rep.need(f"no contract spec for {futures_root(inp.symbol)}: add its multiplier to CONTRACT_SPECS_FILE")
                return None, "contracts"
            return float(spec["multiplier"]), "contracts"
        m = self.specs.options.get("default_multiplier")
        if not m:
            rep.need("option multiplier unknown: send option.multiplier or set options.default_multiplier")
            return None, "contracts"
        return float(m), "contracts"

    def _size_family(self, rep: RiskReport, inp: RiskInputs, entry: float, dist: float, risk_amount: float,
                     full: str | None, micro: str | None) -> RiskReport:
        """Whole full-size contracts first (lower fees), then micros for what's left. Same index, same price."""
        fm = float(self.specs.futures[full]["multiplier"]) if full else 0.0
        mm = float(self.specs.futures[micro]["multiplier"]) if micro else 0.0
        n_full = math.floor(risk_amount / (dist * fm)) if full else 0
        left = risk_amount - n_full * dist * fm
        n_micro = math.floor(left / (dist * mm)) if micro else 0
        if n_full == 0 and n_micro == 0:
            smallest = micro or full
            rep.reject(f"position size rounds to 0 contracts: one {smallest} with a {dist:g}-point stop risks "
                       f"{dist * (mm or fm):,.2f}, more than {risk_amount:,.2f}")
            return rep
        rep.contracts = {k: v for k, v in ((full, n_full), (micro, n_micro)) if k and v}
        base = micro or full
        base_mult = mm or fm
        dollars_per_point = n_full * fm + n_micro * mm
        rep.multiplier, rep.unit = base_mult, f"{base}-equivalent"
        rep.quantity = round(dollars_per_point / base_mult, 6)
        rep.max_loss = round(dist * dollars_per_point, 2)
        rep.potential_profit = [round(abs(t - entry) * dollars_per_point, 2) for t in inp.take_profit]
        rep.notes.append("size: " + " + ".join(f"{v} {k}" for k, v in rep.contracts.items()))
        return rep

    def evaluate(self, inp: RiskInputs) -> RiskReport:
        rep = RiskReport(side=inp.side, stop_loss=inp.stop_loss, risk_percent=inp.risk_percent,
                         min_reward_risk=self.min_rr)
        if inp.risk_percent not in self.allowed:
            rep.reject(f"risk {inp.risk_percent}% is not an allowed setting {self.allowed}")
        if not inp.entry:
            rep.reject("no entry price")
            return rep
        if inp.stop_loss is None:
            rep.reject("no stop-loss: risk is unbounded")
            return rep
        long = inp.side == "long"
        # Judge the worst fill in the zone: the top for a long, the bottom for a short.
        entry = max(inp.entry) if long else min(inp.entry)
        rep.entry_reference = entry
        dist = (entry - inp.stop_loss) if long else (inp.stop_loss - entry)
        if dist <= 0:
            rep.reject(f"stop-loss {inp.stop_loss:g} is on the wrong side of entry {entry:g} for a {inp.side}")
            return rep
        rep.stop_distance = round(dist, 10)
        rep.stop_distance_pct = round(dist / entry * 100, 4)

        if not inp.take_profit:
            rep.reject("no take-profit: reward:risk can't be judged")
        bad = [t for t in inp.take_profit if (t <= entry if long else t >= entry)]
        if bad:
            rep.reject(f"take-profit {bad} on the wrong side of entry {entry:g}")
        rep.reward_risk = [round(abs(t - entry) / dist, 2) for t in inp.take_profit]
        if inp.take_profit and not bad:
            i = min(self.rr_index, len(inp.take_profit))
            rep.reward_risk_basis = f"TP{i}"
            if rep.reward_risk[i - 1] + 1e-9 < self.min_rr:
                rep.reject(f"reward:risk 1:{rep.reward_risk[i - 1]:g} at TP{i} is below the minimum 1:{self.min_rr:g}")
            far = [r for r in rep.reward_risk if self.max_target_r and r > self.max_target_r + 1e-9]
            if far:
                rep.notes.append(f"targets at {far}R are beyond the {self.max_target_r:g}R plan: treat as a stretch")

        mult, unit = self._multiplier(inp, rep)
        rep.multiplier, rep.unit = mult, unit
        if inp.account_balance is None:
            rep.need("account balance not configured: set ACCOUNT_BALANCE or pass account_balance")
            return rep
        risk_amount = inp.account_balance * inp.risk_percent / 100
        rep.risk_amount = round(risk_amount, 2)
        if inp.drawdown_remaining is not None:
            rep.drawdown_remaining = round(inp.drawdown_remaining, 2)
            if risk_amount >= inp.drawdown_remaining:
                rep.reject(f"a full loss of {risk_amount:,.2f} would reach the account drawdown limit "
                           f"({inp.drawdown_remaining:,.2f} left after open risk)")
                return rep
        if mult is None:
            return rep

        if inp.asset_class == AssetClass.future:
            full, micro = self._family(futures_root(inp.symbol))
            if full or micro:
                return self._size_family(rep, inp, entry, dist, risk_amount, full, micro)

        if inp.asset_class == AssetClass.option and inp.option_premium:
            # Long option: the most you can lose is the premium paid. Size so the full premium fits.
            per_contract = inp.option_premium * mult
            qty = math.floor(risk_amount / per_contract)
            rep.notes.append("long option sized on full premium at risk; R:R is on the underlying levels")
            max_loss_per = per_contract
        else:
            per_unit = dist * mult
            raw = risk_amount / per_unit
            if inp.asset_class == AssetClass.crypto:
                step = float((self.specs.crypto.get(inp.symbol.upper()) or {}).get("qty_step", 1e-6))
                qty = math.floor(raw / step) * step
                qty = round(qty, 10)
            else:
                qty = math.floor(raw)
            max_loss_per = per_unit
            if inp.asset_class == AssetClass.option:
                rep.notes.append("no option premium supplied: sized on the underlying stop distance x multiplier")
        if qty <= 0:
            rep.reject(f"position size rounds to 0 {unit}: the stop is too wide for "
                       f"{inp.risk_percent}% risk on {inp.account_balance:,.2f}")
            return rep
        rep.quantity = qty
        rep.max_loss = round(qty * max_loss_per, 2)
        if inp.asset_class == AssetClass.option:
            rep.notes.append("option profit at targets depends on delta, IV and time decay: not estimated")
        else:
            rep.potential_profit = [round(qty * abs(t - entry) * mult, 2) for t in inp.take_profit]
        return rep
