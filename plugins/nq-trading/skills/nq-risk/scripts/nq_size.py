#!/usr/bin/env python3
"""NQ / MNQ trade calculator: size, 2R room test, ladder targets. Standard library only.

Examples:
  python3 nq_size.py --side long --entry 21000 --stop 20990 --next-level 21018
  python3 nq_size.py --side short --entry 21000 --stop 21012.5 --risk 250 --micro-only
  python3 nq_size.py --side long --entry 21000 --stop 20975 --drawdown-left 600

Rules (Zaytovnn's plan): risk $250 per trade, whole NQ ($20/pt) first then MNQ ($2/pt), never round up,
TP1 must be >= 2R from entry, targets at 2R / 3R / 5R, exits in thirds.
"""
import argparse
import json
import math
import sys

NQ, MNQ, TICK = 20.0, 2.0, 0.25


def size(stop_pts: float, risk: float, micro_only: bool) -> dict:
    n_nq = 0 if micro_only else math.floor(risk / (stop_pts * NQ))
    left = risk - n_nq * stop_pts * NQ
    n_mnq = math.floor(left / (stop_pts * MNQ))
    dpp = n_nq * NQ + n_mnq * MNQ
    return {"NQ": n_nq, "MNQ": n_mnq, "dollars_per_point": dpp, "max_loss": round(stop_pts * dpp, 2)}


def legs(n_nq: int, n_mnq: int) -> list[dict]:
    """Split the REAL contracts into TP1/TP2/TP3 as evenly as possible by $ per point (whole contracts only;
    a single NQ can't be split). Biggest contracts first, each to the lightest leg; ties go to TP1."""
    out = [{"NQ": 0, "MNQ": 0, "dpp": 0.0} for _ in range(3)]
    for kind, pv, n in (("NQ", NQ, n_nq), ("MNQ", MNQ, n_mnq)):
        for _ in range(n):
            leg = min(range(3), key=lambda i: (out[i]["dpp"], i))
            out[leg][kind] += 1
            out[leg]["dpp"] += pv
    return out


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="NQ/MNQ size, 2R room test and targets")
    p.add_argument("--side", choices=["long", "short"], required=True)
    p.add_argument("--entry", type=float, required=True)
    p.add_argument("--stop", type=float, required=True)
    p.add_argument("--risk", type=float, default=250.0, help="$ risk per trade (default 250)")
    p.add_argument("--next-level", type=float, help="nearest resistance (long) or support (short)")
    p.add_argument("--min-r", type=float, default=2.0)
    p.add_argument("--targets", default="2,3,5", help="target multiples of R")
    p.add_argument("--micro-only", action="store_true", help="size in MNQ only")
    p.add_argument("--drawdown-left", type=float, help="$ left before the account's drawdown floor")
    p.add_argument("--json", action="store_true")
    a = p.parse_args(argv)

    sign = 1 if a.side == "long" else -1
    r = (a.entry - a.stop) * sign
    out = {"side": a.side, "entry": a.entry, "stop": a.stop, "verdict": "OK", "reasons": []}
    if r <= 0:
        out.update(verdict="BLOCK", reasons=[f"stop {a.stop} is on the wrong side of entry {a.entry} for a {a.side}"])
        return report(out, a.json)
    out["stop_points"] = r
    s = size(r, a.risk, a.micro_only)
    out["size"] = s
    if s["NQ"] == 0 and s["MNQ"] == 0:
        out["verdict"] = "BLOCK"
        out["reasons"].append(f"a {r:g}-pt stop is too wide: 1 MNQ alone risks ${r * MNQ:,.2f} > ${a.risk:,.0f}")
    mults = [float(x) for x in a.targets.split(",")]
    out["targets"] = [{"r": m, "price": round(a.entry + sign * m * r, 2),
                       "profit_if_all_exit": round(m * r * s["dollars_per_point"], 2)} for m in mults]
    out["legs"] = dict(zip(["TP1", "TP2", "TP3"], legs(s["NQ"], s["MNQ"])))
    dpps = [l["dpp"] for l in out["legs"].values()]
    if s["NQ"] and max(dpps) > 1.5 * (sum(dpps) / 3):
        eq = int(s["dollars_per_point"] // MNQ)
        out["note"] = (f"{s['NQ']} NQ can't be split into even thirds. For an even 2R/3R/5R ladder use {eq} MNQ "
                       f"instead: same ${s['dollars_per_point']:g}/pt and same max loss, more commissions.")
    if a.next_level is not None:
        room = (a.next_level - a.entry) * sign / r
        out["room_r"] = round(room, 2)
        if room < a.min_r:
            out["verdict"] = "BLOCK"
            out["reasons"].append(f"next level {a.next_level:g} is only {room:.2f}R away (need {a.min_r:g}R): AVOID")
    if a.drawdown_left is not None and s["max_loss"] >= a.drawdown_left:
        out["verdict"] = "BLOCK"
        out["reasons"].append(f"a full loss (${s['max_loss']:,.2f}) would reach the drawdown floor "
                              f"(${a.drawdown_left:,.2f} left)")
    return report(out, a.json)


def report(out: dict, as_json: bool) -> int:
    if as_json:
        print(json.dumps(out, indent=2))
        return 0 if out["verdict"] == "OK" else 1
    print(f"{out['verdict']}: {out['side']} {out['entry']:g}, stop {out['stop']:g}")
    if "size" in out:
        s = out["size"]
        print(f"  stop {out['stop_points']:g} pts -> size {s['NQ']} NQ + {s['MNQ']} MNQ "
              f"(${s['dollars_per_point']:g}/pt), max loss ${s['max_loss']:,.2f}")
        print("  targets: " + " | ".join(f"{t['r']:g}R {t['price']:,.2f}" for t in out["targets"]))
        if s["NQ"] or s["MNQ"]:
            print("  exit legs: " + " | ".join(
                f"{k} " + (" + ".join(f"{l[c]} {c}" for c in ("NQ", "MNQ") if l[c]) or "none") + f" (${l['dpp']:g}/pt)"
                for k, l in out["legs"].items()))
        if out.get("note"):
            print(f"  note: {out['note']}")
    if "room_r" in out:
        print(f"  room to next level: {out['room_r']}R")
    for r in out["reasons"]:
        print(f"  - {r}")
    return 0 if out["verdict"] == "OK" else 1


if __name__ == "__main__":
    sys.exit(main())
