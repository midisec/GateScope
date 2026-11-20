"""Durable, generation-checked chart snapshots, never a source of trade facts."""
from alembic import op

revision="0029_background_chart_cache"
down_revision="0028_structure_coverage"
branch_labels=depends_on=None


def upgrade():
    if op.get_bind().dialect.name!="postgresql":return
    op.execute("""CREATE TABLE market_chart_snapshots (
        instrument_id uuid NOT NULL REFERENCES instruments(id),timeframe text NOT NULL,
        generation bigint NOT NULL DEFAULT 0,built_generation bigint NOT NULL DEFAULT -1,
        built_at timestamptz,first_open_time timestamptz,last_open_time timestamptz,
        bar_count integer NOT NULL DEFAULT 0 CHECK(bar_count BETWEEN 0 AND 1500),
        payload_json jsonb NOT NULL DEFAULT '{"items":[]}',
        PRIMARY KEY(instrument_id,timeframe),
        CHECK(timeframe IN ('1m','5m','15m','30m','1h','4h','8h','1d'))
    );
    GRANT SELECT ON market_chart_snapshots TO gatescope_api;
    GRANT SELECT,INSERT,UPDATE ON market_chart_snapshots TO gatescope_market;
    CREATE INDEX structure_reference_lookup ON market_structure_stages(context_id,pattern,entry_phase,score_bucket,stage_time);
    INSERT INTO market_structure_stages(context_id,candidate_id,pattern,entry_phase,score_bucket,
        stage_time,phase,resolved_time,sampled,formed,prediction,forecast_json,outcome_json)
    SELECT context_id,candidate_id,event_json->>'pattern',s.key,s.value->>'score_bucket',
        (s.value->>'time')::bigint,phase,(event_json->>'resolved_time')::bigint,
        (s.value->>'sampled')::boolean,(event_json->'stages') ? 'FORMED',
        (s.value->'prediction'->>'probability')::double precision,event_json->'first'->'forecast',event_json->'forecast_outcome'
    FROM (SELECT DISTINCT ON(context_id,candidate_id) context_id,candidate_id,event_json,phase
        FROM market_pattern_events WHERE source_mode='RECORDED'
        ORDER BY context_id,candidate_id,sequence DESC) latest,
        LATERAL jsonb_each(event_json->'stages') s
    ON CONFLICT DO NOTHING;
    """)


def downgrade():
    if op.get_bind().dialect.name=="postgresql":op.execute("DROP TABLE market_chart_snapshots; DROP INDEX structure_reference_lookup")
