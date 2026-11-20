"""Owner-isolated shared-cash portfolios referencing immutable source studies."""
from alembic import op

revision="0050_shared_capital_portfolio"
down_revision="0049_annual_indicator_research"
branch_labels=None
depends_on=None


def upgrade() -> None:
    if op.get_bind().dialect.name!="postgresql":
        return
    op.execute("""
      CREATE TABLE research_portfolios(
        id uuid PRIMARY KEY,tenant_id uuid NOT NULL REFERENCES tenants(id),owner_user_id uuid NOT NULL REFERENCES users(id),
        source_run_ids uuid[] NOT NULL,policy_json jsonb NOT NULL,from_time timestamptz,to_time timestamptz,
        source_manifest jsonb NOT NULL,state text NOT NULL CHECK(state IN ('QUEUED','RUNNING','SUCCEEDED','FAILED')),
        safe_error text,progress_json jsonb NOT NULL DEFAULT '{}',report_json jsonb,
        created_at timestamptz NOT NULL DEFAULT now(),started_at timestamptz,completed_at timestamptz,lease_until timestamptz,
        UNIQUE(id,tenant_id,owner_user_id)
      );
      CREATE UNIQUE INDEX research_portfolio_active ON research_portfolios(tenant_id,owner_user_id) WHERE state IN ('QUEUED','RUNNING');
      CREATE INDEX research_portfolio_history ON research_portfolios(tenant_id,owner_user_id,created_at DESC,id DESC);
      CREATE TABLE research_portfolio_chunks(
        run_id uuid NOT NULL,tenant_id uuid NOT NULL,owner_user_id uuid NOT NULL,
        kind text NOT NULL CHECK(kind IN ('trades','decisions','curve')),chunk_index integer NOT NULL CHECK(chunk_index>=0),
        row_count integer NOT NULL CHECK(row_count BETWEEN 1 AND 512),payload bytea NOT NULL,
        digest text NOT NULL CHECK(length(digest)=64),PRIMARY KEY(run_id,kind,chunk_index),
        FOREIGN KEY(run_id,tenant_id,owner_user_id) REFERENCES research_portfolios(id,tenant_id,owner_user_id)
      );
      CREATE TRIGGER research_portfolio_chunk_immutable BEFORE UPDATE OR DELETE ON research_portfolio_chunks
        FOR EACH ROW EXECUTE FUNCTION gatescope_security.reject_fact_mutation();
      CREATE FUNCTION gatescope_security.portfolio_freeze_guard() RETURNS trigger
        LANGUAGE plpgsql SET search_path=pg_catalog,public AS $$
        BEGIN
          IF OLD.state IN ('SUCCEEDED','FAILED') OR OLD.id<>NEW.id OR OLD.tenant_id<>NEW.tenant_id OR
             OLD.owner_user_id<>NEW.owner_user_id OR OLD.source_run_ids<>NEW.source_run_ids OR
             OLD.policy_json<>NEW.policy_json OR OLD.source_manifest<>NEW.source_manifest OR
             OLD.from_time IS DISTINCT FROM NEW.from_time OR OLD.to_time IS DISTINCT FROM NEW.to_time THEN
            RAISE EXCEPTION 'portfolio source evidence is immutable' USING ERRCODE='55000';
          END IF;
          RETURN NEW;
        END $$;
      CREATE TRIGGER research_portfolio_frozen BEFORE UPDATE ON research_portfolios
        FOR EACH ROW EXECUTE FUNCTION gatescope_security.portfolio_freeze_guard();
      CREATE FUNCTION gatescope_security.claim_research_portfolio()
        RETURNS TABLE(run_id uuid,tenant uuid,subject uuid)
        LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
        BEGIN
          RETURN QUERY WITH candidate AS(
            SELECT id FROM public.research_portfolios WHERE state IN ('QUEUED','RUNNING')
              AND (lease_until IS NULL OR lease_until<now()) ORDER BY coalesce(lease_until,created_at),id
              FOR UPDATE SKIP LOCKED LIMIT 1)
            UPDATE public.research_portfolios r SET state='RUNNING',started_at=coalesce(started_at,now()),
              lease_until=now()+interval '5 minutes' FROM candidate c WHERE r.id=c.id
              RETURNING r.id,r.tenant_id,r.owner_user_id;
        END $$;
      REVOKE ALL ON FUNCTION gatescope_security.claim_research_portfolio() FROM PUBLIC;
      GRANT EXECUTE ON FUNCTION gatescope_security.claim_research_portfolio() TO gatescope_strategy;
      CREATE FUNCTION gatescope_security.completed_structure_insert_guard() RETURNS trigger
        LANGUAGE plpgsql SET search_path=pg_catalog,public AS $$
        BEGIN
          -- The replay writer already holds this parent lock. Other writers
          -- must wait for its final report before checking the immutable seal.
          PERFORM 1 FROM public.research_backtests WHERE id=NEW.run_id AND tenant_id=NEW.tenant_id
            AND owner_user_id=NEW.owner_user_id FOR UPDATE;
          IF NOT FOUND THEN
            RAISE EXCEPTION 'research source owner mismatch' USING ERRCODE='23503';
          END IF;
          IF EXISTS(SELECT 1 FROM public.research_backtest_results WHERE run_id=NEW.run_id) THEN
            RAISE EXCEPTION 'completed research source is sealed' USING ERRCODE='55000';
          END IF;
          RETURN NEW;
        END $$;
    """)
    for table in ("research_structure_inputs","research_structure_events","research_structure_cases"):
        op.execute(f"CREATE TRIGGER completed_structure_seal BEFORE INSERT ON {table} FOR EACH ROW EXECUTE FUNCTION gatescope_security.completed_structure_insert_guard();")
    for table in ("research_portfolios","research_portfolio_chunks"):
        op.execute(f"""ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;
          ALTER TABLE {table} FORCE ROW LEVEL SECURITY;
          CREATE POLICY owner_scope ON {table} USING(
            tenant_id=gatescope_security.current_tenant_id() AND owner_user_id=gatescope_security.current_subject_id())
          WITH CHECK(tenant_id=gatescope_security.current_tenant_id() AND owner_user_id=gatescope_security.current_subject_id());
          GRANT SELECT ON {table} TO gatescope_api,gatescope_strategy;""")
    op.execute("""GRANT INSERT ON research_portfolios TO gatescope_api;
      GRANT UPDATE ON research_portfolios TO gatescope_strategy;
      GRANT INSERT ON research_portfolio_chunks TO gatescope_strategy;""")


def downgrade() -> None:
    if op.get_bind().dialect.name!="postgresql":
        return
    from sqlalchemy import text
    connection=op.get_bind()
    if connection.execute(text("SELECT current_database()")).scalar_one()!="gatescope_test" and connection.execute(text("SELECT EXISTS(SELECT 1 FROM research_portfolios)")).scalar_one():
        raise RuntimeError("Portfolio evidence requires export before schema removal")
    for table in ("research_structure_inputs","research_structure_events","research_structure_cases"):
        op.execute(f"DROP TRIGGER completed_structure_seal ON {table};")
    op.execute("""DROP FUNCTION gatescope_security.completed_structure_insert_guard();
      DROP FUNCTION gatescope_security.claim_research_portfolio();
      DROP TABLE research_portfolio_chunks,research_portfolios;
      DROP FUNCTION gatescope_security.portfolio_freeze_guard();""")
