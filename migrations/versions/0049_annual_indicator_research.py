"""Bounded annual indicator research and lossless owner-scoped evidence chunks."""
from alembic import op

revision = "0049_annual_indicator_research"
down_revision = "0048_paper_decision_evidence"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("""
      ALTER TABLE research_backtests DROP CONSTRAINT research_backtests_bar_limit_check;
      ALTER TABLE research_backtests ADD CONSTRAINT research_backtests_bar_limit_check
        CHECK(bar_limit BETWEEN 300 AND 550000);
      ALTER TABLE research_backtests ADD CONSTRAINT research_backtests_owner_identity UNIQUE(id,tenant_id,owner_user_id);
      CREATE TABLE research_indicator_chunks (
        run_id uuid NOT NULL,
        tenant_id uuid NOT NULL, owner_user_id uuid NOT NULL,
        kind text NOT NULL CHECK(kind IN ('inputs','signals','trades','curve','drawdown')),
        chunk_index integer NOT NULL CHECK(chunk_index>=0),
        row_count integer NOT NULL CHECK(row_count BETWEEN 1 AND 512),
        payload bytea NOT NULL, digest text NOT NULL CHECK(length(digest)=64),
        created_at timestamptz NOT NULL DEFAULT now(),
        PRIMARY KEY(run_id,kind,chunk_index),
        FOREIGN KEY(run_id,tenant_id,owner_user_id) REFERENCES research_backtests(id,tenant_id,owner_user_id)
      );
      ALTER TABLE research_indicator_chunks ENABLE ROW LEVEL SECURITY;
      ALTER TABLE research_indicator_chunks FORCE ROW LEVEL SECURITY;
      CREATE POLICY owner_scope ON research_indicator_chunks USING(
        tenant_id=gatescope_security.current_tenant_id() AND owner_user_id=gatescope_security.current_subject_id()
      ) WITH CHECK(
        tenant_id=gatescope_security.current_tenant_id() AND owner_user_id=gatescope_security.current_subject_id()
      );
      GRANT SELECT ON research_indicator_chunks TO gatescope_api,gatescope_mcp;
      GRANT SELECT,INSERT ON research_indicator_chunks TO gatescope_strategy;
      CREATE TRIGGER research_indicator_chunks_immutable BEFORE UPDATE OR DELETE ON research_indicator_chunks
        FOR EACH ROW EXECUTE FUNCTION gatescope_security.reject_fact_mutation();
      CREATE OR REPLACE FUNCTION gatescope_security.claim_research_backtest()
        RETURNS TABLE(run_id uuid,tenant uuid,subject uuid)
        LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
        BEGIN
          UPDATE public.research_backtests SET state='FAILED',safe_error='回测超时，请重试',completed_at=now()
            WHERE state='RUNNING' AND started_at<now()-interval '5 minutes' AND bar_limit<=5000
              AND coalesce(spec_json->>'engine_mode','INDICATOR')<>'STRUCTURE';
          RETURN QUERY WITH candidate AS (
            SELECT id FROM public.research_backtests
            WHERE coalesce(spec_json->>'engine_mode','INDICATOR')<>'STRUCTURE'
              AND (state='QUEUED' OR (state='RUNNING' AND bar_limit>5000 AND lease_until<now()))
              AND (lease_until IS NULL OR lease_until<now())
            ORDER BY coalesce(lease_until,created_at),id FOR UPDATE SKIP LOCKED LIMIT 1
          ) UPDATE public.research_backtests r SET state='RUNNING',started_at=coalesce(started_at,now()),
              lease_until=now()+interval '5 minutes'
            FROM candidate c WHERE r.id=c.id RETURNING r.id,r.tenant_id,r.owner_user_id;
        END $$;
    """)


def downgrade() -> None:
    # Evidence must not disappear as a side effect of rolling back application
    # code. Operators can use the previous build against this additive schema.
    connection = op.get_bind()
    if connection.dialect.name != "postgresql":
        return
    from sqlalchemy import text
    database = connection.execute(text("SELECT current_database()")).scalar_one()
    exists = connection.execute(text("SELECT EXISTS(SELECT 1 FROM research_indicator_chunks)")).scalar_one()
    if exists and database != "gatescope_test":
        raise RuntimeError("Annual research evidence requires an explicit export before schema removal")
    op.execute("""
      DROP TABLE research_indicator_chunks;
      ALTER TABLE research_backtests DROP CONSTRAINT research_backtests_owner_identity;
      CREATE OR REPLACE FUNCTION gatescope_security.claim_research_backtest()
        RETURNS TABLE(run_id uuid,tenant uuid,subject uuid)
        LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
        BEGIN
          UPDATE public.research_backtests SET state='FAILED',safe_error='回测超时，请重试',completed_at=now()
            WHERE state='RUNNING' AND started_at<now()-interval '5 minutes'
              AND coalesce(spec_json->>'engine_mode','INDICATOR')<>'STRUCTURE';
          RETURN QUERY WITH candidate AS (
            SELECT id FROM public.research_backtests WHERE state='QUEUED'
              AND coalesce(spec_json->>'engine_mode','INDICATOR')<>'STRUCTURE'
            ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT 1
          ) UPDATE public.research_backtests r SET state='RUNNING',started_at=now()
            FROM candidate c WHERE r.id=c.id RETURNING r.id,r.tenant_id,r.owner_user_id;
        END $$;
    """)
