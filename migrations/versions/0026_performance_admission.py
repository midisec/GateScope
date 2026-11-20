"""Four strict, numeric deployment prerequisites, preserving old certificates."""
from alembic import op
from sqlalchemy import text

revision = "0026_performance_admission"
down_revision = "0025_annual_structure_history"
branch_labels = depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    if conn.dialect.name != "postgresql":
        return
    # Old immutable facts remain readable, but cannot authorize new drills.
    constraints = conn.execute(text("SELECT conname FROM pg_constraint WHERE conrelid='strategy_execution_certifications'::regclass AND pg_get_constraintdef(oid) LIKE '%jsonb_array_length(gate_report_json)%'")).scalars().all()
    for name in constraints:
        op.drop_constraint(name, "strategy_execution_certifications", type_="check")
    op.execute("ALTER TABLE strategy_execution_certifications ADD CONSTRAINT certification_gate_count CHECK (jsonb_typeof(gate_report_json)='array' AND jsonb_array_length(gate_report_json) IN (19,23))")
    definition = conn.execute(text("SELECT pg_get_functiondef('gatescope_security.guard_strategy_certification_writer()'::regprocedure)")).scalar_one()
    old = "'DATA_QUALITY','OUT_OF_DISTRIBUTION','COST_MODEL'"
    assert old in definition
    definition = definition.replace(old, old + ",'OOS_SHARPE','OOS_DRAWDOWN','OOS_HIT_RATE','OOS_T_STAT'")
    op.execute(text(definition))
    op.execute("""
      CREATE FUNCTION gatescope_security.guard_performance_thresholds() RETURNS trigger
      LANGUAGE plpgsql AS $$
      DECLARE item jsonb; code text; value numeric; expected boolean;
      BEGIN
        FOR item IN SELECT x FROM jsonb_array_elements(NEW.gate_report_json) x LOOP
          code := item->>'gate_code';
          IF code IN ('OOS_SHARPE','OOS_DRAWDOWN','OOS_HIT_RATE','OOS_T_STAT') THEN
            value := CASE WHEN jsonb_typeof(item->'value'->'metric')='number'
                          THEN (item->'value'->>'metric')::numeric ELSE NULL END;
            expected := coalesce(value IS NOT NULL
              AND jsonb_typeof(item->'value'->'daily_observations')='number'
              AND jsonb_typeof(item->'value'->'trade_count')='number'
              AND (item->'value'->>'daily_observations')::numeric >= 90
              AND (item->'value'->>'trade_count')::numeric >= 30
              AND CASE code WHEN 'OOS_SHARPE' THEN value>1.5
                WHEN 'OOS_DRAWDOWN' THEN value>=0 AND value<0.15
                WHEN 'OOS_HIT_RATE' THEN value>0.55 AND value<=1
                WHEN 'OOS_T_STAT' THEN value>2.0 ELSE false END, false);
            IF (item->>'result'='PASS') IS DISTINCT FROM expected THEN
              RAISE EXCEPTION 'performance metric does not meet fixed thresholds' USING ERRCODE='23514';
            END IF;
          END IF;
        END LOOP;
        RETURN NEW;
      END $$;
      REVOKE ALL ON FUNCTION gatescope_security.guard_performance_thresholds() FROM PUBLIC;
      CREATE TRIGGER certification_performance_guard BEFORE INSERT ON strategy_execution_certifications
        FOR EACH ROW EXECUTE FUNCTION gatescope_security.guard_performance_thresholds();
    """)
    definition = conn.execute(text("SELECT pg_get_functiondef('gatescope_security.guard_testnet_drill_admission()'::regprocedure)")).scalar_one()
    old = "certification.evidence_tier='LIVE_ELIGIBLE'"
    assert old in definition
    op.execute(text(definition.replace(old, old + " AND jsonb_array_length(certification.gate_report_json)=23")))


def downgrade() -> None:
    conn = op.get_bind()
    if conn.dialect.name != "postgresql":
        return
    op.execute("DROP TRIGGER certification_performance_guard ON strategy_execution_certifications; DROP FUNCTION gatescope_security.guard_performance_thresholds()")
    # Retain the backwards-readable count constraint; never delete immutable facts.
    for function in ("guard_strategy_certification_writer", "guard_testnet_drill_admission"):
        definition = conn.execute(text(f"SELECT pg_get_functiondef('gatescope_security.{function}()'::regprocedure)")).scalar_one()
        definition = definition.replace(",'OOS_SHARPE','OOS_DRAWDOWN','OOS_HIT_RATE','OOS_T_STAT'", "")
        definition = definition.replace(" AND jsonb_array_length(certification.gate_report_json)=23", "")
        op.execute(text(definition))
