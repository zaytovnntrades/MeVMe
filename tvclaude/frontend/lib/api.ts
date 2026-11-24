// The browser only ever holds the dashboard token. The Anthropic key stays on the server.
export const API_BASE = (process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000").replace(/\/$/, "");
const KEY = "tvclaude.dashboardToken";

export function getToken(): string | null {
  try { return window.localStorage.getItem(KEY); } catch { return null; }
}
export function setToken(token: string | null): void {
  try { token ? window.localStorage.setItem(KEY, token) : window.localStorage.removeItem(KEY); } catch { /* storage blocked */ }
}

export class ApiError extends Error {
  constructor(public status: number, message: string) { super(message); }
}

export async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  const token = getToken();
  const res = await fetch(`${API_BASE}${path}`, {
    ...init,
    headers: { "Content-Type": "application/json", ...(token ? { Authorization: `Bearer ${token}` } : {}), ...init.headers },
    cache: "no-store",
  });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
    } catch { /* not JSON */ }
    throw new ApiError(res.status, detail);
  }
  return res.json() as Promise<T>;
}

export function wsUrl(): string {
  const base = API_BASE.replace(/^http/, "ws");
  return `${base}/ws?token=${encodeURIComponent(getToken() || "")}`;
}

export const fmt = (n: number | null | undefined, digits = 2): string =>
  n === null || n === undefined ? "—" : n.toLocaleString(undefined, { maximumFractionDigits: digits });
