"""Year-long public cache, deferred source holes, bounded research projections."""
from alembic import op

revision = "0025_annual_structure_history"
down_revision = "0024_structure_rule_revision"
branch_labels = None
depends_on = None


def upgrade():
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("""
      ALTER TABLE market_sync_state DROP CONSTRAINT market_sync_state_target_bars_check;
      ALTER TABLE market_sync_state ADD CHECK(target_bars BETWEEN 1 AND 525600);
      ALTER TABLE market_sync_state ADD COLUMN target_from timestamptz, ADD COLUMN target_to timestamptz;
      UPDATE market_sync_state SET next_history_at=now(),history_status='PENDING';
      CREATE TABLE market_history_deferred (
        instrument_id uuid NOT NULL REFERENCES instruments(id), timeframe text NOT NULL,
        from_time bigint NOT NULL, to_time bigint NOT NULL, retry_at timestamptz NOT NULL,
        reason text NOT NULL, CHECK(to_time>=from_time),
        PRIMARY KEY(instrument_id,timeframe,from_time,to_time)
      );
      GRANT SELECT ON market_history_deferred TO gatescope_api,gatescope_strategy;
      GRANT SELECT,INSERT,UPDATE,DELETE ON market_history_deferred TO gatescope_market;
      GRANT INSERT ON market_pattern_studies TO gatescope_strategy;
      CREATE TABLE market_structure_stages (
        context_id text NOT NULL, candidate_id text NOT NULL, pattern text NOT NULL,
        entry_phase text NOT NULL, score_bucket text NOT NULL, stage_time bigint NOT NULL,
        phase text NOT NULL, resolved_time bigint, sampled boolean NOT NULL, formed boolean NOT NULL,
        prediction double precision, forecast_json jsonb, outcome_json jsonb,
        PRIMARY KEY(context_id,candidate_id,entry_phase)
      );
      CREATE INDEX structure_stage_calibration ON market_structure_stages(context_id,pattern,entry_phase,score_bucket,resolved_time);
      CREATE TABLE market_structure_returns (
        context_id text NOT NULL, evidence_id text NOT NULL, pattern text NOT NULL, phase text NOT NULL,
        horizon integer NOT NULL, observed_time bigint NOT NULL, status text NOT NULL,
        entry_time bigint, exit_time bigint, side integer NOT NULL,
        net_return double precision, mfe double precision, mae double precision,
        PRIMARY KEY(context_id,evidence_id,horizon)
      );
      CREATE INDEX structure_returns_summary ON market_structure_returns(context_id,observed_time);
      GRANT SELECT ON market_structure_stages,market_structure_returns TO gatescope_api;
      GRANT SELECT,INSERT,UPDATE ON market_structure_stages,market_structure_returns TO gatescope_strategy;
      CREATE TABLE market_pattern_report_versions (
        context_id text NOT NULL, snapshot_at text NOT NULL, report_json jsonb NOT NULL,
        created_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY(context_id,snapshot_at)
      );
      CREATE TRIGGER pattern_report_versions_immutable BEFORE UPDATE OR DELETE ON market_pattern_report_versions
        FOR EACH ROW EXECUTE FUNCTION gatescope_security.reject_fact_mutation();
      GRANT SELECT ON market_pattern_report_versions TO gatescope_api;
      GRANT SELECT,INSERT ON market_pattern_report_versions TO gatescope_strategy;
    """)
    definition=op.get_bind().exec_driver_sql("SELECT pg_get_functiondef('gatescope_security.request_pattern_tracking(uuid,text,text)'::regprocedure)").scalar_one()
    old="model_version NOT IN ('structure-study-2','structure-study-2.1')"
    if old not in definition:raise RuntimeError("Unexpected tracking function")
    op.execute(definition.replace(old,"model_version NOT IN ('structure-study-2','structure-study-2.1','structure-study-3')"))


def downgrade():
    if op.get_bind().dialect.name != "postgresql":return
    definition=op.get_bind().exec_driver_sql("SELECT pg_get_functiondef('gatescope_security.request_pattern_tracking(uuid,text,text)'::regprocedure)").scalar_one()
    op.execute(definition.replace("model_version NOT IN ('structure-study-2','structure-study-2.1','structure-study-3')",
        "model_version NOT IN ('structure-study-2','structure-study-2.1')"))
    op.execute("""
      DROP TABLE market_pattern_report_versions,market_structure_returns,market_structure_stages,market_history_deferred;
      ALTER TABLE market_sync_state DROP CONSTRAINT market_sync_state_target_bars_check;
      UPDATE market_sync_state SET target_bars=8000;
      ALTER TABLE market_sync_state ADD CHECK(target_bars BETWEEN 5000 AND 9000);
      ALTER TABLE market_sync_state DROP COLUMN target_from,DROP COLUMN target_to;
      REVOKE INSERT ON market_pattern_studies FROM gatescope_strategy;
    """)
