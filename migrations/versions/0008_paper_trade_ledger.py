"""Immutable signal evidence, PAPER proposals and trade event ledger.

Revision ID: 0008_paper_trade_ledger
Revises: 0007_notification_route_policy
"""

from __future__ import annotations

from alembic import op

revision = "0008_paper_trade_ledger"
down_revision = "0007_notification_route_policy"
branch_labels = None
depends_on = None

PRIVATE_TABLES = [
    "paper_accounts",
    "signal_evidence_snapshots",
    "signal_bar_manifest_items",
    "signal_gate_results",
    "execution_decisions",
    "trade_proposals",
    "proposal_status_events",
    "proposal_status_current",
    "approval_revalidation_snapshots",
    "approvals",
    "trades",
    "trade_bar_observations",
    "trade_events",
    "trade_state_current",
]

IMMUTABLE_TABLES = [
    "signal_evidence_snapshots",
    "signal_bar_manifest_items",
    "signal_gate_results",
    "execution_decisions",
    "trade_proposals",
    "proposal_status_events",
    "approval_revalidation_snapshots",
    "approvals",
    "trades",
    "trade_bar_observations",
    "trade_events",
]


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute(
        """
        CREATE TABLE paper_accounts (
          id uuid PRIMARY KEY,
          tenant_id uuid NOT NULL REFERENCES tenants(id),
          owner_user_id uuid NOT NULL REFERENCES users(id),
          name text NOT NULL CHECK (length(name) BETWEEN 1 AND 120),
          base_currency text NOT NULL CHECK (base_currency ~ '^[A-Z0-9]{2,12}$'),
          initial_equity numeric(38,18) NOT NULL CHECK (initial_equity > 0),
          realized_pnl numeric(38,18) NOT NULL DEFAULT 0,
          risk_fraction numeric(12,10) NOT NULL CHECK (risk_fraction > 0 AND risk_fraction <= 0.005),
          max_notional_fraction numeric(12,10) NOT NULL CHECK (max_notional_fraction > 0 AND max_notional_fraction <= 0.10),
          fee_bps_per_side numeric(10,4) NOT NULL CHECK (fee_bps_per_side >= 0 AND fee_bps_per_side <= 100),
          market_slippage_bps numeric(10,4) NOT NULL CHECK (market_slippage_bps >= 0 AND market_slippage_bps <= 100),
          max_entry_drift_bps numeric(10,4) NOT NULL CHECK (max_entry_drift_bps > 0 AND max_entry_drift_bps <= 100),
          status text NOT NULL CHECK (status IN ('ACTIVE','PAUSED')),
          etag bigint NOT NULL DEFAULT 1 CHECK (etag > 0),
          created_at timestamptz NOT NULL,
          updated_at timestamptz NOT NULL,
          UNIQUE (tenant_id, owner_user_id, name)
        );

        CREATE TABLE signal_evidence_snapshots (
          id uuid PRIMARY KEY,
          tenant_id uuid NOT NULL REFERENCES tenants(id),
          created_by uuid NOT NULL REFERENCES users(id),
          source_alert_event_id uuid REFERENCES alert_event_facts(id),
          instrument_id uuid NOT NULL REFERENCES instruments(id),
          timeframe text NOT NULL CHECK (timeframe IN ('1m','5m','15m','30m','1h','4h','8h','1d')),
          strategy_version_ref text NOT NULL CHECK (length(strategy_version_ref) BETWEEN 1 AND 120),
          policy_version text NOT NULL CHECK (length(policy_version) BETWEEN 1 AND 80),
          evidence_tier text NOT NULL CHECK (evidence_tier IN ('RESEARCH','PAPER')),
          eligibility_status text NOT NULL CHECK (eligibility_status IN ('ABSTAIN','PAPER_ELIGIBLE')),
          side text NOT NULL CHECK (side IN ('LONG','SHORT')),
          entry_reference numeric(38,18) NOT NULL CHECK (entry_reference > 0),
          stop_price numeric(38,18) NOT NULL CHECK (stop_price > 0),
          take_profit_price numeric(38,18) NOT NULL CHECK (take_profit_price > 0),
          time_stop_bars integer NOT NULL CHECK (time_stop_bars BETWEEN 1 AND 10000),
          decision_at timestamptz NOT NULL,
          data_watermark timestamptz NOT NULL,
          metrics_json jsonb NOT NULL,
          assumptions_json jsonb NOT NULL,
          reason_codes text[] NOT NULL,
          indicator_snapshot_json jsonb NOT NULL,
          chart_spec_json jsonb NOT NULL,
          evidence_digest text NOT NULL CHECK (evidence_digest ~ '^[0-9a-f]{64}$'),
          created_at timestamptz NOT NULL,
          UNIQUE (tenant_id, evidence_digest),
          CHECK ((evidence_tier='PAPER') = (eligibility_status='PAPER_ELIGIBLE')),
          CHECK (
            (side='LONG' AND stop_price < entry_reference AND entry_reference < take_profit_price)
            OR (side='SHORT' AND take_profit_price < entry_reference AND entry_reference < stop_price)
          ),
          CHECK (data_watermark <= decision_at),
          CHECK (jsonb_typeof(metrics_json)='object'),
          CHECK (jsonb_typeof(assumptions_json)='array'),
          CHECK (jsonb_typeof(indicator_snapshot_json)='object'),
          CHECK (jsonb_typeof(chart_spec_json)='object')
        );
        CREATE INDEX signal_evidence_lookup_idx
          ON signal_evidence_snapshots(tenant_id, decision_at DESC, id DESC);

        CREATE TABLE signal_bar_manifest_items (
          id uuid PRIMARY KEY,
          tenant_id uuid NOT NULL REFERENCES tenants(id),
          signal_evidence_snapshot_id uuid NOT NULL REFERENCES signal_evidence_snapshots(id),
          sequence integer NOT NULL CHECK (sequence >= 0),
          venue text NOT NULL,
          market text NOT NULL,
          instrument_id uuid NOT NULL REFERENCES instruments(id),
          timeframe text NOT NULL,
          open_time timestamptz NOT NULL,
          revision integer NOT NULL CHECK (revision > 0),
          payload_hash text NOT NULL,
          available_at timestamptz NOT NULL,
          UNIQUE (tenant_id, signal_evidence_snapshot_id, sequence),
          UNIQUE (tenant_id, signal_evidence_snapshot_id, venue, market, instrument_id, timeframe, open_time, revision)
        );

        CREATE TABLE signal_gate_results (
          id uuid PRIMARY KEY,
          tenant_id uuid NOT NULL REFERENCES tenants(id),
          signal_evidence_snapshot_id uuid NOT NULL REFERENCES signal_evidence_snapshots(id),
          gate_code text NOT NULL CHECK (gate_code ~ '^[A-Z][A-Z0-9_]{2,79}$'),
          result text NOT NULL CHECK (result IN ('PASS','FAIL','NOT_APPLICABLE','INSUFFICIENT')),
          value_json jsonb NOT NULL,
          reason_code text,
          method_version text NOT NULL CHECK (length(method_version) BETWEEN 1 AND 80),
          created_at timestamptz NOT NULL,
          UNIQUE (tenant_id, signal_evidence_snapshot_id, gate_code)
        );

        CREATE TABLE execution_decisions (
          id uuid PRIMARY KEY,
          tenant_id uuid NOT NULL REFERENCES tenants(id),
          owner_user_id uuid NOT NULL REFERENCES users(id),
          signal_evidence_snapshot_id uuid NOT NULL REFERENCES signal_evidence_snapshots(id),
          signal_evidence_digest text NOT NULL CHECK (signal_evidence_digest ~ '^[0-9a-f]{64}$'),
          paper_account_id uuid NOT NULL REFERENCES paper_accounts(id),
          decision text NOT NULL CHECK (decision IN ('ABSTAIN','REJECT','ELIGIBLE')),
          environment text NOT NULL DEFAULT 'PAPER' CHECK (environment='PAPER'),
          authorization_mode text NOT NULL CHECK (authorization_mode IN ('NONE','MANUAL')),
          exact_terms_json jsonb NOT NULL,
          exact_terms_digest text NOT NULL CHECK (exact_terms_digest ~ '^[0-9a-f]{64}$'),
          runtime_gates_json jsonb NOT NULL,
          risk_snapshot_json jsonb NOT NULL,
          request_id_hash text NOT NULL CHECK (request_id_hash ~ '^[0-9a-f]{64}$'),
          expires_at timestamptz NOT NULL,
          created_at timestamptz NOT NULL,
          UNIQUE (tenant_id, owner_user_id, request_id_hash),
          CHECK ((decision='ELIGIBLE') = (authorization_mode='MANUAL')),
          CHECK (jsonb_typeof(exact_terms_json)='object'),
          CHECK (jsonb_typeof(runtime_gates_json)='array'),
          CHECK (jsonb_typeof(risk_snapshot_json)='object'),
          CHECK (expires_at > created_at)
        );
        CREATE INDEX execution_decisions_signal_idx
          ON execution_decisions(tenant_id, signal_evidence_snapshot_id, created_at DESC);

        CREATE TABLE trade_proposals (
          id uuid PRIMARY KEY,
          tenant_id uuid NOT NULL REFERENCES tenants(id),
          owner_user_id uuid NOT NULL REFERENCES users(id),
          execution_decision_id uuid NOT NULL REFERENCES execution_decisions(id),
          environment text NOT NULL DEFAULT 'PAPER' CHECK (environment='PAPER'),
          authorization_mode text NOT NULL DEFAULT 'MANUAL' CHECK (authorization_mode='MANUAL'),
          exact_terms_json jsonb NOT NULL CHECK (jsonb_typeof(exact_terms_json)='object'),
          exact_terms_digest text NOT NULL CHECK (exact_terms_digest ~ '^[0-9a-f]{64}$'),
          expires_at timestamptz NOT NULL,
          request_id_hash text NOT NULL CHECK (request_id_hash ~ '^[0-9a-f]{64}$'),
          proposal_digest text NOT NULL CHECK (proposal_digest ~ '^[0-9a-f]{64}$'),
          created_at timestamptz NOT NULL,
          UNIQUE (tenant_id, execution_decision_id),
          UNIQUE (tenant_id, owner_user_id, request_id_hash),
          UNIQUE (tenant_id, proposal_digest),
          CHECK (expires_at > created_at)
        );

        CREATE TABLE proposal_status_events (
          id uuid PRIMARY KEY,
          tenant_id uuid NOT NULL REFERENCES tenants(id),
          proposal_id uuid NOT NULL REFERENCES trade_proposals(id),
          sequence bigint NOT NULL CHECK (sequence > 0),
          event_type text NOT NULL CHECK (event_type IN (
            'CREATED','AUTHORIZED','CONSUMED','REJECTED','CANCELED','EXPIRED','REVALIDATION_REJECTED'
          )),
          resulting_status text NOT NULL CHECK (resulting_status IN (
            'AWAITING_CONFIRMATION','AUTHORIZED','CONSUMED','REJECTED','CANCELED','EXPIRED','REVALIDATION_REJECTED'
          )),
          actor_user_id uuid NOT NULL REFERENCES users(id),
          channel text NOT NULL CHECK (channel IN ('WEB','TELEGRAM','AUTO','SYSTEM')),
          payload_json jsonb NOT NULL CHECK (jsonb_typeof(payload_json)='object'),
          previous_digest text CHECK (previous_digest IS NULL OR previous_digest ~ '^[0-9a-f]{64}$'),
          event_digest text NOT NULL CHECK (event_digest ~ '^[0-9a-f]{64}$'),
          created_at timestamptz NOT NULL,
          UNIQUE (tenant_id, proposal_id, sequence),
          UNIQUE (tenant_id, event_digest)
        );

        CREATE TABLE proposal_status_current (
          proposal_id uuid PRIMARY KEY REFERENCES trade_proposals(id),
          tenant_id uuid NOT NULL REFERENCES tenants(id),
          status text NOT NULL CHECK (status IN (
            'AWAITING_CONFIRMATION','AUTHORIZED','CONSUMED','REJECTED','CANCELED','EXPIRED','REVALIDATION_REJECTED'
          )),
          version bigint NOT NULL CHECK (version > 0),
          last_event_digest text NOT NULL CHECK (last_event_digest ~ '^[0-9a-f]{64}$'),
          updated_at timestamptz NOT NULL
        );

        CREATE TABLE approval_revalidation_snapshots (
          id uuid PRIMARY KEY,
          tenant_id uuid NOT NULL REFERENCES tenants(id),
          proposal_id uuid NOT NULL REFERENCES trade_proposals(id),
          proposal_version bigint NOT NULL CHECK (proposal_version > 0),
          signal_evidence_snapshot_id uuid NOT NULL REFERENCES signal_evidence_snapshots(id),
          exact_terms_digest text NOT NULL CHECK (exact_terms_digest ~ '^[0-9a-f]{64}$'),
          market_bar_manifest_json jsonb NOT NULL CHECK (jsonb_typeof(market_bar_manifest_json)='object'),
          observed_price numeric(38,18) NOT NULL CHECK (observed_price > 0),
          entry_drift_bps numeric(18,8) NOT NULL CHECK (entry_drift_bps >= 0),
          gates_json jsonb NOT NULL CHECK (jsonb_typeof(gates_json)='array'),
          result text NOT NULL CHECK (result IN ('PASS','FAIL')),
          snapshot_digest text NOT NULL CHECK (snapshot_digest ~ '^[0-9a-f]{64}$'),
          created_at timestamptz NOT NULL,
          UNIQUE (tenant_id, snapshot_digest)
        );

        CREATE TABLE approvals (
          id uuid PRIMARY KEY,
          tenant_id uuid NOT NULL REFERENCES tenants(id),
          proposal_id uuid NOT NULL REFERENCES trade_proposals(id),
          proposal_version bigint NOT NULL CHECK (proposal_version > 0),
          approval_revalidation_snapshot_id uuid NOT NULL REFERENCES approval_revalidation_snapshots(id),
          actor_user_id uuid NOT NULL REFERENCES users(id),
          channel text NOT NULL CHECK (channel IN ('WEB','TELEGRAM','AUTO')),
          approval_outcome text NOT NULL CHECK (approval_outcome IN ('APPROVE','REJECT')),
          idempotency_hash text NOT NULL CHECK (idempotency_hash ~ '^[0-9a-f]{64}$'),
          request_digest text NOT NULL CHECK (request_digest ~ '^[0-9a-f]{64}$'),
          decided_at timestamptz NOT NULL,
          UNIQUE (tenant_id, actor_user_id, idempotency_hash)
        );
        CREATE UNIQUE INDEX approvals_one_approve_idx
          ON approvals(tenant_id, proposal_id) WHERE approval_outcome='APPROVE';

        CREATE TABLE trades (
          id uuid PRIMARY KEY,
          public_trade_id text NOT NULL CHECK (public_trade_id ~ '^paper_[0-9A-HJKMNP-TV-Z]{26}$'),
          tenant_id uuid NOT NULL REFERENCES tenants(id),
          owner_user_id uuid NOT NULL REFERENCES users(id),
          proposal_id uuid NOT NULL REFERENCES trade_proposals(id),
          approval_id uuid NOT NULL REFERENCES approvals(id),
          signal_evidence_snapshot_id uuid NOT NULL REFERENCES signal_evidence_snapshots(id),
          paper_account_id uuid NOT NULL REFERENCES paper_accounts(id),
          instrument_id uuid NOT NULL REFERENCES instruments(id),
          timeframe text NOT NULL,
          environment text NOT NULL DEFAULT 'PAPER' CHECK (environment='PAPER'),
          side text NOT NULL CHECK (side IN ('LONG','SHORT')),
          exact_terms_json jsonb NOT NULL CHECK (jsonb_typeof(exact_terms_json)='object'),
          exact_terms_digest text NOT NULL CHECK (exact_terms_digest ~ '^[0-9a-f]{64}$'),
          created_at timestamptz NOT NULL,
          UNIQUE (tenant_id, public_trade_id),
          UNIQUE (tenant_id, proposal_id)
        );

        CREATE TABLE trade_events (
          id uuid PRIMARY KEY,
          tenant_id uuid NOT NULL REFERENCES tenants(id),
          trade_id uuid NOT NULL REFERENCES trades(id),
          sequence bigint NOT NULL CHECK (sequence > 0),
          event_type text NOT NULL CHECK (event_type IN (
            'TRADE_CREATED','PAPER_ENTRY_FILLED','PAPER_PROTECTION_ACTIVE','PAPER_BAR_OBSERVED','PAPER_EXIT_FILLED'
          )),
          payload_json jsonb NOT NULL CHECK (jsonb_typeof(payload_json)='object'),
          occurred_at timestamptz NOT NULL,
          recorded_at timestamptz NOT NULL,
          previous_digest text CHECK (previous_digest IS NULL OR previous_digest ~ '^[0-9a-f]{64}$'),
          event_digest text NOT NULL CHECK (event_digest ~ '^[0-9a-f]{64}$'),
          UNIQUE (tenant_id, trade_id, sequence),
          UNIQUE (tenant_id, event_digest)
        );

        CREATE TABLE trade_bar_observations (
          id uuid PRIMARY KEY,
          tenant_id uuid NOT NULL REFERENCES tenants(id),
          trade_id uuid NOT NULL REFERENCES trades(id),
          sequence integer NOT NULL CHECK (sequence > 0),
          venue text NOT NULL,
          market text NOT NULL,
          instrument_id uuid NOT NULL REFERENCES instruments(id),
          timeframe text NOT NULL,
          open_time timestamptz NOT NULL,
          revision integer NOT NULL CHECK (revision > 0),
          open numeric(38,18) NOT NULL CHECK (open > 0),
          high numeric(38,18) NOT NULL CHECK (high > 0),
          low numeric(38,18) NOT NULL CHECK (low > 0),
          close numeric(38,18) NOT NULL CHECK (close > 0),
          volume numeric(38,18) NOT NULL CHECK (volume >= 0),
          available_at timestamptz NOT NULL,
          payload_hash text NOT NULL,
          quality_flags text[] NOT NULL,
          observed_at timestamptz NOT NULL,
          UNIQUE (tenant_id, trade_id, sequence),
          UNIQUE (tenant_id, trade_id, open_time),
          CHECK (high >= greatest(open, close, low)),
          CHECK (low <= least(open, close, high))
        );

        CREATE TABLE trade_state_current (
          trade_id uuid PRIMARY KEY REFERENCES trades(id),
          tenant_id uuid NOT NULL REFERENCES tenants(id),
          paper_account_id uuid NOT NULL REFERENCES paper_accounts(id),
          instrument_id uuid NOT NULL REFERENCES instruments(id),
          side text NOT NULL CHECK (side IN ('LONG','SHORT')),
          state text NOT NULL CHECK (state IN ('OPEN','CLOSED')),
          close_reason text CHECK (close_reason IS NULL OR close_reason IN ('TP_HIT','SL_HIT','TIME_STOP')),
          entry_fill_price numeric(38,18) NOT NULL CHECK (entry_fill_price > 0),
          exit_fill_price numeric(38,18) CHECK (exit_fill_price IS NULL OR exit_fill_price > 0),
          quantity numeric(38,18) NOT NULL CHECK (quantity > 0),
          gross_pnl numeric(38,18),
          fees numeric(38,18) NOT NULL CHECK (fees >= 0),
          net_pnl numeric(38,18),
          entry_available_at timestamptz NOT NULL,
          closed_at timestamptz,
          evaluated_bars integer NOT NULL DEFAULT 0 CHECK (evaluated_bars >= 0),
          last_evaluated_available_at timestamptz NOT NULL,
          protection_status text NOT NULL CHECK (protection_status IN ('SIMULATED_ACTIVE','COMPLETED')),
          version bigint NOT NULL CHECK (version > 0),
          last_event_digest text NOT NULL CHECK (last_event_digest ~ '^[0-9a-f]{64}$'),
          updated_at timestamptz NOT NULL,
          CHECK ((state='OPEN' AND close_reason IS NULL AND exit_fill_price IS NULL AND closed_at IS NULL)
              OR (state='CLOSED' AND close_reason IS NOT NULL AND exit_fill_price IS NOT NULL AND closed_at IS NOT NULL))
        );
        CREATE INDEX trades_lookup_idx ON trades(tenant_id, created_at DESC, id DESC);
        CREATE INDEX trade_state_open_idx ON trade_state_current(tenant_id, updated_at) WHERE state='OPEN';
        CREATE UNIQUE INDEX trade_state_one_open_position_idx
          ON trade_state_current(tenant_id,paper_account_id,instrument_id,side)
          WHERE state='OPEN';
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION gatescope_security.resolve_active_telegram_principal(
          requested_telegram_user_id bigint, requested_telegram_chat_id bigint
        ) RETURNS TABLE(tenant_id uuid, subject_user_id uuid, role text)
        LANGUAGE sql SECURITY DEFINER STABLE
        SET search_path = pg_catalog, public AS $$
          SELECT tc.tenant_id, tc.user_id, m.role
          FROM public.telegram_connections tc
          JOIN public.users u ON u.id=tc.user_id AND u.status='ACTIVE'
          JOIN public.memberships m
            ON m.tenant_id=tc.tenant_id AND m.user_id=tc.user_id AND m.status='ACTIVE'
          WHERE tc.telegram_user_id=requested_telegram_user_id
            AND tc.telegram_chat_id=requested_telegram_chat_id
            AND tc.status='ACTIVE'
          LIMIT 1
        $$;
        REVOKE ALL ON FUNCTION
          gatescope_security.resolve_active_telegram_principal(bigint,bigint) FROM PUBLIC;
        GRANT EXECUTE ON FUNCTION
          gatescope_security.resolve_active_telegram_principal(bigint,bigint) TO gatescope_api;

        CREATE OR REPLACE FUNCTION gatescope_security.paper_open_trade_due_tenants(
          requested_limit integer
        ) RETURNS TABLE(tenant_id uuid)
        LANGUAGE sql SECURITY DEFINER STABLE
        SET search_path = pg_catalog, public AS $$
          SELECT DISTINCT state.tenant_id
          FROM public.trade_state_current state
          WHERE state.state='OPEN'
          ORDER BY state.tenant_id
          LIMIT LEAST(GREATEST(requested_limit,1),100)
        $$;
        REVOKE ALL ON FUNCTION gatescope_security.paper_open_trade_due_tenants(integer) FROM PUBLIC;
        GRANT EXECUTE ON FUNCTION gatescope_security.paper_open_trade_due_tenants(integer)
          TO gatescope_paper;
        """
    )
    for table in PRIVATE_TABLES:
        op.execute(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY')
        op.execute(f'ALTER TABLE "{table}" FORCE ROW LEVEL SECURITY')
        op.execute(
            f'''CREATE POLICY tenant_isolation ON "{table}"
                USING (tenant_id = gatescope_security.current_tenant_id())
                WITH CHECK (tenant_id = gatescope_security.current_tenant_id())'''
        )
    for table in IMMUTABLE_TABLES:
        op.execute(
            f'''CREATE TRIGGER "{table}_immutable"
                BEFORE UPDATE OR DELETE ON "{table}"
                FOR EACH ROW EXECUTE FUNCTION gatescope_security.reject_fact_mutation()'''
        )
    op.execute(
        """
        GRANT SELECT, INSERT, UPDATE ON paper_accounts TO gatescope_api;
        GRANT SELECT ON signal_evidence_snapshots, signal_bar_manifest_items, signal_gate_results
          TO gatescope_api, gatescope_mcp, gatescope_chart;
        GRANT INSERT ON signal_evidence_snapshots, signal_bar_manifest_items, signal_gate_results
          TO gatescope_strategy;
        GRANT SELECT ON signal_evidence_snapshots, signal_bar_manifest_items, signal_gate_results
          TO gatescope_strategy;
        GRANT SELECT, INSERT ON execution_decisions, trade_proposals, proposal_status_events,
          approval_revalidation_snapshots, approvals, trades, trade_bar_observations, trade_events
          TO gatescope_api;
        GRANT SELECT, INSERT, UPDATE ON proposal_status_current, trade_state_current TO gatescope_api;
        GRANT SELECT ON paper_accounts, execution_decisions, trade_proposals, proposal_status_current,
          approval_revalidation_snapshots, approvals, trades, trade_bar_observations, trade_events, trade_state_current
          TO gatescope_mcp, gatescope_chart;
        GRANT SELECT ON paper_accounts, execution_decisions, trade_proposals, proposal_status_current,
          approval_revalidation_snapshots, approvals, trades, trade_bar_observations, trade_events, trade_state_current
          TO gatescope_executor;
        GRANT SELECT ON instruments, bars, paper_accounts, signal_evidence_snapshots,
          execution_decisions, trade_proposals, approvals, trades, trade_bar_observations,
          trade_events, trade_state_current TO gatescope_paper;
        GRANT INSERT ON trade_bar_observations, trade_events, outbox_events TO gatescope_paper;
        GRANT UPDATE ON trade_state_current, paper_accounts TO gatescope_paper;
        """
    )


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute(
        "DROP FUNCTION IF EXISTS gatescope_security.resolve_active_telegram_principal(bigint,bigint)"
    )
    op.execute("DROP FUNCTION IF EXISTS gatescope_security.paper_open_trade_due_tenants(integer)")
    for table in reversed(PRIVATE_TABLES):
        op.execute(f'DROP TABLE IF EXISTS "{table}" CASCADE')
