"use client";
import { useState } from "react";
import { api, API_BASE, fmt, getToken } from "@/lib/api";
import type { LabRun, ReportPreview } from "@/lib/types";
import { decisionClass, Panel } from "./bits";

export function LabPanel() {
  const [fee, setFee] = useState("");
  const [tz, setTz] = useState("America/New_York");
  const [run, setRun] = useState<LabRun | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function upload(file: File) {
    setErr(null); setBusy(true);
    try {
      const q = new URLSearchParams({ fee_per_contract: fee || "0", chart_tz: tz, filename: file.name });
      const res = await fetch(`${API_BASE}/api/lab/analyze?${q}`, {
        method: "POST", body: await file.text(),
        headers: { "Content-Type": "text/csv", Authorization: `Bearer ${getToken() ?? ""}` },
      });
      const body = await res.json();
      if (!res.ok) throw new Error(typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail));
      setRun(body as LabRun);
    } catch (e) { setErr((e as Error).message); setRun(null); }
    finally { setBusy(false); }
  }

  return (
    <Panel title="Lab · backtest check">
      <div className="body form">
        <div className="foot" style={{ marginTop: 0 }}>Strategy Tester → List of trades → Export, then upload the CSV. Every number comes from the file plus your fee.</div>
        <div className="row">
          <label>Fee per contract, round trip ($)<input className="num" inputMode="decimal" value={fee} onChange={(e) => setFee(e.target.value)} placeholder="e.g. 1.24" /></label>
          <label>Chart time zone<select value={tz} onChange={(e) => setTz(e.target.value)}>
            <option value="America/New_York">New York</option><option value="America/Chicago">Chicago (exchange)</option>
            <option value="UTC">UTC</option></select></label>
        </div>
        <label>Trade list CSV<input type="file" accept=".csv,text/csv" disabled={busy}
          onChange={(e) => { const f = e.target.files?.[0]; if (f) upload(f); e.target.value = ""; }} /></label>
        {err && <div className="err">{err}</div>}
        {run && (
          <div>
            <span className={decisionClass(run.verdict === "PASS" ? "APPROVED" : "WAIT")}>{run.verdict}</span>
            <span className="foot"> {run.filename} · {run.trades} trades</span>
            <table className="levels num"><tbody>
              <tr><td>Net after fees</td><td className={run.total_net >= 0 ? "pos" : "neg"}>{fmt(run.total_net)} (fees {fmt(run.total_fees)})</td></tr>
              <tr><td>Win rate</td><td>{run.win_rate ?? "—"}% · avg win {fmt(run.avg_win)} · avg loss {fmt(run.avg_loss)}</td></tr>
              <tr><td>Expectancy</td><td>{fmt(run.expectancy_usd)} per trade · {run.expectancy_r ?? "—"}R</td></tr>
              <tr><td>Max drawdown</td><td className="neg">{fmt(run.max_drawdown_usd)} · {run.max_consecutive_losses} losses in a row</td></tr>
              <tr><td>Worst day</td><td>{run.worst_day ? `${run.worst_day.day}: ${fmt(run.worst_day.net)}` : "—"} · {run.days_past_daily_limit} day(s) past the daily limit</td></tr>
              <tr><td>Drawdown rule</td><td className={run.drawdown_rule.breached ? "neg" : "pos"}>{run.drawdown_rule.breached ? `breached on ${run.drawdown_rule.breach_date}` : "never breached"}</td></tr>
            </tbody></table>
            {run.checks.map((c, i) => <div className="foot" key={i}>{c.pass ? "✓" : "✗"} {c.check}{c.value !== null ? ` (${c.value})` : ""}</div>)}
            {run.notes.map((n, i) => <div className="warn" key={i}>{n}</div>)}
          </div>)}
      </div>
    </Panel>
  );
}

export function CoordinatorPanel() {
  const [rep, setRep] = useState<ReportPreview | null>(null);
  const [msg, setMsg] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function preview(kind: "daily" | "weekly") {
    setBusy(true); setMsg(null);
    try { setRep(await api<ReportPreview>(`/api/coordinator/${kind}`)); }
    catch (e) { setMsg((e as Error).message); } finally { setBusy(false); }
  }
  async function send(kind: "daily" | "weekly") {
    if (!window.confirm(`Send the ${kind} report to Telegram and Discord now?`)) return;
    setBusy(true); setMsg(null);
    try {
      const r = await api<{ sent: boolean; reason?: string; channels?: Record<string, boolean>; text: string }>(
        `/api/coordinator/${kind}/send`, { method: "POST" });
      setRep({ day: "", text: r.text });
      setMsg(r.sent ? `Sent: ${Object.entries(r.channels ?? {}).map(([k, v]) => `${k} ${v ? "✓" : "✗"}`).join(", ") || "no channels configured"}` : r.reason ?? "not sent");
    } catch (e) { setMsg((e as Error).message); } finally { setBusy(false); }
  }

  return (
    <Panel title="Coordinator · reports">
      <div className="body form">
        <div className="foot" style={{ marginTop: 0 }}>Sent automatically after DAILY_REPORT_TIME on weekdays; the weekly review on WEEKLY_REVIEW_DAY. Each goes out once.</div>
        <div className="row">
          <button className="btn" disabled={busy} onClick={() => preview("daily")}>Preview today</button>
          <button className="btn" disabled={busy} onClick={() => preview("weekly")}>Preview week</button>
        </div>
        <div className="row">
          <button className="btn primary" disabled={busy} onClick={() => send("daily")}>Send daily now</button>
          <button className="btn primary" disabled={busy} onClick={() => send("weekly")}>Send weekly now</button>
        </div>
        {msg && <div className="foot">{msg}</div>}
        {rep && <pre className="report">{rep.text}</pre>}
      </div>
    </Panel>
  );
}
