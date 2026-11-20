"""Gate TESTNET arming and durable execution drills.

Revision ID: 0010_gate_testnet_execution
Revises: 0009_exchange_credential_admission
"""

from __future__ import annotations

from alembic import op

revision = "0010_gate_testnet_execution"
down_revision = "0009_exchange_credential_admission"
branch_labels = None
depends_on = None

PRIVATE_TABLES = [
    "strategy_execution_certifications",
    "testnet_execution_drills",
    "testnet_execution_events",
]


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute(
        """
        ALTER TABLE exchange_accounts DROP CONSTRAINT exchange_accounts_status_check;
        ALTER TABLE exchange_accounts DROP CONSTRAINT exchange_accounts_trading_ready_check;
        ALTER TABLE exchange_accounts
          ADD COLUMN testnet_armed_at timestamptz,
          ADD COLUMN testnet_armed_by uuid REFERENCES users(id),
          ADD COLUMN execution_freeze_code text,
          ADD CONSTRAINT exchange_accounts_status_check CHECK (status IN (
            'PENDING_VALIDATION','READ_VALIDATED','TESTNET_READY','TESTNET_BUSY',
            'EXECUTION_FROZEN','VALIDATION_FAILED','DISABLED'
          )),
          ADD CONSTRAINT exchange_accounts_trading_ready_check CHECK (
            trading_ready = (status='TESTNET_READY')
          ),
          ADD CONSTRAINT exchange_accounts_arming_check CHECK (
            (status IN ('TESTNET_READY','TESTNET_BUSY')
              AND testnet_armed_at IS NOT NULL AND testnet_armed_by IS NOT NULL
              AND execution_freeze_code IS NULL)
            OR status NOT IN ('TESTNET_READY','TESTNET_BUSY')
          ),
          ADD CONSTRAINT exchange_accounts_freeze_check CHECK (
            (status='EXECUTION_FROZEN' AND execution_freeze_code IS NOT NULL)
            OR (status<>'EXECUTION_FROZEN' AND execution_freeze_code IS NULL)
          );

        CREATE TABLE strategy_execution_certifications (
          id uuid PRIMARY KEY,
          tenant_id uuid NOT NULL REFERENCES tenants(id),
          strategy_version_ref text NOT NULL CHECK (length(strategy_version_ref) BETWEEN 1 AND 120),
          environment text NOT NULL CHECK (environment='TESTNET'),
          decision text NOT NULL CHECK (decision IN ('CERTIFY','ABSTAIN')),
          evidence_tier text NOT NULL CHECK (evidence_tier IN ('PAPER','LIVE_ELIGIBLE')),
          certification_policy_version text NOT NULL CHECK (
            length(certification_policy_version) BETWEEN 1 AND 80
          ),
          source_artifact_digest text NOT NULL CHECK (source_artifact_digest ~ '^[0-9a-f]{64}$'),
          artifact_computed_at timestamptz NOT NULL,
          gate_report_json jsonb NOT NULL CHECK (
            jsonb_typeof(gate_report_json)='array' AND jsonb_array_length(gate_report_json)=19
          ),
          reason_codes_json jsonb NOT NULL CHECK (jsonb_typeof(reason_codes_json)='array'),
          assumptions_json jsonb NOT NULL CHECK (
            jsonb_typeof(assumptions_json)='array' AND jsonb_array_length(assumptions_json)>0
          ),
          certification_digest text NOT NULL CHECK (certification_digest ~ '^[0-9a-f]{64}$'),
          approved_by uuid NOT NULL REFERENCES users(id),
          approved_at timestamptz NOT NULL,
          expires_at timestamptz NOT NULL,
          created_at timestamptz NOT NULL,
          UNIQUE (tenant_id,certification_digest),
          CHECK (artifact_computed_at <= approved_at AND approved_at <= created_at),
          CHECK (expires_at > approved_at AND expires_at <= approved_at + interval '90 days'),
          CHECK ((decision='CERTIFY') = (evidence_tier='LIVE_ELIGIBLE'))
        );
        CREATE TABLE testnet_execution_drills (
          id uuid PRIMARY KEY,
          public_drill_id text NOT NULL CHECK (
            public_drill_id ~ '^testnet_[0-9A-HJKMNP-TV-Z]{26}$'
          ),
          tenant_id uuid NOT NULL REFERENCES tenants(id),
          owner_user_id uuid NOT NULL REFERENCES users(id),
          paper_trade_id uuid NOT NULL REFERENCES trades(id),
          exchange_account_id uuid NOT NULL REFERENCES exchange_accounts(id),
          instrument_id uuid NOT NULL REFERENCES instruments(id),
          contract text NOT NULL CHECK (contract ~ '^[A-Z0-9]{2,20}_USDT$'),
          side text NOT NULL CHECK (side IN ('LONG','SHORT')),
          environment text NOT NULL DEFAULT 'TESTNET' CHECK (environment='TESTNET'),
          exact_terms_json jsonb NOT NULL CHECK (jsonb_typeof(exact_terms_json)='object'),
          exact_terms_digest text NOT NULL CHECK (exact_terms_digest ~ '^[0-9a-f]{64}$'),
          idempotency_hash text NOT NULL CHECK (idempotency_hash ~ '^[0-9a-f]{64}$'),
          state text NOT NULL CHECK (state IN (
            'QUEUED','PROCESSING','ENTRY_UNFILLED','ENTRY_UNKNOWN','PROTECTED',
            'CLOSED_PROTECTED','CLOSED_FAIL_SAFE','PREFLIGHT_REJECTED',
            'MANUAL_INTERVENTION_REQUIRED'
          )),
          safe_code text,
          entry_order_id text,
          filled_contracts integer NOT NULL DEFAULT 0 CHECK (filled_contracts >= 0),
          stop_order_id text,
          take_profit_order_id text,
          fail_safe_order_id text,
          frozen boolean NOT NULL DEFAULT false,
          attempt_count integer NOT NULL DEFAULT 0 CHECK (attempt_count BETWEEN 0 AND 3),
          lease_owner text,
          lease_expires_at timestamptz,
          next_reconcile_at timestamptz,
          monitor_lease_owner text,
          monitor_lease_expires_at timestamptz,
          reconcile_count integer NOT NULL DEFAULT 0 CHECK (reconcile_count >= 0),
          version bigint NOT NULL DEFAULT 1 CHECK (version > 0),
          created_at timestamptz NOT NULL,
          started_at timestamptz,
          completed_at timestamptz,
          updated_at timestamptz NOT NULL,
          UNIQUE (tenant_id, public_drill_id),
          UNIQUE (tenant_id, owner_user_id, idempotency_hash),
          UNIQUE (tenant_id, paper_trade_id, exchange_account_id),
          CHECK ((state IN ('QUEUED','PROCESSING')) = (completed_at IS NULL)),
          CHECK ((state='PROCESSING') = (lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL)),
          CHECK ((state='PROTECTED') = (next_reconcile_at IS NOT NULL)),
          CHECK ((monitor_lease_owner IS NULL) = (monitor_lease_expires_at IS NULL)),
          CHECK (state='QUEUED' OR attempt_count > 0)
        );
        CREATE INDEX testnet_execution_queue_idx
          ON testnet_execution_drills(created_at,id) WHERE state='QUEUED';
        CREATE INDEX testnet_execution_account_idx
          ON testnet_execution_drills(tenant_id,exchange_account_id,created_at DESC);

        CREATE TABLE testnet_execution_events (
          id uuid PRIMARY KEY,
          tenant_id uuid NOT NULL REFERENCES tenants(id),
          drill_id uuid NOT NULL REFERENCES testnet_execution_drills(id),
          sequence bigint NOT NULL CHECK (sequence > 0),
          event_type text NOT NULL CHECK (event_type ~ '^[A-Z][A-Z0-9_]{2,79}$'),
          payload_json jsonb NOT NULL CHECK (jsonb_typeof(payload_json)='object'),
          previous_digest text CHECK (previous_digest IS NULL OR previous_digest ~ '^[0-9a-f]{64}$'),
          event_digest text NOT NULL CHECK (event_digest ~ '^[0-9a-f]{64}$'),
          occurred_at timestamptz NOT NULL,
          recorded_at timestamptz NOT NULL,
          UNIQUE (tenant_id,drill_id,sequence),
          UNIQUE (tenant_id,event_digest)
        );
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
    op.execute(
        """
        CREATE TRIGGER testnet_execution_events_immutable
          BEFORE UPDATE OR DELETE ON testnet_execution_events
          FOR EACH ROW EXECUTE FUNCTION gatescope_security.reject_fact_mutation();

        CREATE OR REPLACE FUNCTION gatescope_security.guard_testnet_event_writer()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF (current_user='gatescope_api' OR pg_has_role(current_user, 'gatescope_api', 'member'))
             AND NOT pg_has_role(current_user, 'gatescope_executor', 'member')
             AND (NEW.event_type<>'DRILL_QUEUED' OR NEW.sequence<>1
                  OR NEW.previous_digest IS NOT NULL) THEN
            RAISE EXCEPTION 'API role can only create the initial TESTNET event'
              USING ERRCODE='42501';
          END IF;
          RETURN NEW;
        END
        $$;
        REVOKE ALL ON FUNCTION gatescope_security.guard_testnet_event_writer() FROM PUBLIC;
        GRANT EXECUTE ON FUNCTION gatescope_security.guard_testnet_event_writer()
          TO gatescope_api,gatescope_executor;
        CREATE TRIGGER testnet_execution_event_writer_guard
          BEFORE INSERT ON testnet_execution_events FOR EACH ROW
          EXECUTE FUNCTION gatescope_security.guard_testnet_event_writer();

        CREATE OR REPLACE FUNCTION gatescope_security.guard_exchange_validation_projection()
        RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE valid_arm boolean;
        DECLARE valid_disarm boolean;
        DECLARE valid_reservation boolean;
        DECLARE valid_freeze boolean;
        BEGIN
          IF (current_user='gatescope_api' OR pg_has_role(current_user, 'gatescope_api', 'member'))
             AND NOT pg_has_role(current_user, 'gatescope_executor', 'member') THEN
            IF TG_OP='INSERT' THEN
              IF NEW.status<>'PENDING_VALIDATION' OR NEW.trading_ready
                 OR NEW.active_credential_id IS NOT NULL
                 OR NEW.credential_fingerprint IS NOT NULL
                 OR NEW.capability_json<>'{}'::jsonb OR NEW.validated_at IS NOT NULL THEN
                RAISE EXCEPTION 'API role cannot assert exchange validation results'
                  USING ERRCODE='42501';
              END IF;
              RETURN NEW;
            END IF;
            valid_arm := OLD.status='READ_VALIDATED' AND NEW.status='TESTNET_READY'
              AND NEW.trading_ready AND OLD.active_credential_id IS NOT NULL
              AND coalesce((OLD.capability_json->>'account_read')::boolean,false)
              AND coalesce((OLD.capability_json->>'futures_usdt_read')::boolean,false)
              AND coalesce((OLD.capability_json->>'ip_whitelist_configured')::boolean,false)
              AND NEW.testnet_armed_at IS NOT NULL
              AND NEW.testnet_armed_by=gatescope_security.current_subject_id();
            valid_disarm := OLD.status='TESTNET_READY' AND NEW.status='READ_VALIDATED'
              AND NOT NEW.trading_ready AND NEW.testnet_armed_at IS NULL
              AND NEW.testnet_armed_by IS NULL;
            valid_reservation := OLD.status='TESTNET_READY' AND NEW.status='TESTNET_BUSY'
              AND NOT NEW.trading_ready;
            valid_freeze := OLD.status='TESTNET_BUSY' AND NEW.status='EXECUTION_FROZEN'
              AND NOT NEW.trading_ready AND NEW.execution_freeze_code IS NOT NULL;
            IF NEW.active_credential_id IS DISTINCT FROM OLD.active_credential_id
               OR NEW.credential_fingerprint IS DISTINCT FROM OLD.credential_fingerprint
               OR NEW.capability_json IS DISTINCT FROM OLD.capability_json
               OR NEW.validated_at IS DISTINCT FROM OLD.validated_at
               OR (NEW.status='READ_VALIDATED' AND NOT valid_disarm)
               OR (NEW.status='TESTNET_READY' AND NOT valid_arm)
               OR (NEW.status='TESTNET_BUSY' AND NOT valid_reservation)
               OR (NEW.status='EXECUTION_FROZEN' AND NOT valid_freeze) THEN
              RAISE EXCEPTION 'API role cannot assert exchange validation or execution state'
                USING ERRCODE='42501';
            END IF;
          END IF;
          RETURN NEW;
        END
        $$;
        REVOKE ALL ON FUNCTION gatescope_security.guard_exchange_validation_projection() FROM PUBLIC;
        GRANT EXECUTE ON FUNCTION gatescope_security.guard_exchange_validation_projection()
          TO gatescope_api,gatescope_executor;

        CREATE OR REPLACE FUNCTION gatescope_security.testnet_execution_due_tenants(
          requested_limit integer
        ) RETURNS TABLE(tenant_id uuid)
        LANGUAGE sql SECURITY DEFINER STABLE
        SET search_path = pg_catalog, public AS $$
          SELECT DISTINCT drill.tenant_id
          FROM public.testnet_execution_drills drill
          WHERE drill.state='QUEUED'
             OR (drill.state='PROCESSING' AND drill.lease_expires_at <= now())
             OR (drill.state='PROTECTED' AND drill.next_reconcile_at <= now()
                 AND (drill.monitor_lease_expires_at IS NULL OR drill.monitor_lease_expires_at <= now()))
          ORDER BY drill.tenant_id
          LIMIT LEAST(GREATEST(requested_limit,1),100)
        $$;
        REVOKE ALL ON FUNCTION gatescope_security.testnet_execution_due_tenants(integer) FROM PUBLIC;
        GRANT EXECUTE ON FUNCTION gatescope_security.testnet_execution_due_tenants(integer)
          TO gatescope_executor;

        CREATE OR REPLACE FUNCTION gatescope_security.guard_testnet_drill_admission()
        RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE source_trade record;
        DECLARE target_account record;
        BEGIN
          SELECT t.tenant_id,t.owner_user_id,t.instrument_id,t.side,t.exact_terms_json,
                 t.exact_terms_digest,state.state,evidence.strategy_version_ref
            INTO source_trade
          FROM trades t JOIN trade_state_current state ON state.trade_id=t.id
          JOIN signal_evidence_snapshots evidence ON evidence.id=t.signal_evidence_snapshot_id
          WHERE t.id=NEW.paper_trade_id;
          IF NOT FOUND THEN
            RAISE EXCEPTION 'testnet drill source trade not found' USING ERRCODE='23514';
          END IF;
          SELECT tenant_id,owner_user_id,status,trading_ready,environment,market_type
            INTO target_account FROM exchange_accounts WHERE id=NEW.exchange_account_id;
          IF NOT FOUND THEN
            RAISE EXCEPTION 'testnet drill exchange account not found' USING ERRCODE='23514';
          END IF;
          IF source_trade.tenant_id IS DISTINCT FROM NEW.tenant_id
             OR source_trade.owner_user_id IS DISTINCT FROM NEW.owner_user_id
             OR source_trade.instrument_id IS DISTINCT FROM NEW.instrument_id
             OR source_trade.side IS DISTINCT FROM NEW.side
             OR source_trade.exact_terms_json IS DISTINCT FROM NEW.exact_terms_json
             OR source_trade.exact_terms_digest IS DISTINCT FROM NEW.exact_terms_digest
             OR source_trade.state<>'OPEN'
             OR NOT EXISTS (
               SELECT 1 FROM strategy_execution_certifications certification
               WHERE certification.tenant_id=NEW.tenant_id
                 AND certification.strategy_version_ref=source_trade.strategy_version_ref
                 AND certification.environment='TESTNET'
                 AND certification.decision='CERTIFY'
                 AND certification.evidence_tier='LIVE_ELIGIBLE'
                 AND certification.approved_at <= now() AND certification.expires_at > now()
             )
             OR target_account.tenant_id IS DISTINCT FROM NEW.tenant_id
             OR target_account.owner_user_id IS DISTINCT FROM NEW.owner_user_id
             OR target_account.status<>'TESTNET_READY' OR NOT target_account.trading_ready
             OR target_account.environment<>'TESTNET' OR target_account.market_type<>'FUTURES_USDT' THEN
            RAISE EXCEPTION 'testnet drill admission invariants failed' USING ERRCODE='23514';
          END IF;
          RETURN NEW;
        END
        $$;
        REVOKE ALL ON FUNCTION gatescope_security.guard_testnet_drill_admission() FROM PUBLIC;
        GRANT EXECUTE ON FUNCTION gatescope_security.guard_testnet_drill_admission()
          TO gatescope_api;
        CREATE TRIGGER testnet_drill_admission_guard
          BEFORE INSERT ON testnet_execution_drills FOR EACH ROW
          EXECUTE FUNCTION gatescope_security.guard_testnet_drill_admission();

        CREATE TRIGGER strategy_execution_certifications_immutable
          BEFORE UPDATE OR DELETE ON strategy_execution_certifications
          FOR EACH ROW EXECUTE FUNCTION gatescope_security.reject_fact_mutation();

        CREATE OR REPLACE FUNCTION gatescope_security.guard_strategy_certification_writer()
        RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE expected_codes text[] := ARRAY[
          'INDEPENDENT_BLOCK_POWER','HOLM_FWER','POLICY_VALUE_LCB','DEFLATED_SHARPE',
          'BACKTEST_OVERFITTING','BASE_COST_OOS_EV','DOUBLE_COST_OOS_EV',
          'EVENT_EV_ROBUST_LCB','REGIME_CONCENTRATION','REGIME_BLOCKS',
          'LEAVE_ONE_REGIME_OUT','TRIAL_REGISTRY_COMPLETE','NESTED_WALK_FORWARD',
          'SEALED_HOLDOUT_CONSUMED_ONCE','CALIBRATION','MAXIMUM_DRAWDOWN',
          'DATA_QUALITY','OUT_OF_DISTRIBUTION','COST_MODEL'
        ];
        DECLARE observed_codes text[];
        DECLARE failed_codes text[];
        DECLARE recorded_reasons text[];
        BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM memberships membership
            JOIN users target ON target.id=membership.user_id
            WHERE membership.tenant_id=NEW.tenant_id
              AND membership.user_id=NEW.approved_by
              AND membership.role='OWNER' AND membership.status='ACTIVE'
              AND target.status='ACTIVE'
          ) THEN
            RAISE EXCEPTION 'strategy certification requires an active OWNER approver'
              USING ERRCODE='42501';
          END IF;
          SELECT array_agg(item->>'gate_code' ORDER BY ordinal)
            INTO observed_codes
          FROM jsonb_array_elements(NEW.gate_report_json) WITH ORDINALITY report(item,ordinal);
          IF observed_codes IS DISTINCT FROM expected_codes
             OR EXISTS (
               SELECT 1
               FROM jsonb_array_elements(NEW.gate_report_json) report(item)
               WHERE jsonb_typeof(item)<>'object'
                  OR coalesce(item->>'result','') NOT IN ('PASS','FAIL')
                  OR NOT (item ? 'value') OR NOT (item ? 'threshold')
             ) THEN
            RAISE EXCEPTION 'strategy certification gate report is invalid'
              USING ERRCODE='23514';
          END IF;
          SELECT coalesce(array_agg(item->>'gate_code' ORDER BY ordinal)
                          FILTER (WHERE item->>'result'='FAIL'), ARRAY[]::text[])
            INTO failed_codes
          FROM jsonb_array_elements(NEW.gate_report_json) WITH ORDINALITY report(item,ordinal);
          SELECT coalesce(array_agg(value ORDER BY ordinal), ARRAY[]::text[])
            INTO recorded_reasons
          FROM jsonb_array_elements_text(NEW.reason_codes_json)
               WITH ORDINALITY reason(value,ordinal);
          IF recorded_reasons IS DISTINCT FROM failed_codes
             OR (NEW.decision='CERTIFY' AND cardinality(failed_codes)<>0)
             OR (NEW.decision='ABSTAIN' AND cardinality(failed_codes)=0)
             OR EXISTS (
               SELECT 1 FROM jsonb_array_elements(NEW.reason_codes_json) reason(item)
               WHERE jsonb_typeof(item)<>'string'
             )
             OR EXISTS (
               SELECT 1 FROM jsonb_array_elements(NEW.assumptions_json) assumption(item)
               WHERE jsonb_typeof(item)<>'string' OR length(item #>> '{}')=0
             ) THEN
            RAISE EXCEPTION 'strategy certification decision does not match its evidence'
              USING ERRCODE='23514';
          END IF;
          RETURN NEW;
        END
        $$;
        REVOKE ALL ON FUNCTION gatescope_security.guard_strategy_certification_writer() FROM PUBLIC;
        GRANT EXECUTE ON FUNCTION gatescope_security.guard_strategy_certification_writer()
          TO gatescope_strategy;
        CREATE TRIGGER strategy_execution_certification_writer_guard
          BEFORE INSERT ON strategy_execution_certifications FOR EACH ROW
          EXECUTE FUNCTION gatescope_security.guard_strategy_certification_writer();

        GRANT SELECT,INSERT ON testnet_execution_drills,testnet_execution_events TO gatescope_api;
        GRANT SELECT ON strategy_execution_certifications TO gatescope_api,gatescope_executor;
        GRANT SELECT,INSERT ON strategy_execution_certifications TO gatescope_strategy;
        GRANT UPDATE (state,safe_code,entry_order_id,filled_contracts,stop_order_id,
          take_profit_order_id,fail_safe_order_id,frozen,attempt_count,lease_owner,
          lease_expires_at,next_reconcile_at,monitor_lease_owner,monitor_lease_expires_at,
          reconcile_count,version,started_at,completed_at,updated_at)
          ON testnet_execution_drills TO gatescope_executor;
        GRANT SELECT ON testnet_execution_drills,testnet_execution_events TO gatescope_executor;
        GRANT INSERT ON testnet_execution_events TO gatescope_executor;
        GRANT UPDATE (status,trading_ready,testnet_armed_at,testnet_armed_by,
          execution_freeze_code,etag,updated_at) ON exchange_accounts TO gatescope_api;
        GRANT UPDATE (status,trading_ready,execution_freeze_code,etag,updated_at)
          ON exchange_accounts TO gatescope_executor;
        GRANT INSERT ON outbox_events TO gatescope_executor;
        """
    )


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("DROP FUNCTION IF EXISTS gatescope_security.testnet_execution_due_tenants(integer)")
    op.execute("DROP FUNCTION IF EXISTS gatescope_security.guard_testnet_drill_admission() CASCADE")
    op.execute("DROP FUNCTION IF EXISTS gatescope_security.guard_testnet_event_writer() CASCADE")
    op.execute("DROP FUNCTION IF EXISTS gatescope_security.guard_strategy_certification_writer() CASCADE")
    for table in reversed(PRIVATE_TABLES):
        op.execute(f'DROP TABLE IF EXISTS "{table}" CASCADE')
    op.execute(
        """
        ALTER TABLE exchange_accounts DROP CONSTRAINT exchange_accounts_freeze_check;
        ALTER TABLE exchange_accounts DROP CONSTRAINT exchange_accounts_arming_check;
        ALTER TABLE exchange_accounts DROP CONSTRAINT exchange_accounts_trading_ready_check;
        ALTER TABLE exchange_accounts DROP CONSTRAINT exchange_accounts_status_check;
        UPDATE exchange_accounts
           SET status = CASE
                 WHEN status IN ('TESTNET_READY','TESTNET_BUSY') THEN 'READ_VALIDATED'
                 WHEN status='EXECUTION_FROZEN' THEN 'VALIDATION_FAILED'
                 ELSE status
               END,
               trading_ready = false,
               updated_at = now();
        ALTER TABLE exchange_accounts DROP COLUMN execution_freeze_code;
        ALTER TABLE exchange_accounts DROP COLUMN testnet_armed_by;
        ALTER TABLE exchange_accounts DROP COLUMN testnet_armed_at;
        ALTER TABLE exchange_accounts
          ADD CONSTRAINT exchange_accounts_status_check CHECK (
            status IN ('PENDING_VALIDATION','READ_VALIDATED','VALIDATION_FAILED','DISABLED')
          ),
          ADD CONSTRAINT exchange_accounts_trading_ready_check CHECK (NOT trading_ready);
        """
    )
