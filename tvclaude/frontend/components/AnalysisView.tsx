"use client";
import { fmt } from "@/lib/api";
import type { Analysis } from "@/lib/types";
import { DecisionChip, decisionClass, Panel } from "./bits";

function List({ title, items, tone = "" }: { title: string; items: string[] | undefined; tone?: string }) {
  if (!items || items.length === 0) return null;
  return (
    <div className={`list ${tone}`}>
      <h4>{title}</h4>
      <ul>{items.map((x, i) => <li key={i}>{x}</li>)}</ul>
    </div>
  );
}

export function AnalysisView({ analysis, pending, onJournal }:
  { analysis: Analysis | null; pending: boolean; onJournal: (a: Analysis) => void }) {
  if (!analysis) {
    return (
      <Panel title="Claude analysis">
        <div className="empty">{pending ? "Claude is analysing this alert…" : "Select an alert to see its analysis."}</div>
      </Panel>
    );
  }
  const r = analysis.result;
  const risk = analysis.risk;
  const overridden = analysis.claude_decision && analysis.claude_decision !== analysis.decision;

  return (
    <Panel title="Claude analysis" right={<span className="foot num">
      {analysis.model ?? "—"} · {analysis.latency_ms ? `${(analysis.latency_ms / 1000).toFixed(1)}s` : "—"}
      {analysis.output_tokens ? ` · ${analysis.input_tokens}/${analysis.output_tokens} tok` : ""}</span>}>
      <div className="body">
        <div className="hero">
          <h3>{r?.symbol ?? analysis.alert?.ticker}</h3>
          <span className="sub">{r?.timeframe ?? analysis.alert?.timeframe} · {r?.strategy ?? analysis.alert?.strategy} · {r?.asset_class ?? analysis.alert?.asset_class}</span>
          <DecisionChip d={analysis.decision} />
          {r && <span className={decisionClass(r.market_bias)}>{r.market_bias}</span>}
        </div>

        {analysis.error && <div className="err">Analysis failed ({analysis.error_kind}): {analysis.error}. Decision defaults to WAIT.</div>}
        {overridden && <div className="warn">Claude said {analysis.claude_decision}; the system changed it to {analysis.decision}.</div>}

        {r && <>
          <div className="kpis">
            <div className="kpi"><div className="k">Setup</div><div className="v">{r.setup}</div></div>
            <div className="kpi"><div className="k">Quality</div><div className="v">{r.setup_quality}</div></div>
            <div className="kpi"><div className="k">Confidence</div><div className="v">{r.confidence}</div></div>
            <div className="kpi"><div className="k">Reward : risk</div><div className="v num">{r.risk_reward}</div></div>
          </div>

          <table className="levels num">
            <tbody>
              <tr><td>Entry zone</td><td>{r.entry_zone.length ? r.entry_zone.map((x) => fmt(x, 4)).join(" – ") : "—"}</td></tr>
              <tr className="sl"><td>Stop loss</td><td>{fmt(r.stop_loss, 4)}</td></tr>
              {[0, 1, 2].map((i) => (
                <tr className="tp" key={i}><td>Take profit {i + 1}</td>
                  <td>{fmt(r.take_profit[i], 4)}{risk?.reward_risk[i] !== undefined ? `  (${risk.reward_risk[i]}R)` : ""}</td></tr>
              ))}
            </tbody>
          </table>

          <div className="cols">
            <List title="Bullish conditions" items={r.bullish_conditions} tone="bull" />
            <List title="Bearish conditions" items={r.bearish_conditions} tone="bear" />
            <List title="Confirmation signals" items={r.confirmation} />
            <List title="Reasons to avoid" items={r.reasons_to_avoid} tone="bear" />
          </div>
          <div className="reason"><strong>Invalidation:</strong> {r.invalidation}<br /><br />{r.reasoning}</div>
          <div className="cols" style={{ marginTop: 12 }}>
            <List title="Data used" items={r.data_used} />
            <List title="Data Claude asked for" items={r.data_requests} />
          </div>
        </>}

        {analysis.warnings.map((w, i) => <div className="warn" key={i}>{w}</div>)}

        {risk && (
          <div style={{ marginTop: 12 }}>
            <div className="hero" style={{ gap: 8 }}>
              <h4 style={{ margin: 0, fontSize: 11, letterSpacing: "0.08em", color: "var(--muted)" }}>RISK MANAGER</h4>
              <span className={decisionClass(risk.verdict)}>{risk.verdict}</span>
            </div>
            <table className="levels num">
              <tbody>
                <tr><td>Size</td><td>{risk.verdict === "REJECTED" ? "— (rejected: no position)"
                  : Object.keys(risk.contracts ?? {}).length ? Object.entries(risk.contracts).map(([k, v]) => `${v} ${k}`).join(" + ")
                  : risk.quantity !== null ? `${fmt(risk.quantity, 6)} ${risk.unit}` : "— (needs input)"}</td></tr>
                <tr><td>Risk</td><td>{risk.risk_percent}% = {fmt(risk.risk_amount)} · max loss {fmt(risk.max_loss)}</td></tr>
                <tr><td>Stop distance</td><td>{fmt(risk.stop_distance, 4)} ({fmt(risk.stop_distance_pct, 3)}%) from worst fill {fmt(risk.entry_reference, 4)}</td></tr>
                <tr><td>Minimum</td><td>1:{risk.min_reward_risk} at {risk.reward_risk_basis ?? "—"}</td></tr>
                {risk.drawdown_remaining !== null && <tr><td>Drawdown left</td><td>{fmt(risk.drawdown_remaining)} before this trade</td></tr>}
                {risk.verdict !== "REJECTED" && risk.potential_profit.length > 0 &&
                  <tr className="tp"><td>Profit at targets</td><td>{risk.potential_profit.map((p) => fmt(p)).join(" / ")}</td></tr>}
              </tbody>
            </table>
            {[...risk.reasons, ...risk.notes].map((x, i) => <div className="foot" key={i}>• {x}</div>)}
          </div>
        )}

        {r && risk && risk.verdict !== "REJECTED" && r.entry_zone.length > 0 && (
          <div style={{ marginTop: 12 }}>
            <button className="btn primary" onClick={() => onJournal(analysis)}>Add to paper journal</button>
          </div>
        )}
        <div className="foot">Analysis only. Confidence reflects the supplied evidence, not a probability of profit. Not financial advice. No orders are placed.</div>
      </div>
    </Panel>
  );
}
