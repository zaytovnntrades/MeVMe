"use client";
import { useState } from "react";
import { api, fmt } from "@/lib/api";
import type { RiskReport, Status } from "@/lib/types";
import { decisionClass, Panel } from "./bits";

export function RiskCalculator({ status }: { status: Status | null }) {
  const [f, setF] = useState({ side: "long", asset_class: "future", symbol: "NQ1!", entry: "", stop_loss: "",
    take_profit: "", account_balance: "", risk_percent: String(status?.risk.risk_percent ?? 1), multiplier: "" });
  const [rep, setRep] = useState<RiskReport | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const set = (k: keyof typeof f) => (e: React.ChangeEvent<HTMLInputElement | HTMLSelectElement>) => setF({ ...f, [k]: e.target.value });
  const allowed = status?.risk.allowed ?? [0.5, 1, 2];

  async function calc(e: React.FormEvent) {
    e.preventDefault();
    setErr(null);
    const num = (s: string) => (s.trim() === "" ? undefined : Number(s));
    try {
      setRep(await api<RiskReport>("/api/risk/calculate", {
        method: "POST",
        body: JSON.stringify({
          side: f.side, asset_class: f.asset_class, symbol: f.symbol || undefined, entry: num(f.entry),
          stop_loss: num(f.stop_loss), risk_percent: num(f.risk_percent), account_balance: num(f.account_balance),
          multiplier: num(f.multiplier),
          take_profit: f.take_profit.split(/[ ,]+/).filter(Boolean).map(Number),
        }),
      }));
    } catch (e2) { setErr((e2 as Error).message); setRep(null); }
  }

  return (
    <Panel title="Risk calculator" className="right">
      <form className="body form" onSubmit={calc}>
        <div className="row">
          <label>Side<select value={f.side} onChange={set("side")}><option value="long">Long</option><option value="short">Short</option></select></label>
          <label>Asset<select value={f.asset_class} onChange={set("asset_class")}>
            <option value="future">Futures</option><option value="stock">Stock</option>
            <option value="crypto">Crypto</option><option value="option">Option</option></select></label>
        </div>
        <label>Symbol<input value={f.symbol} onChange={set("symbol")} placeholder="MNQ1!, AAPL, BTCUSDT" /></label>
        <div className="row">
          <label>Entry<input className="num" inputMode="decimal" value={f.entry} onChange={set("entry")} required /></label>
          <label>Stop loss<input className="num" inputMode="decimal" value={f.stop_loss} onChange={set("stop_loss")} required /></label>
        </div>
        <label>Take profits (comma separated)<input className="num" value={f.take_profit} onChange={set("take_profit")} placeholder="2R, 3R, 5R prices e.g. 21020, 21030, 21050" /></label>
        <div className="row">
          <label>Account balance<input className="num" inputMode="decimal" value={f.account_balance} onChange={set("account_balance")}
            placeholder={status?.risk.account_balance_set ? "server setting" : "required"} /></label>
          <label>Risk %<select value={f.risk_percent} onChange={set("risk_percent")}>
            {allowed.map((p) => <option key={p} value={p}>{p}%</option>)}</select></label>
        </div>
        <label>Multiplier override (blank = contract spec)<input className="num" inputMode="decimal" value={f.multiplier} onChange={set("multiplier")} /></label>
        <button className="btn primary" type="submit">Calculate</button>
        {err && <div className="err">{err}</div>}
        {rep && (
          <div>
            <span className={decisionClass(rep.verdict)}>{rep.verdict}</span>
            <table className="levels num"><tbody>
              <tr><td>Size</td><td>{Object.keys(rep.contracts ?? {}).length ? Object.entries(rep.contracts).map(([k, v]) => `${v} ${k}`).join(" + ")
                : rep.quantity !== null ? `${fmt(rep.quantity, 6)} ${rep.unit}` : "—"}</td></tr>
              <tr><td>Risk amount</td><td>{fmt(rep.risk_amount)}</td></tr>
              <tr><td>Max loss</td><td className="neg">{fmt(rep.max_loss)}</td></tr>
              {rep.drawdown_remaining !== null && <tr><td>Drawdown left</td><td>{fmt(rep.drawdown_remaining)}</td></tr>}
              <tr><td>Stop distance</td><td>{fmt(rep.stop_distance, 4)} ({fmt(rep.stop_distance_pct, 3)}%)</td></tr>
              <tr><td>R per target</td><td>{rep.reward_risk.length ? rep.reward_risk.map((x) => `${x}R`).join(" / ") : "—"}</td></tr>
              <tr><td>Profit at targets</td><td className="pos">{rep.potential_profit.length ? rep.potential_profit.map((x) => fmt(x)).join(" / ") : "—"}</td></tr>
            </tbody></table>
            {[...rep.reasons, ...rep.notes].map((x, i) => <div className="foot" key={i}>• {x}</div>)}
          </div>
        )}
      </form>
    </Panel>
  );
}
