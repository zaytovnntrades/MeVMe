"use client";
import { useState } from "react";
import { api, fmt } from "@/lib/api";
import type { Analysis, JournalEntry, Performance } from "@/lib/types";
import { DecisionChip, Panel, timeAgo } from "./bits";

export function Journal({ entries, onChange }: { entries: JournalEntry[]; onChange: () => void }) {
  const [exitPx, setExitPx] = useState<Record<number, string>>({});
  const [err, setErr] = useState<string | null>(null);

  async function update(id: number, body: object) {
    setErr(null);
    try { await api(`/api/journal/${id}`, { method: "PATCH", body: JSON.stringify(body) }); onChange(); }
    catch (e) { setErr((e as Error).message); }
  }

  return (
    <Panel title="Paper trade journal">
      {err && <div className="body"><div className="err">{err}</div></div>}
      {entries.length === 0 ? <div className="empty">No journal entries yet. Add one from an analysis.</div> : (
        <div className="tablewrap scroll"><table className="data num">
          <thead><tr><th>Date</th><th>Symbol</th><th>Side</th><th>Entry</th><th>Stop</th><th>Targets</th><th>Qty</th><th>Status</th><th>Exit</th><th>R</th><th>P&L</th><th></th></tr></thead>
          <tbody>{entries.map((j) => (
            <tr key={j.id}>
              <td>{new Date(j.created_at).toLocaleDateString()}</td><td>{j.symbol}</td>
              <td className={j.side === "long" ? "pos" : "neg"}>{j.side}</td>
              <td>{fmt(j.entry, 4)}</td><td>{fmt(j.stop_loss, 4)}</td><td>{j.take_profit.map((t) => fmt(t, 4)).join(" / ")}</td>
              <td>{fmt(j.quantity, 6)}</td><td>{j.status}</td><td>{fmt(j.exit_price, 4)}</td>
              <td className={(j.r_multiple ?? 0) >= 0 ? "pos" : "neg"}>{j.r_multiple === null ? "—" : `${j.r_multiple}R`}</td>
              <td className={(j.pnl ?? 0) >= 0 ? "pos" : "neg"}>{fmt(j.pnl)}</td>
              <td>{j.status !== "closed" && j.status !== "skipped" && (
                <span style={{ display: "inline-flex", gap: 4 }}>
                  {j.status === "planned" && <button className="btn small" onClick={() => update(j.id, { status: "open" })}>Open</button>}
                  <input style={{ width: 90 }} placeholder="exit px" value={exitPx[j.id] ?? ""}
                    onChange={(e) => setExitPx({ ...exitPx, [j.id]: e.target.value })} />
                  <button className="btn small" onClick={() => update(j.id, { status: "closed", exit_price: Number(exitPx[j.id]) })}
                    disabled={!exitPx[j.id]}>Close</button>
                  <button className="btn small" onClick={() => update(j.id, { status: "skipped" })}>Skip</button>
                </span>)}</td>
            </tr>))}</tbody>
        </table></div>)}
    </Panel>
  );
}

export function PerformancePanel({ perf }: { perf: Performance | null }) {
  const rows = perf ? Object.entries(perf.by_strategy) : [];
  return (
    <Panel title={`Strategy performance${perf ? ` · ${perf.days}d` : ""}`}>
      {rows.length === 0 ? <div className="empty">No analyses yet.</div> : (
        <div className="tablewrap"><table className="data num">
          <thead><tr><th>Strategy</th><th>Analyses</th><th>Long</th><th>Short</th><th>Wait</th><th>Avoid</th><th>Closed</th><th>Win rate</th><th>Avg R</th><th>P&L</th></tr></thead>
          <tbody>{rows.map(([name, s]) => {
            const d = s.decisions;
            return (
              <tr key={name}><td>{name}</td><td>{s.analyses}</td><td className="pos">{d.LONG_SETUP ?? 0}</td>
                <td className="neg">{d.SHORT_SETUP ?? 0}</td><td>{(d.WAIT ?? 0) + (d.WAIT_FOR_CONFIRMATION ?? 0)}</td>
                <td>{d.AVOID ?? 0}</td><td>{s.journal.closed_trades ?? 0}</td>
                <td>{s.journal.win_rate == null ? "—" : `${Math.round(s.journal.win_rate * 100)}%`}</td>
                <td>{s.journal.avg_r == null ? "—" : `${s.journal.avg_r}R`}</td>
                <td className={(s.journal.total_pnl ?? 0) >= 0 ? "pos" : "neg"}>{fmt(s.journal.total_pnl ?? 0)}</td></tr>);
          })}</tbody>
        </table></div>)}
      {perf && <div className="body foot">{perf.note}</div>}
    </Panel>
  );
}

export function History({ analyses, onSelect }: { analyses: Analysis[]; onSelect: (a: Analysis) => void }) {
  return (
    <Panel title="Analysis history">
      {analyses.length === 0 ? <div className="empty">No analyses yet.</div> : (
        <div className="tablewrap scroll"><table className="data num">
          <thead><tr><th>When</th><th>Symbol</th><th>TF</th><th>Strategy</th><th>Decision</th><th>Bias</th><th>Conf.</th><th>R:R</th><th>Risk</th><th>Latency</th></tr></thead>
          <tbody>{analyses.map((a) => (
            <tr key={a.id} className="click" onClick={() => onSelect(a)}>
              <td>{timeAgo(a.created_at)}</td><td>{a.alert?.ticker}</td><td>{a.alert?.timeframe}</td><td>{a.alert?.strategy}</td>
              <td><DecisionChip d={a.decision} /></td><td>{a.market_bias ?? "—"}</td><td>{a.confidence ?? "—"}</td>
              <td>{a.result?.risk_reward ?? "—"}</td><td>{a.risk?.verdict ?? (a.error ? "error" : "—")}</td>
              <td>{a.latency_ms ? `${(a.latency_ms / 1000).toFixed(1)}s` : "—"}</td>
            </tr>))}</tbody>
        </table></div>)}
    </Panel>
  );
}
