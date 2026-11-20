"""Shared public-market structure research cache; no user or trade data."""
from alembic import op

revision = "0022_pattern_studies"
down_revision = "0021_market_cache_sync"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("""
      CREATE TABLE market_pattern_studies (
        instrument_id uuid NOT NULL REFERENCES instruments(id),
        timeframe text NOT NULL CHECK (timeframe IN ('1m','5m','15m','30m','1h','4h','8h','1d')),
        version text NOT NULL, state text NOT NULL CHECK (state IN ('QUEUED','RUNNING','READY','FAILED')),
        requested_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
        cursor integer NOT NULL DEFAULT 0, total integer NOT NULL DEFAULT 0,
        input_json jsonb, checkpoint_json jsonb, report_json jsonb, safe_error text,
        PRIMARY KEY (instrument_id,timeframe)
      );
      GRANT SELECT ON market_pattern_studies TO gatescope_api;
      GRANT SELECT,UPDATE ON market_pattern_studies TO gatescope_strategy;
      CREATE FUNCTION gatescope_security.request_pattern_study(instrument uuid, period text, model_version text)
        RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
      BEGIN
        IF period NOT IN ('1m','5m','15m','30m','1h','4h','8h','1d') OR length(model_version)>60 THEN
          RAISE EXCEPTION 'invalid study' USING ERRCODE='22023';
        END IF;
        PERFORM pg_advisory_xact_lock(7342281);
        IF EXISTS (SELECT 1 FROM public.market_pattern_studies WHERE instrument_id=instrument AND timeframe=period
          AND version=model_version AND (state IN ('QUEUED','RUNNING') OR requested_at>now()-interval '5 minutes')) THEN
          RETURN;
        END IF;
        IF (SELECT count(*) FROM public.market_pattern_studies WHERE state IN ('QUEUED','RUNNING'))>=8 THEN
          RAISE EXCEPTION 'study queue full' USING ERRCODE='53300';
        END IF;
        INSERT INTO public.market_pattern_studies(instrument_id,timeframe,version,state)
          VALUES (instrument,period,model_version,'QUEUED')
        ON CONFLICT (instrument_id,timeframe) DO UPDATE SET version=EXCLUDED.version,state='QUEUED',
          requested_at=now(),updated_at=now(),cursor=0,total=0,input_json=NULL,checkpoint_json=NULL,safe_error=NULL;
      END $$;
      REVOKE ALL ON FUNCTION gatescope_security.request_pattern_study(uuid,text,text) FROM PUBLIC;
      GRANT EXECUTE ON FUNCTION gatescope_security.request_pattern_study(uuid,text,text) TO gatescope_api;
    """)


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP FUNCTION gatescope_security.request_pattern_study(uuid,text,text); DROP TABLE market_pattern_studies")
