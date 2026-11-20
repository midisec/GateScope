"""Owner-isolated, resumable same-engine structural research; no execution grant."""
from alembic import op

revision = "0037_structure_research"
down_revision = "0036_signal_notice"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("""
      ALTER TABLE research_backtests DROP CONSTRAINT research_backtests_bar_limit_check;
      ALTER TABLE research_backtests ADD CONSTRAINT research_backtests_bar_limit_check
        CHECK (bar_limit BETWEEN 300 AND 550000 AND
          (coalesce(spec_json->>'engine_mode','INDICATOR')='STRUCTURE' OR bar_limit<=5000));
      ALTER TABLE research_backtests ADD COLUMN progress_json jsonb NOT NULL DEFAULT '{}';
      ALTER TABLE research_backtests ADD COLUMN lease_until timestamptz;
      CREATE TABLE research_structure_checkpoints (
        run_id uuid PRIMARY KEY REFERENCES research_backtests(id), tenant_id uuid NOT NULL,
        owner_user_id uuid NOT NULL, checkpoint_json jsonb NOT NULL,
        updated_at timestamptz NOT NULL DEFAULT now()
      );
      CREATE TABLE research_structure_inputs (
        run_id uuid NOT NULL REFERENCES research_backtests(id), tenant_id uuid NOT NULL,
        owner_user_id uuid NOT NULL, bar_index integer NOT NULL, open_time bigint NOT NULL,
        bar_json jsonb NOT NULL, PRIMARY KEY(run_id,bar_index), UNIQUE(run_id,open_time)
      );
      CREATE TABLE research_structure_events (
        run_id uuid NOT NULL REFERENCES research_backtests(id), tenant_id uuid NOT NULL,
        owner_user_id uuid NOT NULL, candidate_id text NOT NULL, sequence integer NOT NULL,
        observed_time bigint NOT NULL, phase text NOT NULL, pattern text NOT NULL,
        event_json jsonb NOT NULL, PRIMARY KEY(run_id,candidate_id,sequence)
      );
      CREATE INDEX research_structure_events_page ON research_structure_events(run_id,observed_time,candidate_id,sequence);
      CREATE TABLE research_structure_cases (
        run_id uuid NOT NULL REFERENCES research_backtests(id), tenant_id uuid NOT NULL,
        owner_user_id uuid NOT NULL, candidate_id text NOT NULL, pattern text NOT NULL,
        observed_time bigint NOT NULL, selected boolean NOT NULL, baseline boolean NOT NULL,
        selection_reasons jsonb NOT NULL, environment jsonb NOT NULL, state text NOT NULL,
        entry_time bigint, exit_time bigint, net_return double precision, net_r double precision,
        fees double precision, funding double precision, case_json jsonb NOT NULL,
        PRIMARY KEY(run_id,candidate_id)
      );
      CREATE INDEX research_structure_cases_pending ON research_structure_cases(run_id,state);
      CREATE INDEX research_structure_cases_page ON research_structure_cases(run_id,observed_time,candidate_id);
      CREATE TRIGGER research_structure_input_immutable BEFORE UPDATE OR DELETE ON research_structure_inputs
        FOR EACH ROW EXECUTE FUNCTION gatescope_security.reject_fact_mutation();
      CREATE TRIGGER research_structure_event_immutable BEFORE UPDATE OR DELETE ON research_structure_events
        FOR EACH ROW EXECUTE FUNCTION gatescope_security.reject_fact_mutation();
      CREATE FUNCTION gatescope_security.structure_research_case_guard() RETURNS trigger
        LANGUAGE plpgsql SET search_path=pg_catalog,public AS $$
        BEGIN
          IF EXISTS(SELECT 1 FROM public.research_backtest_results WHERE run_id=OLD.run_id) THEN
            RAISE EXCEPTION 'completed research cases are immutable' USING ERRCODE='55000';
          END IF;
          RETURN NEW;
        END $$;
      CREATE TRIGGER research_structure_case_frozen BEFORE UPDATE ON research_structure_cases
        FOR EACH ROW EXECUTE FUNCTION gatescope_security.structure_research_case_guard();
    """)
    for table in ("research_structure_checkpoints", "research_structure_inputs", "research_structure_events", "research_structure_cases"):
        op.execute(f"""
          ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;
          ALTER TABLE {table} FORCE ROW LEVEL SECURITY;
          CREATE POLICY owner_scope ON {table} USING (
            tenant_id=gatescope_security.current_tenant_id() AND owner_user_id=gatescope_security.current_subject_id()
          ) WITH CHECK (
            tenant_id=gatescope_security.current_tenant_id() AND owner_user_id=gatescope_security.current_subject_id()
          );
          GRANT SELECT ON {table} TO gatescope_api;
          GRANT SELECT,INSERT ON {table} TO gatescope_strategy;
        """)
    op.execute("""
      GRANT UPDATE ON research_structure_checkpoints,research_structure_cases TO gatescope_strategy;
      CREATE OR REPLACE FUNCTION gatescope_security.claim_research_backtest()
        RETURNS TABLE(run_id uuid, tenant uuid, subject uuid)
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
      CREATE FUNCTION gatescope_security.claim_structure_research()
        RETURNS TABLE(run_id uuid, tenant uuid, subject uuid)
        LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
        BEGIN
          RETURN QUERY WITH candidate AS (
            SELECT id FROM public.research_backtests
            WHERE state IN ('QUEUED','RUNNING') AND spec_json->>'engine_mode'='STRUCTURE'
              AND (lease_until IS NULL OR lease_until<now())
            ORDER BY coalesce(lease_until,created_at),id FOR UPDATE SKIP LOCKED LIMIT 1
          ) UPDATE public.research_backtests r SET state='RUNNING',started_at=coalesce(started_at,now()),
              lease_until=now()+interval '2 minutes'
            FROM candidate c WHERE r.id=c.id RETURNING r.id,r.tenant_id,r.owner_user_id;
        END $$;
      REVOKE ALL ON FUNCTION gatescope_security.claim_structure_research() FROM PUBLIC;
      GRANT EXECUTE ON FUNCTION gatescope_security.claim_structure_research() TO gatescope_strategy;
    """)


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("""
      DROP FUNCTION gatescope_security.claim_structure_research();
      DROP TABLE research_structure_cases,research_structure_events,research_structure_inputs,research_structure_checkpoints;
      DROP FUNCTION gatescope_security.structure_research_case_guard();
      ALTER TABLE research_backtests DROP COLUMN progress_json, DROP COLUMN lease_until;
      -- Long completed requests are preserved; the original table is removed by 0020 downgrade.
    """)
    op.execute("""
      CREATE OR REPLACE FUNCTION gatescope_security.claim_research_backtest()
        RETURNS TABLE(run_id uuid, tenant uuid, subject uuid)
        LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
        BEGIN
          UPDATE public.research_backtests SET state='FAILED',safe_error='回测超时，请重试',completed_at=now()
            WHERE state='RUNNING' AND started_at<now()-interval '5 minutes';
          RETURN QUERY WITH candidate AS (SELECT id FROM public.research_backtests WHERE state='QUEUED'
            ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT 1)
          UPDATE public.research_backtests r SET state='RUNNING',started_at=now()
            FROM candidate c WHERE r.id=c.id RETURNING r.id,r.tenant_id,r.owner_user_id;
        END $$;
    """)
