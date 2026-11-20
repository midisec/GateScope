"""Encrypt-only Gate TESTNET credential admission and validation queue.

Revision ID: 0009_exchange_credential_admission
Revises: 0008_paper_trade_ledger
"""

from __future__ import annotations

from alembic import op

revision = "0009_exchange_credential_admission"
down_revision = "0008_paper_trade_ledger"
branch_labels = None
depends_on = None

PRIVATE_TABLES = [
    "exchange_accounts",
    "secret_credentials",
    "credential_validation_commands",
    "credential_validation_results",
]


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute(
        """
        CREATE TABLE exchange_accounts (
          id uuid PRIMARY KEY,
          tenant_id uuid NOT NULL REFERENCES tenants(id),
          owner_user_id uuid NOT NULL REFERENCES users(id),
          venue text NOT NULL CHECK (venue='GATE'),
          alias text NOT NULL CHECK (length(alias) BETWEEN 1 AND 80),
          environment text NOT NULL CHECK (environment='TESTNET'),
          market_type text NOT NULL CHECK (market_type='FUTURES_USDT'),
          settle_asset text NOT NULL CHECK (settle_asset='USDT'),
          status text NOT NULL CHECK (
            status IN ('PENDING_VALIDATION','READ_VALIDATED','VALIDATION_FAILED','DISABLED')
          ),
          trading_ready boolean NOT NULL DEFAULT false CHECK (NOT trading_ready),
          credential_version integer NOT NULL DEFAULT 0 CHECK (credential_version >= 0),
          active_credential_id uuid,
          pending_credential_id uuid,
          credential_fingerprint text CHECK (
            credential_fingerprint IS NULL OR credential_fingerprint ~ '^SHA256:[0-9A-F]{12}$'
          ),
          capability_json jsonb NOT NULL DEFAULT '{}',
          warning_codes text[] NOT NULL DEFAULT '{}',
          last_safe_error_code text,
          validation_requested_at timestamptz NOT NULL,
          validated_at timestamptz,
          etag bigint NOT NULL DEFAULT 1 CHECK (etag > 0),
          created_at timestamptz NOT NULL,
          updated_at timestamptz NOT NULL,
          CHECK (jsonb_typeof(capability_json)='object'),
          UNIQUE (tenant_id,id)
        );
        CREATE UNIQUE INDEX exchange_accounts_active_alias_idx
          ON exchange_accounts(tenant_id, alias) WHERE status<>'DISABLED';

        CREATE TABLE secret_credentials (
          id uuid PRIMARY KEY,
          tenant_id uuid NOT NULL REFERENCES tenants(id),
          exchange_account_id uuid NOT NULL,
          version integer NOT NULL CHECK (version > 0),
          ciphertext bytea NOT NULL CHECK (octet_length(ciphertext) BETWEEN 32 AND 8192),
          nonce bytea NOT NULL CHECK (octet_length(nonce)=12),
          wrapped_data_key bytea NOT NULL CHECK (octet_length(wrapped_data_key) BETWEEN 384 AND 1024),
          encryption_context text NOT NULL CHECK (length(encryption_context) BETWEEN 16 AND 512),
          key_version text NOT NULL CHECK (length(key_version) BETWEEN 1 AND 80),
          fingerprint text NOT NULL CHECK (fingerprint ~ '^SHA256:[0-9A-F]{12}$'),
          no_withdrawal_attested boolean NOT NULL CHECK (no_withdrawal_attested),
          status text NOT NULL CHECK (
            status IN ('PENDING_VALIDATION','READ_VALIDATED','REJECTED','SUPERSEDED','REVOKED')
          ),
          created_by uuid NOT NULL REFERENCES users(id),
          created_at timestamptz NOT NULL,
          validated_at timestamptz,
          rejected_at timestamptz,
          revoked_at timestamptz,
          FOREIGN KEY (tenant_id,exchange_account_id)
            REFERENCES exchange_accounts(tenant_id,id),
          UNIQUE (tenant_id,id),
          UNIQUE (tenant_id,exchange_account_id,id),
          UNIQUE (tenant_id, exchange_account_id, version),
          UNIQUE (tenant_id, exchange_account_id, fingerprint)
        );
        ALTER TABLE exchange_accounts
          ADD CONSTRAINT exchange_accounts_active_credential_fk
          FOREIGN KEY (tenant_id,active_credential_id)
          REFERENCES secret_credentials(tenant_id,id);
        ALTER TABLE exchange_accounts
          ADD CONSTRAINT exchange_accounts_pending_credential_fk
          FOREIGN KEY (tenant_id,pending_credential_id)
          REFERENCES secret_credentials(tenant_id,id);

        CREATE TABLE credential_validation_commands (
          id uuid PRIMARY KEY,
          tenant_id uuid NOT NULL REFERENCES tenants(id),
          exchange_account_id uuid NOT NULL,
          credential_id uuid NOT NULL,
          requested_by uuid NOT NULL REFERENCES users(id),
          state text NOT NULL CHECK (state IN ('PENDING','PROCESSING','SUCCEEDED','FAILED')),
          attempt_count integer NOT NULL DEFAULT 0 CHECK (attempt_count BETWEEN 0 AND 5),
          next_attempt_at timestamptz NOT NULL,
          lease_owner text,
          lease_expires_at timestamptz,
          safe_result_code text,
          created_at timestamptz NOT NULL,
          started_at timestamptz,
          completed_at timestamptz,
          updated_at timestamptz NOT NULL,
          FOREIGN KEY (tenant_id,exchange_account_id)
            REFERENCES exchange_accounts(tenant_id,id),
          FOREIGN KEY (tenant_id,exchange_account_id,credential_id)
            REFERENCES secret_credentials(tenant_id,exchange_account_id,id),
          UNIQUE (tenant_id,exchange_account_id,credential_id,id)
        );
        CREATE UNIQUE INDEX credential_validation_one_active_idx
          ON credential_validation_commands(tenant_id, exchange_account_id)
          WHERE state IN ('PENDING','PROCESSING');
        CREATE INDEX credential_validation_due_idx
          ON credential_validation_commands(next_attempt_at, created_at)
          WHERE state='PENDING';

        CREATE TABLE credential_validation_results (
          id uuid PRIMARY KEY,
          tenant_id uuid NOT NULL REFERENCES tenants(id),
          exchange_account_id uuid NOT NULL,
          credential_id uuid NOT NULL,
          command_id uuid NOT NULL UNIQUE,
          success boolean NOT NULL,
          capability_json jsonb NOT NULL,
          warning_codes text[] NOT NULL DEFAULT '{}',
          safe_error_code text,
          checked_at timestamptz NOT NULL,
          created_at timestamptz NOT NULL,
          FOREIGN KEY (tenant_id,exchange_account_id)
            REFERENCES exchange_accounts(tenant_id,id),
          FOREIGN KEY (tenant_id,exchange_account_id,credential_id)
            REFERENCES secret_credentials(tenant_id,exchange_account_id,id),
          FOREIGN KEY (tenant_id,exchange_account_id,credential_id,command_id)
            REFERENCES credential_validation_commands(tenant_id,exchange_account_id,credential_id,id),
          CHECK (jsonb_typeof(capability_json)='object'),
          CHECK ((success AND safe_error_code IS NULL) OR (NOT success AND safe_error_code IS NOT NULL))
        );
        CREATE INDEX credential_validation_results_account_idx
          ON credential_validation_results(tenant_id, exchange_account_id, checked_at DESC);
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
        CREATE TRIGGER credential_validation_results_immutable
          BEFORE UPDATE OR DELETE ON credential_validation_results
          FOR EACH ROW EXECUTE FUNCTION gatescope_security.reject_fact_mutation();

        CREATE OR REPLACE FUNCTION gatescope_security.guard_exchange_validation_projection()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF (current_user='gatescope_api' OR pg_has_role(current_user, 'gatescope_api', 'member'))
             AND NOT pg_has_role(current_user, 'gatescope_executor', 'member') THEN
            IF (TG_OP='INSERT' AND (
                 NEW.status<>'PENDING_VALIDATION'
                 OR NEW.trading_ready
                 OR NEW.active_credential_id IS NOT NULL
                 OR NEW.credential_fingerprint IS NOT NULL
                 OR NEW.capability_json<>'{}'::jsonb
                 OR NEW.validated_at IS NOT NULL
               )) OR (TG_OP='UPDATE' AND (
                 NEW.status='READ_VALIDATED'
                 OR NEW.active_credential_id IS DISTINCT FROM OLD.active_credential_id
                 OR NEW.credential_fingerprint IS DISTINCT FROM OLD.credential_fingerprint
                 OR NEW.capability_json IS DISTINCT FROM OLD.capability_json
                 OR NEW.validated_at IS DISTINCT FROM OLD.validated_at
               )) THEN
              RAISE EXCEPTION 'API role cannot assert exchange validation results'
                USING ERRCODE = '42501';
            END IF;
          END IF;
          RETURN NEW;
        END
        $$;
        REVOKE ALL ON FUNCTION gatescope_security.guard_exchange_validation_projection() FROM PUBLIC;
        GRANT EXECUTE ON FUNCTION gatescope_security.guard_exchange_validation_projection()
          TO gatescope_api, gatescope_executor;
        CREATE TRIGGER exchange_accounts_validation_guard
          BEFORE INSERT OR UPDATE ON exchange_accounts FOR EACH ROW
          EXECUTE FUNCTION gatescope_security.guard_exchange_validation_projection();

        CREATE OR REPLACE FUNCTION gatescope_security.guard_secret_credential_envelope()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF NEW.id IS DISTINCT FROM OLD.id
             OR NEW.tenant_id IS DISTINCT FROM OLD.tenant_id
             OR NEW.exchange_account_id IS DISTINCT FROM OLD.exchange_account_id
             OR NEW.version IS DISTINCT FROM OLD.version
             OR NEW.ciphertext IS DISTINCT FROM OLD.ciphertext
             OR NEW.nonce IS DISTINCT FROM OLD.nonce
             OR NEW.wrapped_data_key IS DISTINCT FROM OLD.wrapped_data_key
             OR NEW.encryption_context IS DISTINCT FROM OLD.encryption_context
             OR NEW.key_version IS DISTINCT FROM OLD.key_version
             OR NEW.fingerprint IS DISTINCT FROM OLD.fingerprint
             OR NEW.no_withdrawal_attested IS DISTINCT FROM OLD.no_withdrawal_attested
             OR NEW.created_by IS DISTINCT FROM OLD.created_by
             OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
            RAISE EXCEPTION 'credential envelope version is immutable'
              USING ERRCODE = '55000';
          END IF;
          RETURN NEW;
        END
        $$;
        REVOKE ALL ON FUNCTION gatescope_security.guard_secret_credential_envelope() FROM PUBLIC;
        GRANT EXECUTE ON FUNCTION gatescope_security.guard_secret_credential_envelope()
          TO gatescope_executor;
        CREATE TRIGGER secret_credentials_envelope_guard
          BEFORE UPDATE ON secret_credentials FOR EACH ROW
          EXECUTE FUNCTION gatescope_security.guard_secret_credential_envelope();

        CREATE OR REPLACE FUNCTION gatescope_security.credential_validation_due_tenants(
          requested_limit integer
        ) RETURNS TABLE(tenant_id uuid)
        LANGUAGE sql SECURITY DEFINER STABLE
        SET search_path = pg_catalog, public AS $$
          SELECT DISTINCT command.tenant_id
          FROM public.credential_validation_commands command
          WHERE (command.state='PENDING' AND command.next_attempt_at <= now())
             OR (command.state='PROCESSING' AND command.lease_expires_at <= now())
          ORDER BY command.tenant_id
          LIMIT LEAST(GREATEST(requested_limit,1),100)
        $$;
        REVOKE ALL ON FUNCTION gatescope_security.credential_validation_due_tenants(integer) FROM PUBLIC;
        GRANT EXECUTE ON FUNCTION gatescope_security.credential_validation_due_tenants(integer)
          TO gatescope_executor;

        GRANT SELECT, INSERT ON exchange_accounts TO gatescope_api;
        GRANT UPDATE (status,trading_ready,credential_version,pending_credential_id,
          warning_codes,last_safe_error_code,validation_requested_at,etag,updated_at)
          ON exchange_accounts TO gatescope_api;
        GRANT INSERT ON secret_credentials TO gatescope_api;
        GRANT SELECT, INSERT ON credential_validation_commands TO gatescope_api;
        GRANT SELECT ON credential_validation_results TO gatescope_api;
        GRANT INSERT ON outbox_events TO gatescope_api;

        GRANT SELECT ON exchange_accounts, secret_credentials,
          credential_validation_commands TO gatescope_executor;
        GRANT UPDATE (active_credential_id,pending_credential_id,credential_fingerprint,
          status,trading_ready,capability_json,warning_codes,last_safe_error_code,
          validated_at,etag,updated_at) ON exchange_accounts TO gatescope_executor;
        GRANT UPDATE (status,validated_at,rejected_at,revoked_at)
          ON secret_credentials TO gatescope_executor;
        GRANT UPDATE (state,attempt_count,next_attempt_at,lease_owner,lease_expires_at,
          safe_result_code,started_at,completed_at,updated_at)
          ON credential_validation_commands TO gatescope_executor;
        GRANT INSERT ON credential_validation_results, outbox_events TO gatescope_executor;
        """
    )


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("DROP FUNCTION IF EXISTS gatescope_security.credential_validation_due_tenants(integer)")
    op.execute("DROP FUNCTION IF EXISTS gatescope_security.guard_exchange_validation_projection() CASCADE")
    op.execute("DROP FUNCTION IF EXISTS gatescope_security.guard_secret_credential_envelope() CASCADE")
    for table in reversed(PRIVATE_TABLES):
        op.execute(f'DROP TABLE IF EXISTS "{table}" CASCADE')
