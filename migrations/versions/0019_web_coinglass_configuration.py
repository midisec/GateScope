"""Encrypted tenant Coinglass configuration and bounded test jobs.

Revision ID: 0019_web_coinglass_configuration
Revises: 0018_market_strategy_subscriptions
"""

from __future__ import annotations

from alembic import op


revision = "0019_web_coinglass_configuration"
down_revision = "0018_market_strategy_subscriptions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute(
        """
        CREATE TABLE coinglass_configs (
          id uuid PRIMARY KEY,
          tenant_id uuid NOT NULL UNIQUE REFERENCES tenants(id),
          origin text NOT NULL CHECK (origin='https://vip2.coinglass.site'),
          key_fingerprint text NOT NULL CHECK (key_fingerprint ~ '^SHA256:[0-9A-F]{12}$'),
          secret_ciphertext bytea NOT NULL,
          secret_nonce bytea NOT NULL CHECK (octet_length(secret_nonce)=12),
          wrapped_data_key bytea NOT NULL,
          encryption_context text NOT NULL CHECK (length(encryption_context) BETWEEN 16 AND 512),
          key_version text NOT NULL CHECK (length(key_version) BETWEEN 1 AND 80),
          secret_version integer NOT NULL CHECK (secret_version > 0),
          status text NOT NULL CHECK (status IN ('PENDING_TEST','ACTIVE','VALIDATION_FAILED','DISABLED')),
          last_safe_error_code text,
          capability_json jsonb NOT NULL DEFAULT '{}'::jsonb
            CHECK (jsonb_typeof(capability_json)='object'),
          last_tested_at timestamptz,
          created_by uuid NOT NULL REFERENCES users(id),
          updated_by uuid NOT NULL REFERENCES users(id),
          disabled_at timestamptz,
          etag bigint NOT NULL DEFAULT 1 CHECK (etag > 0),
          created_at timestamptz NOT NULL,
          updated_at timestamptz NOT NULL
        );

        CREATE TABLE coinglass_test_jobs (
          id uuid PRIMARY KEY,
          tenant_id uuid NOT NULL REFERENCES tenants(id),
          config_id uuid NOT NULL REFERENCES coinglass_configs(id),
          config_secret_version integer NOT NULL CHECK (config_secret_version > 0),
          requested_by uuid NOT NULL REFERENCES users(id),
          test_symbol text NOT NULL CHECK (test_symbol ~ '^[A-Z0-9]{1,20}$'),
          status text NOT NULL CHECK (status IN ('QUEUED','RUNNING','SUCCEEDED','FAILED')),
          worker_id text,
          attempt_count integer NOT NULL DEFAULT 0 CHECK (attempt_count BETWEEN 0 AND 2),
          result_json jsonb NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(result_json)='object'),
          safe_error_code text,
          retry_after_seconds integer CHECK (retry_after_seconds BETWEEN 1 AND 3600),
          created_at timestamptz NOT NULL,
          started_at timestamptz,
          completed_at timestamptz,
          updated_at timestamptz NOT NULL
        );
        CREATE INDEX coinglass_test_jobs_queue_idx
          ON coinglass_test_jobs(status, created_at) WHERE status='QUEUED';
        CREATE INDEX coinglass_test_jobs_tenant_idx
          ON coinglass_test_jobs(tenant_id, created_at DESC);
        CREATE UNIQUE INDEX coinglass_test_jobs_one_active_idx
          ON coinglass_test_jobs(tenant_id, config_id, config_secret_version)
          WHERE status IN ('QUEUED','RUNNING');

        ALTER TABLE coinglass_configs ENABLE ROW LEVEL SECURITY;
        ALTER TABLE coinglass_configs FORCE ROW LEVEL SECURITY;
        CREATE POLICY tenant_isolation ON coinglass_configs
          USING (tenant_id = gatescope_security.current_tenant_id())
          WITH CHECK (tenant_id = gatescope_security.current_tenant_id());
        ALTER TABLE coinglass_test_jobs ENABLE ROW LEVEL SECURITY;
        ALTER TABLE coinglass_test_jobs FORCE ROW LEVEL SECURITY;
        CREATE POLICY tenant_isolation ON coinglass_test_jobs
          USING (tenant_id = gatescope_security.current_tenant_id())
          WITH CHECK (tenant_id = gatescope_security.current_tenant_id());

        CREATE OR REPLACE FUNCTION gatescope_security.claim_coinglass_test_job(
          requested_worker text
        )
        RETURNS TABLE(job_id uuid, tenant_id uuid)
        LANGUAGE sql SECURITY DEFINER VOLATILE
        SET search_path = pg_catalog, public AS $$
          WITH expired AS (
            UPDATE public.coinglass_test_jobs
               SET status='FAILED', safe_error_code='PROVIDER_TEST_WORKER_TIMEOUT',
                   completed_at=now(), updated_at=now()
             WHERE status='RUNNING' AND attempt_count >= 2
               AND updated_at < now() - interval '2 minutes'
             RETURNING id
          ), candidate AS (
            SELECT id
              FROM public.coinglass_test_jobs
             WHERE (status='QUEUED' OR (
                      status='RUNNING' AND attempt_count < 2
                      AND updated_at < now() - interval '2 minutes'
                    ))
               AND id NOT IN (SELECT id FROM expired)
             ORDER BY created_at, id
             FOR UPDATE SKIP LOCKED
             LIMIT 1
          )
          UPDATE public.coinglass_test_jobs job
             SET status='RUNNING', worker_id=left(requested_worker, 80),
                 attempt_count=attempt_count+1, started_at=now(), updated_at=now()
            FROM candidate
           WHERE job.id=candidate.id
             AND requested_worker ~ '^[A-Za-z0-9_.:-]{1,80}$'
          RETURNING job.id, job.tenant_id
        $$;
        REVOKE ALL ON FUNCTION gatescope_security.claim_coinglass_test_job(text) FROM PUBLIC;
        REVOKE ALL ON coinglass_configs, coinglass_test_jobs FROM PUBLIC;

        GRANT SELECT,INSERT,UPDATE ON coinglass_configs TO gatescope_api;
        GRANT SELECT,INSERT ON coinglass_test_jobs TO gatescope_api;
        GRANT SELECT ON coinglass_configs TO gatescope_market;
        GRANT UPDATE (status,last_safe_error_code,capability_json,last_tested_at,etag,updated_at)
          ON coinglass_configs TO gatescope_market;
        GRANT SELECT,UPDATE ON coinglass_test_jobs TO gatescope_market;
        GRANT EXECUTE ON FUNCTION gatescope_security.claim_coinglass_test_job(text)
          TO gatescope_market;
        """
    )


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute(
        """
        DROP FUNCTION IF EXISTS gatescope_security.claim_coinglass_test_job(text);
        DROP TABLE IF EXISTS coinglass_test_jobs;
        DROP TABLE IF EXISTS coinglass_configs;
        """
    )
