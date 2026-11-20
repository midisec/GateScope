export type Session = {
  user: { id: string; tenant_id: string; username: string; role: string };
  expires_at: string;
  mfa_required: boolean;
  auth_strength: string;
};

export type Instrument = {
  id: string;
  symbol: string;
  display_name: string;
  market: string;
  settle: string;
  status: string;
};

export type Bar = {
  is_final?: boolean;
  close_time?: string;
  available_at?: string;
  revision?: number;
  open_time: string;
  open: string;
  high: string;
  low: string;
  close: string;
  volume: string;
  quality_flags: string[];
};

export type IndicatorCatalogItem = {
  indicator_id: string;
  version: string;
  outputs: string[];
  parameters: Record<string, {
    type: "integer" | "number";
    default: number;
    minimum: number;
    maximum: number;
  }>;
  warmup_bars: number;
};

export type IndicatorPoint = {
  instance_id: string;
  indicator_id: string;
  indicator_version: string;
  open_time: string;
  available_at: string;
  bar_revision: number;
  ready: boolean;
  values: Record<string, number | null>;
};

export type PatternCatalogItem = {
  pattern_id: string;
  name: string;
  group: string;
  direction: "bullish" | "bearish" | "neutral";
  experimental: boolean;
};

export type VersionedResource = {
  id: string;
  name: string;
  spec: Record<string, unknown>;
  etag: number;
  created_at: string;
  updated_at: string;
};

export type Operation = {
  id: string;
  status: "QUEUED" | "RUNNING" | "SUCCEEDED" | "FAILED";
  progress: number;
  safe_error_code: string | null;
};

export type Page<T> = { items: T[]; next_cursor: string | null; has_more: boolean };

export type TelegramConnection = {
  id: string;
  telegram_user_id: string;
  telegram_chat_id: string;
  telegram_username: string | null;
  status: string;
  etag: number;
};

export type NotificationChannel = {
  id: string;
  alias: string;
  status: string;
  connection_id: string;
  etag: number;
};

export type NotificationRoute = {
  quiet_hours?: { timezone: string; start: string; end: string } | null;
  strategy_ids?: string[];
  exchange_account_ids?: string[];
  environments?: string[];
  digest_mode?: boolean;
  bypass_quiet_rules?: Array<{ event_type: string; environments: string[] }>;
  id: string;
  name: string;
  status: string;
  etag: number;
  channel_id: string;
  event_types: string[];
  symbols: string[];
  minimum_severity: string;
  max_per_minute: number;
  dedupe_seconds: number;
  enabled: boolean;
};

export type Watchlist = {
  id: string;
  name: string;
  etag: number;
  items: Array<{ instrument_id: string; symbol: string; default_timeframe: string }>;
};

export type AlertRule = {
  symbol?: string;
  id: string;
  name: string;
  status: string;
  etag: number;
  current_version: number;
  version: {
    condition: {
      instrument_id: string;
      timeframe: string;
      pattern_ids: string[];
      match_mode: "ANY" | "ALL";
      minimum_confidence: number;
      confirmed_only: boolean;
      coincidence_bars: number;
      cooldown_seconds: number;
    };
    routing: { channel_ids: string[] };
  };
};

export type McpToken = {
  id: string;
  client_id: string;
  label: string;
  token_hint: string;
  scopes: string[];
  status: "ACTIVE" | "EXPIRED" | "REVOKED";
  expires_at: string;
  last_used_at: string | null;
  revoked_at: string | null;
  created_at: string;
};

export type McpClient = {
  id: string;
  name: string;
  status: "ACTIVE" | "REVOKED";
  etag: number;
  tokens: McpToken[];
};

export type ExchangeAccount = {
  id: string;
  alias: string;
  venue: "GATE";
  environment: "TESTNET";
  market_type: "FUTURES_USDT";
  settle_asset: "USDT";
  status: "PENDING_VALIDATION" | "READ_VALIDATED" | "TESTNET_READY" | "TESTNET_BUSY" | "EXECUTION_FROZEN" | "VALIDATION_FAILED" | "DISABLED";
  trading_ready: boolean;
  credential_version: number;
  credential_fingerprint: string | null;
  capabilities: {
    account_read?: boolean;
    futures_usdt_read?: boolean;
    ip_whitelist_configured?: boolean;
    trade_permission?: "UNVERIFIED";
    withdrawal_permission?: "UNVERIFIED";
  };
  warning_codes: string[];
  last_safe_error_code: string | null;
  testnet_armed_at: string | null;
  testnet_armed_by: string | null;
  execution_freeze_code: string | null;
  validation_requested_at: string;
  validated_at: string | null;
  etag: number;
};

export type TestnetDrill = {
  id: string;
  public_drill_id: string;
  paper_public_trade_id: string;
  exchange_account_id: string;
  exchange_alias: string;
  instrument_id: string;
  contract: string;
  side: "LONG" | "SHORT";
  environment: "TESTNET";
  exact_terms: ExactPaperTerms;
  exact_terms_digest: string;
  state: "QUEUED" | "PROCESSING" | "RECONCILIATION_PENDING" | "ENTRY_UNFILLED" | "ENTRY_UNKNOWN" | "PROTECTED" | "CLOSED_PROTECTED" | "CLOSED_FAIL_SAFE" | "PREFLIGHT_REJECTED" | "MANUAL_INTERVENTION_REQUIRED";
  safe_code: string | null;
  entry_order_id: string | null;
  filled_contracts: number;
  stop_order_id: string | null;
  take_profit_order_id: string | null;
  fail_safe_order_id: string | null;
  frozen: boolean;
  version: number;
  created_at: string;
  updated_at: string;
};

export type StrategyExecutionCertification = {
  id: string;
  strategy_version_ref: string;
  environment: "TESTNET";
  decision: "CERTIFY";
  evidence_tier: "LIVE_ELIGIBLE";
  certification_policy_version: string;
  source_artifact_digest: string;
  certification_digest: string;
  approved_at: string;
  expires_at: string;
};

export type PaperAccount = {
  id: string;
  name: string;
  base_currency: string;
  equity: string;
  realized_pnl: string;
  risk_fraction: string;
  max_notional_fraction: string;
  status: string;
  etag: number;
};

export type SignalEvidence = {
  id: string;
  instrument_id: string;
  symbol: string;
  timeframe: string;
  strategy_version_ref: string;
  policy_version: string;
  evidence_tier: "RESEARCH" | "PAPER";
  eligibility_status: "ABSTAIN" | "PAPER_ELIGIBLE";
  side: "LONG" | "SHORT";
  entry_reference: string;
  stop_price: string;
  take_profit_price: string;
  time_stop_bars: number;
  metrics: {
    win_probability_lcb?: number;
    expected_net_return_lcb?: number;
    independent_blocks?: number;
  };
  reason_codes: string[];
  decision_at: string;
  data_watermark: string;
  evidence_digest: string;
};

export type ExactPaperTerms = {
  side: "LONG" | "SHORT";
  quantity: string;
  quantity_unit: "BASE";
  entry_reference: string;
  stop_price: string;
  take_profit_price: string;
  time_stop_bars: number;
  risk_budget: string;
  worst_case_loss: string;
  max_notional: string;
  same_bar_policy: "STOP_FIRST";
  digest: string;
};

export type ExecutionDecision = {
  id: string;
  decision: "ABSTAIN" | "REJECT" | "ELIGIBLE";
  environment: "PAPER";
  authorization_mode: "NONE" | "MANUAL";
  exact_terms: ExactPaperTerms | Record<string, never>;
  runtime_gates: Array<{ gate_code: string; result: string; reason_code: string | null }>;
  expires_at: string;
};

export type PaperProposal = {
  id: string;
  signal_evidence_snapshot_id: string;
  symbol: string;
  timeframe: string;
  environment: "PAPER";
  exact_terms: ExactPaperTerms;
  expires_at: string;
  status: string;
  version: number;
};

export type PaperTrade = {
  id: string;
  public_trade_id: string;
  signal_evidence_snapshot_id: string;
  symbol: string;
  timeframe: string;
  environment: "PAPER";
  side: "LONG" | "SHORT";
  exact_terms: ExactPaperTerms;
  state: "OPEN" | "CLOSED";
  close_reason: string | null;
  entry_fill_price: string;
  exit_fill_price: string | null;
  quantity: string;
  fees: string;
  net_pnl: string | null;
  evaluated_bars: number;
  last_evaluated_available_at: string;
  protection_status: string;
};

export type PaperChart = {
  original_bars: Bar[];
  progress_bars: Bar[];
  evidence_boundary: string;
  data_watermark: string;
  overlays: {
    entry_fill: string;
    stop: string;
    take_profit: string;
    exit_fill: string | null;
  };
};
