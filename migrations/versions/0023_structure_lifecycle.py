"""Immutable structure decisions, report archive and bounded live checkpoints."""
from alembic import op

revision = "0023_structure_lifecycle"
down_revision = "0022_pattern_studies"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("""
      CREATE TABLE market_pattern_events (
        context_id text NOT NULL, candidate_id text NOT NULL, sequence integer NOT NULL CHECK(sequence>0),
        instrument_id uuid NOT NULL REFERENCES instruments(id), timeframe text NOT NULL, version text NOT NULL,
        source_mode text NOT NULL CHECK(source_mode IN ('REPLAY','RECORDED')),
        phase text NOT NULL CHECK(phase IN ('FORMING','FORMED','CONFIRMED','INVALIDATED','EXPIRED','CENSORED')),
        from_phase text, observed_time timestamptz NOT NULL, recorded_at timestamptz NOT NULL DEFAULT now(),
        event_json jsonb NOT NULL, PRIMARY KEY(context_id,candidate_id,sequence)
      );
      CREATE INDEX pattern_events_lookup ON market_pattern_events(instrument_id,timeframe,version,context_id,candidate_id,sequence DESC);
      CREATE TRIGGER pattern_events_immutable BEFORE UPDATE OR DELETE ON market_pattern_events
        FOR EACH ROW EXECUTE FUNCTION gatescope_security.reject_fact_mutation();
      CREATE TABLE market_pattern_reports (
        context_id text PRIMARY KEY, instrument_id uuid NOT NULL REFERENCES instruments(id), timeframe text NOT NULL,
        version text NOT NULL, report_json jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT now()
      );
      INSERT INTO market_pattern_reports SELECT 'legacy:'||instrument_id||':'||timeframe||':'||requested_at,
        instrument_id,timeframe,COALESCE(report_json->>'version',version),report_json,now()
        FROM market_pattern_studies WHERE report_json IS NOT NULL;
      CREATE TRIGGER pattern_reports_immutable BEFORE UPDATE OR DELETE ON market_pattern_reports
        FOR EACH ROW EXECUTE FUNCTION gatescope_security.reject_fact_mutation();
      CREATE TABLE market_pattern_tracking (
        instrument_id uuid NOT NULL REFERENCES instruments(id), timeframe text NOT NULL
          CHECK(timeframe IN ('1m','5m','15m','30m','1h','4h','8h','1d')),
        version text NOT NULL, checkpoint_json jsonb NOT NULL DEFAULT '{}',
        updated_at timestamptz NOT NULL DEFAULT 'epoch', requested_at timestamptz NOT NULL DEFAULT now(),
        safe_error text, PRIMARY KEY(instrument_id,timeframe,version)
      );
      GRANT SELECT ON market_pattern_events,market_pattern_reports,market_pattern_tracking TO gatescope_api;
      GRANT SELECT,INSERT ON market_pattern_events,market_pattern_reports TO gatescope_strategy;
      GRANT SELECT,INSERT,UPDATE ON market_pattern_tracking TO gatescope_strategy;
      GRANT SELECT ON market_sync_state TO gatescope_strategy;
      CREATE FUNCTION gatescope_security.request_pattern_tracking(instrument uuid,period text,model_version text)
        RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
      BEGIN
        IF period NOT IN ('1m','5m','15m','30m','1h','4h','8h','1d') OR model_version<>'structure-study-2' THEN
          RAISE EXCEPTION 'invalid tracking' USING ERRCODE='22023';
        END IF;
        PERFORM pg_advisory_xact_lock(7342283);
        IF EXISTS(SELECT 1 FROM public.market_pattern_tracking WHERE instrument_id=instrument AND timeframe=period AND version=model_version) THEN
          UPDATE public.market_pattern_tracking SET requested_at=now() WHERE instrument_id=instrument AND timeframe=period AND version=model_version;
          RETURN;
        END IF;
        IF (SELECT count(*) FROM public.market_pattern_tracking WHERE requested_at>now()-interval '24 hours')>=64 THEN
          RAISE EXCEPTION 'tracking queue full' USING ERRCODE='53300';
        END IF;
        INSERT INTO public.market_pattern_tracking(instrument_id,timeframe,version) VALUES(instrument,period,model_version);
      END $$;
      REVOKE ALL ON FUNCTION gatescope_security.request_pattern_tracking(uuid,text,text) FROM PUBLIC;
      GRANT EXECUTE ON FUNCTION gatescope_security.request_pattern_tracking(uuid,text,text) TO gatescope_api;
    """)


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP FUNCTION gatescope_security.request_pattern_tracking(uuid,text,text); DROP TABLE market_pattern_tracking,market_pattern_reports,market_pattern_events")
