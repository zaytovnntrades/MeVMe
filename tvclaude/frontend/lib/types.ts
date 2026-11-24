export type Decision = "LONG_SETUP" | "SHORT_SETUP" | "WAIT_FOR_CONFIRMATION" | "WAIT" | "AVOID";

export interface Alert {
  id: number; received_at: string; ticker: string; asset_class: string; timeframe: string;
  strategy: string; price: number | null; signal: string | null; status: string;
  payload?: Record<string, unknown>;
}

export interface ClaudeResult {
  symbol: string; asset_class: string; timeframe: string; strategy: string;
  market_bias: "BULLISH" | "BEARISH" | "NEUTRAL"; setup: string;
  bullish_conditions: string[]; bearish_conditions: string[];
  entry_zone: number[]; stop_loss: number | null; take_profit: number[]; risk_reward: string;
  setup_quality: string; confidence: string; confirmation: string[]; invalidation: string;
  reasons_to_avoid: string[]; data_used: string[]; data_requests: string[]; decision: Decision; reasoning: string;
}

export interface RiskReport {
  verdict: "APPROVED" | "REJECTED" | "NEEDS_INPUT"; reasons: string[]; side: string | null;
  entry_reference: number | null; stop_loss: number | null; stop_distance: number | null;
  stop_distance_pct: number | null; reward_risk: number[]; reward_risk_basis: string | null;
  min_reward_risk: number | null; risk_percent: number | null; risk_amount: number | null;
  multiplier: number | null; quantity: number | null; unit: string | null; max_loss: number | null;
  potential_profit: number[]; contracts: Record<string, number>; drawdown_remaining: number | null; notes: string[];
}

export interface Account {
  starting_balance: number | null; drawdown_limit: number | null; mode: string; realized_pnl: number;
  equity: number | null; peak_equity: number | null; open_risk: number; drawdown_used: number;
  remaining: number | null; breached: boolean;
}

export interface Analysis {
  id: number; alert_id: number; created_at: string; model: string | null; decision: Decision;
  claude_decision: Decision | null; market_bias: string | null; confidence: string | null;
  setup_quality: string | null; result: ClaudeResult | null; risk: RiskReport | null; warnings: string[];
  error_kind: string | null; error: string | null; latency_ms: number | null;
  input_tokens: number | null; output_tokens: number | null; alert?: Alert;
}

export interface JournalEntry {
  id: number; created_at: string; analysis_id: number | null; symbol: string; strategy: string | null;
  side: "long" | "short"; entry: number; stop_loss: number; take_profit: number[]; quantity: number | null;
  multiplier: number; status: "planned" | "open" | "closed" | "skipped"; exit_price: number | null;
  pnl: number | null; r_multiple: number | null; closed_at: string | null; notes: string | null;
}

export interface Status {
  mode: string; broker_execution: string; database: string; redis: string;
  anthropic: { model: string; effort: string; api_key_set: boolean }; telegram: string;
  queue_depth: number; workers_running: number; account: Account;
  instruments: { asset_classes: string[] | "all"; futures_roots: string[] | "all" };
  risk: { account_balance_set: boolean; risk_per_trade: number | null; risk_percent: number; allowed: number[];
    min_reward_risk: number; rr_target: string; target_ladder: number[]; max_target_r: number | null };
}

export interface Performance {
  days: number; note: string;
  by_strategy: Record<string, {
    analyses: number; decisions: Record<string, number>; avg_latency_ms: number | null;
    journal: { closed_trades?: number; wins?: number; win_rate?: number | null; avg_r?: number | null; total_pnl?: number };
  }>;
}
