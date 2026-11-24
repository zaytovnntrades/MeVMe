"use client";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { AnalysisView } from "@/components/AnalysisView";
import { DecisionChip, Panel, timeAgo } from "@/components/bits";
import { RiskCalculator } from "@/components/RiskCalculator";
import { History, Journal, PerformancePanel } from "@/components/Tables";
import { api, ApiError, fmt, getToken, setToken, wsUrl } from "@/lib/api";
import type { Alert, Analysis, JournalEntry, Performance, Status } from "@/lib/types";

type WsState = "connecting" | "live" | "down";

function TokenGate({ onSave, error }: { onSave: (t: string) => void; error: string | null }) {
  const [t, setT] = useState("");
  return (
    <main className="gate">
      <section className="panel">
        <header><h2>TV × Claude Analyst</h2><span className="badge paper">PAPER</span></header>
        <form className="body form" onSubmit={(e) => { e.preventDefault(); onSave(t.trim()); }}>
          <p>Enter the dashboard token (<code>DASHBOARD_TOKEN</code> on the server). It's stored only in this browser. The Anthropic API key never leaves the server.</p>
          <label>Dashboard token<input type="password" value={t} onChange={(e) => setT(e.target.value)} autoFocus /></label>
          {error && <div className="err">{error}</div>}
          <button className="btn primary" type="submit" disabled={t.length < 16}>Connect</button>
        </form>
      </section>
    </main>
  );
}

function Pill({ label, state }: { label: string; state: "ok" | "bad" | "warn" | "off" }) {
  return <span className="pill"><span className={`dot ${state === "off" ? "" : `is-${state}`}`} />{label}</span>;
}

export default function Page() {
  const [ready, setReady] = useState(false);
  const [authed, setAuthed] = useState(false);
  const [authErr, setAuthErr] = useState<string | null>(null);
  const [status, setStatus] = useState<Status | null>(null);
  const [alerts, setAlerts] = useState<Alert[]>([]);
  const [analyses, setAnalyses] = useState<Analysis[]>([]);
  const [journal, setJournal] = useState<JournalEntry[]>([]);
  const [perf, setPerf] = useState<Performance | null>(null);
  const [selected, setSelected] = useState<number | null>(null);
  const [fresh, setFresh] = useState<Set<number>>(new Set());
  const [ws, setWs] = useState<WsState>("connecting");
  const [apiDown, setApiDown] = useState(false);
  const retry = useRef(0);

  const loadAll = useCallback(async () => {
    try {
      const [s, al, an, j, p] = await Promise.all([
        api<Status>("/api/status"), api<Alert[]>("/api/alerts?limit=100"), api<Analysis[]>("/api/analyses?limit=200"),
        api<JournalEntry[]>("/api/journal"), api<Performance>("/api/performance"),
      ]);
      setStatus(s); setAlerts(al); setAnalyses(an); setJournal(j); setPerf(p);
      setAuthed(true); setApiDown(false);
      setSelected((cur) => cur ?? al[0]?.id ?? null);
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) { setToken(null); setAuthed(false); setAuthErr("That token was rejected."); }
      else setApiDown(true);
    }
  }, []);

  useEffect(() => { setReady(true); if (getToken()) loadAll(); }, [loadAll]);

  // Live updates. Reconnects with backoff; refreshes stats when an analysis lands.
  useEffect(() => {
    if (!authed) return;
    let sock: WebSocket | null = null;
    let closed = false;
    let ping: ReturnType<typeof setInterval> | undefined;
    const connect = () => {
      setWs("connecting");
      sock = new WebSocket(wsUrl());
      sock.onopen = () => { retry.current = 0; setWs("live"); ping = setInterval(() => sock?.send("ping"), 25000); };
      sock.onmessage = (m) => {
        const { event, data } = JSON.parse(m.data);
        if (event === "alert_received") {
          setAlerts((xs) => [data as Alert, ...xs].slice(0, 200));
          setFresh((f) => new Set(f).add(data.id));
          setSelected((cur) => cur ?? data.id);
        } else if (event === "analysis_started") {
          setAlerts((xs) => xs.map((a) => (a.id === data.alert_id ? { ...a, status: "analyzing" } : a)));
        } else if (event === "analysis_completed" || event === "analysis_failed") {
          const an = data as Analysis;
          setAnalyses((xs) => [an, ...xs.filter((x) => x.id !== an.id)].slice(0, 300));
          setAlerts((xs) => xs.map((a) => (a.id === an.alert_id ? { ...a, status: event === "analysis_completed" ? "analyzed" : "failed" } : a)));
          api<Performance>("/api/performance").then(setPerf).catch(() => {});
          api<Status>("/api/status").then(setStatus).catch(() => {});
        }
      };
      sock.onclose = () => {
        clearInterval(ping);
        if (closed) return;
        setWs("down");
        retry.current = Math.min(retry.current + 1, 6);
        setTimeout(connect, 500 * 2 ** retry.current);
      };
    };
    connect();
    return () => { closed = true; clearInterval(ping); sock?.close(); };
  }, [authed]);

  const byAlert = useMemo(() => {
    const m = new Map<number, Analysis>();
    for (const a of [...analyses].reverse()) m.set(a.alert_id, a);   // latest analysis per alert
    return m;
  }, [analyses]);

  const current = selected !== null ? byAlert.get(selected) ?? null : null;
  const currentAlert = alerts.find((a) => a.id === selected);

  async function addToJournal(a: Analysis) {
    try {
      await api("/api/journal", { method: "POST", body: JSON.stringify({ analysis_id: a.id }) });
      setJournal(await api<JournalEntry[]>("/api/journal"));
      setStatus(await api<Status>("/api/status"));   // new entry reserves its risk in the drawdown buffer
    } catch (e) { window.alert((e as Error).message); }
  }

  if (!ready) return null;
  if (!authed) return <TokenGate error={authErr} onSave={(t) => { setToken(t); setAuthErr(null); loadAll(); }} />;

  const s = status;
  return (
    <>
      <div className="topbar">
        <div className="brand">TV × Claude <span>Trading Intelligence</span></div>
        <span className="badge paper">PAPER · ANALYSIS ONLY</span>
        <div className="pills">
          <Pill label={apiDown ? "API down" : "API"} state={apiDown ? "bad" : "ok"} />
          <Pill label={ws === "live" ? "Live" : ws === "connecting" ? "Connecting" : "Reconnecting"} state={ws === "live" ? "ok" : ws === "connecting" ? "warn" : "bad"} />
          <Pill label="Database" state={s?.database === "ok" ? "ok" : "bad"} />
          <Pill label={`Redis ${s?.redis ?? ""}`} state={s?.redis === "ok" ? "ok" : s?.redis === "disabled" ? "off" : "bad"} />
          <Pill label={`Claude ${s?.anthropic.model ?? ""}`} state={s?.anthropic.api_key_set ? "ok" : "warn"} />
          <Pill label={`Telegram ${s?.telegram ?? ""}`} state={s?.telegram === "configured" ? "ok" : "off"} />
          <Pill label={`Queue ${s?.queue_depth ?? 0} · workers ${s?.workers_running ?? 0}`} state={(s?.workers_running ?? 0) > 0 ? "ok" : "bad"} />
          <button className="linkbtn" onClick={() => { setToken(null); setAuthed(false); }}>Sign out</button>
        </div>
      </div>

      {s?.account.drawdown_limit != null && (() => {
        const a = s.account, left = a.remaining ?? 0, lim = a.drawdown_limit ?? 1;
        const pct = Math.max(0, Math.min(1, left / lim));
        const tone = a.breached || left <= (s.risk.risk_per_trade ?? 0) ? "var(--bear)" : pct < 0.5 ? "var(--wait)" : "var(--bull)";
        return (
          <div className="ddbar">
            <span>Drawdown buffer <strong className="num" style={{ color: tone }}>{fmt(left)}</strong> of <span className="num">{fmt(lim)}</span> ({a.mode})</span>
            <span className="ddtrack"><span style={{ width: `${pct * 100}%`, background: tone }} /></span>
            <span className="num">P&L {fmt(a.realized_pnl)} · open risk {fmt(a.open_risk)} · {fmt(s.risk.risk_per_trade)}/trade
              {" · "}{left > 0 && s.risk.risk_per_trade ? `${Math.floor(left / s.risk.risk_per_trade)} full losses left` : "no new trades"}</span>
            <span className="num">{Array.isArray(s.instruments.futures_roots) ? s.instruments.futures_roots.join(" / ") : "all"} · TP1 ≥ {s.risk.min_reward_risk}R{s.risk.target_ladder.length ? ` · ladder ${s.risk.target_ladder.join("/")}R` : ""}</span>
          </div>);
      })()}

      {s && !s.risk.account_balance_set &&
        <div style={{ padding: "8px 16px 0" }}><div className="warn">ACCOUNT_BALANCE isn't set on the server, so the risk manager can't size positions (it never assumes a balance).</div></div>}

      <main className="grid">
        <Panel title={`Live alerts · ${alerts.length}`}>
          {alerts.length === 0 ? <div className="empty">Waiting for TradingView alerts on <code>/webhook/tradingview</code>.</div> : (
            <ul className="feed scroll">{alerts.map((a) => {
              const an = byAlert.get(a.id);
              return (
                <li key={a.id} className={`${selected === a.id ? "sel" : ""} ${fresh.has(a.id) ? "new" : ""}`} onClick={() => setSelected(a.id)}>
                  <span className="sym">{a.ticker} <span className="meta">{a.timeframe} · {a.strategy}</span></span>
                  <span>{an ? <DecisionChip d={an.decision} /> : <span className="chip info">{a.status.toUpperCase()}</span>}</span>
                  <span className="meta num">{a.asset_class} · {a.signal ?? "alert"} · {fmt(a.price, 4)}</span>
                  <span className="meta">{timeAgo(a.received_at)}</span>
                </li>);
            })}</ul>)}
        </Panel>

        <AnalysisView analysis={current} pending={!!currentAlert && !current} onJournal={addToJournal} />
        <RiskCalculator status={s} />

        <div className="span-all two">
          <Journal entries={journal} onChange={async () => { setJournal(await api<JournalEntry[]>("/api/journal")); api<Status>("/api/status").then(setStatus).catch(() => {}); }} />
          <PerformancePanel perf={perf} />
        </div>
        <div className="span-all">
          <History analyses={analyses} onSelect={(a) => setSelected(a.alert_id)} />
        </div>
      </main>
    </>
  );
}
