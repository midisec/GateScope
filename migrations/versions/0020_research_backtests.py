"""Durable owner-isolated research runs and immutable result artifacts."""
from alembic import op

revision = "0020_research_backtests"
down_revision = "0019_web_coinglass_configuration"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("""
      CREATE TABLE research_backtests (
        id uuid PRIMARY KEY, tenant_id uuid NOT NULL REFERENCES tenants(id),
        owner_user_id uuid NOT NULL REFERENCES users(id),
        instrument_id uuid NOT NULL REFERENCES instruments(id),
        timeframe text NOT NULL, spec_json jsonb NOT NULL,
        bar_limit integer NOT NULL CHECK (bar_limit BETWEEN 300 AND 5000),
        from_time timestamptz, to_time timestamptz,
        state text NOT NULL CHECK (state IN ('QUEUED','RUNNING','SUCCEEDED','FAILED')),
        safe_error text, created_at timestamptz NOT NULL DEFAULT now(),
        started_at timestamptz, completed_at timestamptz
      );
      CREATE INDEX research_backtests_owner ON research_backtests(tenant_id,owner_user_id,created_at DESC);
      CREATE UNIQUE INDEX research_backtests_active ON research_backtests(tenant_id,owner_user_id)
        WHERE state IN ('QUEUED','RUNNING');
      CREATE TABLE research_backtest_results (
        run_id uuid PRIMARY KEY REFERENCES research_backtests(id), tenant_id uuid NOT NULL,
        owner_user_id uuid NOT NULL, report_json jsonb NOT NULL,
        created_at timestamptz NOT NULL DEFAULT now()
      );
      ALTER TABLE research_backtests ENABLE ROW LEVEL SECURITY;
      ALTER TABLE research_backtests FORCE ROW LEVEL SECURITY;
      CREATE POLICY own_run ON research_backtests USING (
        tenant_id=gatescope_security.current_tenant_id() AND owner_user_id=gatescope_security.current_subject_id()
      ) WITH CHECK (
        tenant_id=gatescope_security.current_tenant_id() AND owner_user_id=gatescope_security.current_subject_id()
      );
      ALTER TABLE research_backtest_results ENABLE ROW LEVEL SECURITY;
      ALTER TABLE research_backtest_results FORCE ROW LEVEL SECURITY;
      CREATE POLICY own_report ON research_backtest_results USING (
        tenant_id=gatescope_security.current_tenant_id() AND owner_user_id=gatescope_security.current_subject_id()
      ) WITH CHECK (
        tenant_id=gatescope_security.current_tenant_id() AND owner_user_id=gatescope_security.current_subject_id()
      );
      CREATE TRIGGER research_result_immutable BEFORE UPDATE OR DELETE ON research_backtest_results
        FOR EACH ROW EXECUTE FUNCTION gatescope_security.reject_fact_mutation();
      CREATE FUNCTION gatescope_security.claim_research_backtest()
        RETURNS TABLE(run_id uuid, tenant uuid, subject uuid)
        LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
        BEGIN
          UPDATE public.research_backtests SET state='FAILED',safe_error='回测超时，请重试',completed_at=now()
            WHERE state='RUNNING' AND started_at < now()-interval '5 minutes';
          RETURN QUERY WITH candidate AS (
            SELECT id FROM public.research_backtests WHERE state='QUEUED'
            ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT 1
          ) UPDATE public.research_backtests r SET state='RUNNING',started_at=now()
            FROM candidate c WHERE r.id=c.id RETURNING r.id,r.tenant_id,r.owner_user_id;
        END $$;
      REVOKE ALL ON FUNCTION gatescope_security.claim_research_backtest() FROM PUBLIC;
      GRANT EXECUTE ON FUNCTION gatescope_security.claim_research_backtest() TO gatescope_strategy;
      GRANT SELECT,INSERT ON research_backtests TO gatescope_api;
      GRANT SELECT ON research_backtest_results TO gatescope_api;
      GRANT SELECT,UPDATE ON research_backtests TO gatescope_strategy;
      GRANT SELECT,INSERT ON research_backtest_results TO gatescope_strategy;
    """)


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("DROP FUNCTION gatescope_security.claim_research_backtest(); DROP TABLE research_backtest_results; DROP TABLE research_backtests;")
