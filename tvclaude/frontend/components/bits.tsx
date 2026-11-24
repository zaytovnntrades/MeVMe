import type { Decision } from "@/lib/types";

export function decisionClass(d: string | null | undefined): string {
  if (d === "LONG_SETUP" || d === "BULLISH" || d === "APPROVED") return "chip bull";
  if (d === "SHORT_SETUP" || d === "BEARISH" || d === "REJECTED" || d === "AVOID") return "chip bear";
  if (d === "WAIT" || d === "WAIT_FOR_CONFIRMATION" || d === "NEEDS_INPUT") return "chip wait";
  return "chip";
}

const LABEL: Record<Decision, string> = {
  LONG_SETUP: "LONG SETUP", SHORT_SETUP: "SHORT SETUP", WAIT_FOR_CONFIRMATION: "WAIT · CONFIRM",
  WAIT: "WAIT", AVOID: "AVOID",
};

export function DecisionChip({ d }: { d: Decision | string | null | undefined }) {
  return <span className={decisionClass(d)}>{d ? LABEL[d as Decision] ?? d : "—"}</span>;
}

export function Panel({ title, right, children, className = "" }:
  { title: string; right?: React.ReactNode; children: React.ReactNode; className?: string }) {
  return (
    <section className={`panel ${className}`}>
      <header><h2>{title}</h2>{right}</header>
      {children}
    </section>
  );
}

export function timeAgo(iso: string): string {
  const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (s < 60) return `${Math.floor(s)}s ago`;
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return new Date(iso).toLocaleDateString();
}
