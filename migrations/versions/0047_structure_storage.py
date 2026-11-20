"""Lossless compact structure evidence with immutable searchable projections.

Metadata-only upgrade: no table rewrite or archive mutation during deployment.
Historical repacking is an explicit, resumable privileged maintenance operation.
"""

from alembic import op

revision = "0047_structure_storage"
down_revision = "0046_paper_order_execution"
branch_labels = depends_on = None


def upgrade():
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("""
      ALTER TABLE market_pattern_events ADD COLUMN event_blob bytea;
      ALTER TABLE market_pattern_events ALTER COLUMN event_blob SET STORAGE EXTERNAL;
      CREATE FUNCTION gatescope_security.structure_event_record(header jsonb, body bytea)
        RETURNS jsonb LANGUAGE sql IMMUTABLE PARALLEL SAFE
        SET search_path=pg_catalog AS $$
          SELECT CASE WHEN body IS NULL THEN header ELSE header ||
            jsonb_build_object('_archive_data',replace(encode(body,'base64'),E'\\n','')) END
        $$;
      REVOKE ALL ON FUNCTION gatescope_security.structure_event_record(jsonb,bytea) FROM PUBLIC;
      GRANT EXECUTE ON FUNCTION gatescope_security.structure_event_record(jsonb,bytea)
        TO gatescope_api,gatescope_strategy,gatescope_mcp,gatescope_notification,gatescope_paper;
      CREATE TABLE gatescope_security.structure_storage_progress (
        job text PRIMARY KEY, cursor_json jsonb, scanned bigint NOT NULL DEFAULT 0,
        packed bigint NOT NULL DEFAULT 0, estimated_saved_bytes bigint NOT NULL DEFAULT 0,
        updated_at timestamptz NOT NULL DEFAULT now()
      );
      CREATE TABLE gatescope_security.structure_storage_audit (
        id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,job text NOT NULL,
        scanned integer NOT NULL,packed integer NOT NULL,estimated_saved_bytes bigint NOT NULL,
        checked_digest text NOT NULL,repacked_at timestamptz NOT NULL DEFAULT now()
      );
      CREATE FUNCTION gatescope_security.guard_structure_event() RETURNS trigger
        LANGUAGE plpgsql SET search_path=pg_catalog,public AS $$
      BEGIN
        IF TG_OP='UPDATE'
          AND current_user=pg_get_userbyid((SELECT relowner FROM pg_class WHERE oid=TG_RELID))
          AND ((current_setting('gatescope.structure_repack',true)='verified-v1'
            AND OLD.event_blob IS NULL AND NEW.event_blob IS NOT NULL)
            OR (current_setting('gatescope.structure_repack',true)='verified-restore-v1'
            AND OLD.event_blob IS NOT NULL AND NEW.event_blob IS NULL))
          AND (to_jsonb(NEW)-'event_json'-'event_blob')=(to_jsonb(OLD)-'event_json'-'event_blob') THEN
          RETURN NEW;
        END IF;
        RAISE EXCEPTION 'immutable structure evidence' USING ERRCODE='42501';
      END $$;
      DROP TRIGGER pattern_events_immutable ON market_pattern_events;
      CREATE TRIGGER pattern_events_immutable BEFORE UPDATE OR DELETE ON market_pattern_events
        FOR EACH ROW EXECUTE FUNCTION gatescope_security.guard_structure_event();
      CREATE FUNCTION gatescope_security.repack_structure_event(ctx text,candidate text,seq integer,
          original jsonb,header jsonb,body bytea) RETURNS boolean
        LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
      DECLARE prior text; changed integer; projection jsonb;
      BEGIN
        IF body IS NULL OR header->'_archive'->>'format' IS DISTINCT FROM 'structure-zlib-1'
          OR COALESCE((header->'_archive'->>'sha256') !~ '^[a-f0-9]{64}$',true)
          OR COALESCE((header->'_archive'->>'raw_bytes')::integer NOT BETWEEN 1 AND 4194304,true) THEN
          RAISE EXCEPTION 'invalid archive representation' USING ERRCODE='22023';
        END IF;
        SELECT COALESCE(jsonb_object_agg(key,value),'{}') INTO projection FROM jsonb_each(original)
          WHERE key=ANY(ARRAY['id','pattern','phase','sequence','reason','observed_time',
            'first_observed_time','deadline_time','snapshot_at','first_snapshot_at','source_mode']);
        projection=projection || jsonb_build_object('latest',
          (SELECT COALESCE(jsonb_object_agg(key,value),'{}') FROM jsonb_each(COALESCE(original->'latest','{}'))
            WHERE key=ANY(ARRAY['name','stage','evidence_id'])));
        IF header-'_archive'<>projection THEN
          RAISE EXCEPTION 'archive projection changed' USING ERRCODE='22023';
        END IF;
        prior=current_setting('gatescope.structure_repack',true);
        PERFORM set_config('gatescope.structure_repack','verified-v1',true);
        UPDATE public.market_pattern_events SET event_json=header,event_blob=body
          WHERE context_id=ctx AND candidate_id=candidate AND sequence=seq
            AND event_blob IS NULL AND event_json=original;
        GET DIAGNOSTICS changed=ROW_COUNT;
        PERFORM set_config('gatescope.structure_repack',COALESCE(prior,''),true);
        RETURN changed=1;
      END $$;
      REVOKE ALL ON FUNCTION gatescope_security.repack_structure_event(text,text,integer,jsonb,jsonb,bytea) FROM PUBLIC;
    """)


def downgrade():
    if op.get_bind().dialect.name != "postgresql":
        return
    # Explicit downgrade is lossless but can be expensive: reserve sufficient
    # space and maintenance time. Never discard packed evidence with a column.
    import json
    import hashlib
    from gatescope.quant.structure_archive_codec import unpack_structure_record, canonical_record

    connection = op.get_bind()
    op.execute("SELECT set_config('gatescope.structure_repack','verified-restore-v1',true)")
    while True:
        rows = connection.exec_driver_sql("""SELECT context_id,candidate_id,sequence,
          gatescope_security.structure_event_record(event_json,event_blob)
          FROM market_pattern_events WHERE event_blob IS NOT NULL LIMIT 256""").fetchall()
        if not rows:
            break
        for context, candidate, sequence, value in rows:
            original = unpack_structure_record(value)
            connection.exec_driver_sql(
                """UPDATE market_pattern_events SET event_json=%s::jsonb,event_blob=NULL
              WHERE context_id=%s AND candidate_id=%s AND sequence=%s""",
                (json.dumps(original, ensure_ascii=False, allow_nan=False), context, candidate, sequence),
            )
    op.execute("SELECT set_config('gatescope.structure_repack','',true)")
    while True:
        rows = connection.exec_driver_sql("""SELECT s.context_id,s.candidate_id,s.entry_phase,
            s.forecast_json->'_evidence'->>'sha256',e.event_json->'first'->'forecast'
          FROM market_structure_stages s LEFT JOIN market_pattern_events e
            ON e.context_id=s.context_id AND e.candidate_id=s.candidate_id
            AND e.sequence=(s.forecast_json->'_evidence'->>'sequence')::integer
          WHERE s.forecast_json ? '_evidence' LIMIT 256""").fetchall()
        if not rows:
            break
        for context, candidate, phase, expected, value in rows:
            if value is None or hashlib.sha256(canonical_record(value)).hexdigest() != expected:
                raise ValueError("STAGE_ARCHIVE_RESTORE_INTEGRITY_FAILED")
            connection.exec_driver_sql(
                """UPDATE market_structure_stages SET forecast_json=%s::jsonb
              WHERE context_id=%s AND candidate_id=%s AND entry_phase=%s""",
                (json.dumps(value, ensure_ascii=False, allow_nan=False), context, candidate, phase),
            )
    op.execute("""
      DROP FUNCTION gatescope_security.repack_structure_event(text,text,integer,jsonb,jsonb,bytea);
      DROP TABLE gatescope_security.structure_storage_audit,gatescope_security.structure_storage_progress;
      DROP TRIGGER pattern_events_immutable ON market_pattern_events;
      DROP FUNCTION gatescope_security.guard_structure_event();
      CREATE TRIGGER pattern_events_immutable BEFORE UPDATE OR DELETE ON market_pattern_events
        FOR EACH ROW EXECUTE FUNCTION gatescope_security.reject_fact_mutation();
      DROP FUNCTION gatescope_security.structure_event_record(jsonb,bytea);
      ALTER TABLE market_pattern_events DROP COLUMN event_blob;
    """)
