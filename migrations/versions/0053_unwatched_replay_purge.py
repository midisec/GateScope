"""Owner-only, reference-protected purge of unwatched public REPLAY contexts.

No production rows are deleted by upgrade. Bars, RECORDED and private ledgers
are never deletion targets. Guards remain enabled; partially purged context IDs
remain tombstones so an in-flight reader cannot later create a dangling link.
"""
from alembic import op

revision = "0053_unwatched_replay_purge"
down_revision = "0052_admin_membership_grants"
branch_labels = depends_on = None

EVENT_BRANCH = """        IF TG_OP='DELETE' AND gatescope_security.replay_purge_row_allowed(TG_RELID,jsonb_build_object(
          'context_id',OLD.context_id,'instrument_id',OLD.instrument_id,'source_mode',OLD.source_mode)) THEN
          RETURN OLD;
        END IF;
"""
REFERENCE_TABLES = ("alert_event_facts", "pattern_event_facts", "shadow_trade_facts", "paper_decision_snapshots", "coinglass_pattern_reviews")
SOURCE_TABLES = ("market_pattern_events", "market_pattern_reports", "market_pattern_report_versions", "market_structure_stages", "market_structure_returns")


def upgrade():
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("SET LOCAL lock_timeout='1s'; SET LOCAL statement_timeout='3s'")
    op.execute("""
      CREATE TABLE gatescope_security.replay_purge_jobs (
        id uuid PRIMARY KEY, instrument_id uuid NOT NULL REFERENCES public.instruments(id),
        status text NOT NULL CHECK(status IN ('OPEN','PAUSED','DONE','FAILED')),
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        updated_at timestamptz NOT NULL DEFAULT clock_timestamp(), expires_at timestamptz NOT NULL,
        deleted_rows bigint NOT NULL DEFAULT 0
      );
      CREATE TABLE gatescope_security.replay_purge_contexts (
        context_id text PRIMARY KEY CHECK(context_id ~ '^[a-f0-9]{64}$'),
        job_id uuid NOT NULL REFERENCES gatescope_security.replay_purge_jobs(id),
        instrument_id uuid NOT NULL, timeframe text NOT NULL, version text NOT NULL,
        phase text NOT NULL DEFAULT 'stages' CHECK(phase IN ('stages','returns','versions','events','reports','done')),
        state text NOT NULL DEFAULT 'GUARDED' CHECK(state IN ('GUARDED','PARTIAL','PURGED','RETAINED')),
        deleted_rows bigint NOT NULL DEFAULT 0
      );
      CREATE INDEX replay_purge_job_context ON gatescope_security.replay_purge_contexts(job_id,context_id);
      REVOKE ALL ON gatescope_security.replay_purge_jobs,gatescope_security.replay_purge_contexts FROM PUBLIC;

      CREATE FUNCTION gatescope_security.replay_purge_owner() RETURNS void
        LANGUAGE plpgsql SET search_path=pg_catalog,public AS $$
      BEGIN
        IF session_user<>current_user OR current_user<>pg_get_userbyid(
          (SELECT relowner FROM pg_class WHERE oid='gatescope_security.replay_purge_jobs'::regclass))
          OR NOT EXISTS(SELECT 1 FROM pg_roles WHERE rolname=current_user AND (rolsuper OR rolbypassrls)) THEN
          RAISE EXCEPTION 'PURGE_OWNER_REQUIRED' USING ERRCODE='42501';
        END IF;
      END $$;

      CREATE FUNCTION gatescope_security.replay_reference_contexts(kind text,payload jsonb) RETURNS text[]
        LANGUAGE plpgsql SET search_path=pg_catalog,public AS $$
      DECLARE result text[]='{}'; item jsonb; value text; candidates text[];
      BEGIN
        IF jsonb_typeof(payload) IS DISTINCT FROM 'object' THEN
          RAISE EXCEPTION 'PURGE_REFERENCE_UNVERIFIED' USING ERRCODE='55000';
        END IF;
        IF kind='alert_event_facts' THEN
          IF jsonb_typeof(payload->'matched') IS DISTINCT FROM 'array'
            OR jsonb_array_length(payload->'matched') NOT BETWEEN 1 AND 256 THEN
            RAISE EXCEPTION 'PURGE_REFERENCE_UNVERIFIED' USING ERRCODE='55000';
          END IF;
          FOR item IN SELECT * FROM jsonb_array_elements(payload->'matched') LOOP
            candidates=array_remove(ARRAY[item->>'context_id',item#>>'{source_event,context_id}',
              item#>>'{trade_notice,evidence,context_id}'],NULL);
            IF cardinality(candidates)=0 OR EXISTS(SELECT 1 FROM unnest(candidates) v WHERE v !~ '^[a-f0-9]{64}$')
              OR (SELECT count(DISTINCT v) FROM unnest(candidates) v)>1 THEN
              RAISE EXCEPTION 'PURGE_REFERENCE_UNVERIFIED' USING ERRCODE='55000';
            END IF;
            result=array_cat(result,candidates);
          END LOOP;
        ELSIF kind='paper_decision_snapshots' THEN
          IF payload->>'version' IS DISTINCT FROM 'paper-decision-1'
            OR jsonb_typeof(payload#>'{server_evidence,pattern_events}') IS DISTINCT FROM 'array'
            OR jsonb_array_length(payload#>'{server_evidence,pattern_events}')>256 THEN
            RAISE EXCEPTION 'PURGE_REFERENCE_UNVERIFIED' USING ERRCODE='55000';
          END IF;
          FOR item IN SELECT * FROM jsonb_array_elements(payload#>'{server_evidence,pattern_events}') LOOP
            value=item->>'context_id';
            IF value IS NULL OR value !~ '^[a-f0-9]{64}$' THEN
              RAISE EXCEPTION 'PURGE_REFERENCE_UNVERIFIED' USING ERRCODE='55000';
            END IF;
            result=array_append(result,value);
          END LOOP;
        ELSE
          value=payload->>'context_id';
          IF value IS NULL OR value !~ '^[a-f0-9]{64}$' THEN
            RAISE EXCEPTION 'PURGE_REFERENCE_UNVERIFIED' USING ERRCODE='55000';
          END IF;
          result=array_append(result,value);
        END IF;
        RETURN ARRAY(SELECT DISTINCT v FROM unnest(result) v ORDER BY v);
      END $$;

      CREATE FUNCTION gatescope_security.replay_purge_preflight(target uuid) RETURNS void
        LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public SET row_security=off AS $$
      BEGIN
        PERFORM gatescope_security.replay_purge_owner();
        IF NOT EXISTS(SELECT 1 FROM instruments WHERE id=target) THEN
          RAISE EXCEPTION 'PURGE_TARGET_NOT_FOUND' USING ERRCODE='55000';
        END IF;
        IF EXISTS(SELECT 1 FROM watchlist_items WHERE instrument_id=target) THEN
          RAISE EXCEPTION 'PURGE_TARGET_WATCHED' USING ERRCODE='55000';
        END IF;
        IF EXISTS(SELECT 1 FROM alert_rules r JOIN alert_rule_versions v
          ON v.tenant_id=r.tenant_id AND v.alert_rule_id=r.id AND v.version=r.current_version
          WHERE r.status='ACTIVE' AND v.condition_json->>'instrument_id'=target::text) THEN
          RAISE EXCEPTION 'PURGE_TARGET_ACTIVE_ALERT' USING ERRCODE='55000';
        END IF;
        IF EXISTS(SELECT 1 FROM market_sync_state WHERE instrument_id=target AND active)
          OR EXISTS(SELECT 1 FROM market_pattern_studies WHERE instrument_id=target AND state IN ('QUEUED','RUNNING')) THEN
          RAISE EXCEPTION 'PURGE_TARGET_ACTIVE_COLLECTION' USING ERRCODE='55000';
        END IF;
        -- Old PAPER manifests are not self-contained. Keep everything whenever
        -- this older, more general reference format cannot be proven unrelated.
        IF EXISTS(SELECT 1 FROM signal_evidence_snapshots WHERE instrument_id=target)
          OR EXISTS(SELECT 1 FROM research_backtests WHERE instrument_id=target) THEN
          RAISE EXCEPTION 'PURGE_LEGACY_OR_RESEARCH_REFERENCES' USING ERRCODE='55000';
        END IF;
        IF EXISTS(SELECT 1 FROM paper_orders WHERE instrument_id=target AND decision_snapshot_id IS NULL
          AND request_json#>'{decision_context,pattern_events}' IS NOT NULL
          AND request_json#>'{decision_context,pattern_events}'<>'[]'::jsonb) THEN
          RAISE EXCEPTION 'PURGE_REFERENCE_UNVERIFIED' USING ERRCODE='55000';
        END IF;
      END $$;

      CREATE FUNCTION gatescope_security.replay_purge_references(target uuid) RETURNS text[]
        LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public SET row_security=off AS $$
      DECLARE item record; result text[]='{}'; n integer=0;
      BEGIN
        PERFORM gatescope_security.replay_purge_owner();
        FOR item IN SELECT * FROM (
          SELECT 'alert_event_facts' AS kind,payload_json AS payload FROM alert_event_facts WHERE instrument_id=target
          UNION ALL SELECT 'pattern_event_facts',payload_json FROM pattern_event_facts WHERE instrument_id=target
          UNION ALL SELECT 'shadow_trade_facts',source_event_json FROM shadow_trade_facts WHERE instrument_id=target
          UNION ALL SELECT 'paper_decision_snapshots',evidence_json FROM paper_decision_snapshots WHERE instrument_id=target
          UNION ALL SELECT 'coinglass_pattern_reviews',jsonb_build_object('context_id',context_id)
            FROM coinglass_pattern_reviews WHERE instrument_id=target
        ) refs LIMIT 5001 LOOP
          n=n+1;
          IF n>5000 THEN RAISE EXCEPTION 'PURGE_REFERENCE_BUDGET' USING ERRCODE='55000'; END IF;
          result=array_cat(result,gatescope_security.replay_reference_contexts(item.kind,item.payload));
          IF cardinality(result)>20000 THEN RAISE EXCEPTION 'PURGE_REFERENCE_BUDGET' USING ERRCODE='55000'; END IF;
        END LOOP;
        RETURN ARRAY(SELECT DISTINCT v FROM unnest(result) v ORDER BY v);
      END $$;

      CREATE FUNCTION gatescope_security.inspect_replay_purge(target uuid,requested_limit integer DEFAULT 16) RETURNS jsonb
        LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public SET row_security=off AS $$
      DECLARE item record; previous text[]; refs text[]; contexts jsonb='[]'; skipped integer=0; scanned integer=0;
      BEGIN
        PERFORM gatescope_security.replay_purge_preflight(target);
        IF requested_limit NOT BETWEEN 1 AND 32 THEN RAISE EXCEPTION 'PURGE_INVALID_BUDGET'; END IF;
        refs=gatescope_security.replay_purge_references(target);
        -- Keyset jump across complete groups rather than DISTINCT scanning
        -- millions of rows from the same context. Existing lookup index fits.
        FOR n IN 1..32 LOOP
          SELECT timeframe,version,context_id INTO item FROM market_pattern_events
            WHERE instrument_id=target AND source_mode='REPLAY' AND context_id ~ '^[a-f0-9]{64}$'
              AND (previous IS NULL OR (timeframe,version,context_id)>(previous[1],previous[2],previous[3]))
            ORDER BY timeframe,version,context_id LIMIT 1;
          EXIT WHEN NOT FOUND;
          previous=ARRAY[item.timeframe,item.version,item.context_id]; scanned=scanned+1;
          IF item.context_id=ANY(refs)
            OR EXISTS(SELECT 1 FROM market_pattern_events WHERE context_id=item.context_id
              AND (instrument_id<>target OR source_mode<>'REPLAY' OR timeframe<>item.timeframe OR version<>item.version))
            OR EXISTS(SELECT 1 FROM market_pattern_reports WHERE context_id=item.context_id AND instrument_id<>target)
            OR EXISTS(SELECT 1 FROM gatescope_security.replay_purge_contexts WHERE context_id=item.context_id) THEN
            skipped=skipped+1; CONTINUE;
          END IF;
          contexts=contexts||jsonb_build_array(jsonb_build_object('context_id',item.context_id,'timeframe',item.timeframe,'version',item.version));
          EXIT WHEN jsonb_array_length(contexts)>=requested_limit;
        END LOOP;
        RETURN jsonb_build_object('instrument_id',target,'contexts',contexts,'scanned_contexts',scanned,
          'retained_contexts',skipped,'enumeration_bounded',true,'physical_reclaimed_bytes',NULL);
      END $$;

      CREATE FUNCTION gatescope_security.open_replay_purge(job uuid,target uuid) RETURNS jsonb
        LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public SET row_security=off AS $$
      DECLARE preview jsonb; item jsonb; refs text[]; previous_target uuid;
      BEGIN
        PERFORM gatescope_security.replay_purge_owner();
        PERFORM pg_advisory_xact_lock(hashtextextended('replay-purge-job:'||target::text,0));
        PERFORM pg_advisory_xact_lock(hashtextextended('replay-instrument:'||target::text,0));
        PERFORM 1 FROM instruments WHERE id=target FOR UPDATE;
        PERFORM gatescope_security.replay_purge_preflight(target);
        SELECT instrument_id INTO previous_target FROM gatescope_security.replay_purge_jobs WHERE id=job FOR UPDATE;
        IF FOUND THEN
          IF previous_target<>target THEN RAISE EXCEPTION 'PURGE_JOB_TARGET_MISMATCH'; END IF;
          UPDATE gatescope_security.replay_purge_jobs SET status='OPEN',expires_at=clock_timestamp()+interval '5 minutes',updated_at=clock_timestamp() WHERE id=job;
        ELSE
          preview=gatescope_security.inspect_replay_purge(target,32);
          INSERT INTO gatescope_security.replay_purge_jobs(id,instrument_id,status,expires_at)
            VALUES(job,target,'OPEN',clock_timestamp()+interval '5 minutes');
          FOR item IN SELECT * FROM jsonb_array_elements(preview->'contexts') LOOP
            PERFORM pg_advisory_xact_lock(hashtextextended('replay-context:'||(item->>'context_id'),0));
            refs=gatescope_security.replay_purge_references(target);
            IF item->>'context_id'=ANY(refs) THEN CONTINUE; END IF;
            INSERT INTO gatescope_security.replay_purge_contexts(context_id,job_id,instrument_id,timeframe,version)
              VALUES(item->>'context_id',job,target,item->>'timeframe',item->>'version') ON CONFLICT DO NOTHING;
          END LOOP;
        END IF;
        RETURN jsonb_build_object('job_id',job,'instrument_id',target,'contexts',
          (SELECT count(*) FROM gatescope_security.replay_purge_contexts WHERE job_id=job),'status','OPEN');
      END $$;

      CREATE FUNCTION gatescope_security.replay_purge_row_allowed(relation oid,old_row jsonb) RETURNS boolean
        LANGUAGE plpgsql SET search_path=pg_catalog,public AS $$
      DECLARE scope text=current_setting('gatescope.replay_purge_job',true); allowed boolean=false;
      BEGIN
        IF current_user<>pg_get_userbyid((SELECT relowner FROM pg_class WHERE oid=relation))
          OR session_user<>current_user OR scope IS NULL OR scope='' THEN RETURN false; END IF;
        IF relation NOT IN ('public.market_pattern_events'::regclass,'public.market_pattern_reports'::regclass,
          'public.market_pattern_report_versions'::regclass) THEN RETURN false; END IF;
        SELECT true INTO allowed FROM gatescope_security.replay_purge_jobs j
          JOIN gatescope_security.replay_purge_contexts c ON c.job_id=j.id AND c.instrument_id=j.instrument_id
          WHERE j.id::text=scope AND j.status='OPEN' AND j.expires_at>clock_timestamp()
            AND c.context_id=old_row->>'context_id' AND c.state IN ('GUARDED','PARTIAL')
            AND (relation='public.market_pattern_report_versions'::regclass OR old_row->>'instrument_id'=j.instrument_id::text)
            AND (relation<>'public.market_pattern_events'::regclass OR old_row->>'source_mode'='REPLAY');
        RETURN coalesce(allowed,false);
      END $$;

      CREATE FUNCTION gatescope_security.guard_replay_report() RETURNS trigger
        LANGUAGE plpgsql SET search_path=pg_catalog,public AS $$
      DECLARE binding jsonb;
      BEGIN
        IF TG_TABLE_NAME='market_pattern_reports' THEN
          binding=jsonb_build_object('context_id',OLD.context_id,'instrument_id',OLD.instrument_id);
        ELSE
          binding=jsonb_build_object('context_id',OLD.context_id);
        END IF;
        IF TG_OP='DELETE' AND gatescope_security.replay_purge_row_allowed(TG_RELID,binding) THEN RETURN OLD; END IF;
        RAISE EXCEPTION 'immutable fact cannot be updated or deleted' USING ERRCODE='55000';
      END $$;

      CREATE FUNCTION gatescope_security.guard_replay_reference() RETURNS trigger
        LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public SET row_security=off AS $$
      DECLARE payload jsonb; refs text[]; context text; target uuid;
      BEGIN
        target=NEW.instrument_id;
        -- Always lock, including before the first job exists. BEFORE INSERT
        -- runs before the FK lock: relying on the FK leaves an initial-job race.
        PERFORM pg_advisory_xact_lock_shared(hashtextextended('replay-instrument:'||target::text,0));
        -- No behavioral change for instruments with no open/partial/purged job.
        IF NOT EXISTS(SELECT 1 FROM gatescope_security.replay_purge_jobs j
          JOIN gatescope_security.replay_purge_contexts c ON c.job_id=j.id
          WHERE j.instrument_id=target AND (c.state IN ('PARTIAL','PURGED') OR (c.state='GUARDED' AND j.status='OPEN'
            AND j.expires_at>clock_timestamp()))) THEN RETURN NEW; END IF;
        CASE TG_TABLE_NAME
          WHEN 'shadow_trade_facts' THEN payload=NEW.source_event_json;
          WHEN 'paper_decision_snapshots' THEN payload=NEW.evidence_json;
          WHEN 'coinglass_pattern_reviews' THEN payload=jsonb_build_object('context_id',NEW.context_id);
          ELSE payload=NEW.payload_json;
        END CASE;
        refs=gatescope_security.replay_reference_contexts(TG_TABLE_NAME,payload);
        FOREACH context IN ARRAY refs LOOP
          PERFORM pg_advisory_xact_lock_shared(hashtextextended('replay-context:'||context,0));
          IF EXISTS(SELECT 1 FROM gatescope_security.replay_purge_contexts c
            JOIN gatescope_security.replay_purge_jobs j ON j.id=c.job_id
            WHERE c.context_id=context AND (c.state IN ('PARTIAL','PURGED') OR (c.state='GUARDED' AND j.status='OPEN'
              AND j.expires_at>clock_timestamp()))) THEN
            RAISE EXCEPTION 'PURGE_CONTEXT_RESERVED_OR_REMOVED' USING ERRCODE='55000';
          END IF;
        END LOOP;
        RETURN NEW;
      END $$;

      CREATE FUNCTION gatescope_security.guard_replay_source() RETURNS trigger
        LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public SET row_security=off AS $$
      DECLARE context text=NEW.context_id; target uuid;
      BEGIN
        IF TG_TABLE_NAME IN ('market_pattern_events','market_pattern_reports') THEN target=NEW.instrument_id; END IF;
        IF target IS NULL THEN
          SELECT instrument_id INTO target FROM gatescope_security.replay_purge_contexts WHERE context_id=context;
          IF target IS NULL THEN
            SELECT instrument_id INTO target FROM market_pattern_events WHERE context_id=context LIMIT 1;
          END IF;
          IF target IS NULL THEN
            SELECT instrument_id INTO target FROM market_pattern_reports WHERE context_id=context;
          END IF;
        END IF;
        IF target IS NOT NULL THEN
          PERFORM pg_advisory_xact_lock_shared(hashtextextended('replay-instrument:'||target::text,0));
        END IF;
        PERFORM pg_advisory_xact_lock_shared(hashtextextended('replay-context:'||context,0));
        IF EXISTS(SELECT 1 FROM gatescope_security.replay_purge_contexts c
          JOIN gatescope_security.replay_purge_jobs j ON j.id=c.job_id
          WHERE c.context_id=context AND (c.state IN ('PARTIAL','PURGED') OR (c.state='GUARDED' AND j.status='OPEN'
            AND j.expires_at>clock_timestamp()))) THEN
          RAISE EXCEPTION 'PURGE_CONTEXT_RESERVED_OR_REMOVED' USING ERRCODE='55000';
        END IF;
        RETURN NEW;
      END $$;

      CREATE FUNCTION gatescope_security.replay_purge_batch(job uuid,requested_rows integer DEFAULT 100) RETURNS jsonb
        LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public SET row_security=off AS $$
      DECLARE target uuid; context record; refs text[]; amount integer; remaining integer=requested_rows;
        total integer=0; counts jsonb='{}'; prior text; table_name text; key_columns text; next_phase text;
      BEGIN
        PERFORM gatescope_security.replay_purge_owner();
        IF requested_rows NOT BETWEEN 1 AND 500 THEN RAISE EXCEPTION 'PURGE_INVALID_BUDGET'; END IF;
        SELECT instrument_id INTO target FROM gatescope_security.replay_purge_jobs
          WHERE id=job AND status='OPEN' AND expires_at>clock_timestamp();
        IF target IS NULL THEN RAISE EXCEPTION 'PURGE_JOB_NOT_OPEN'; END IF;
        PERFORM pg_advisory_xact_lock(hashtextextended('replay-purge-job:'||target::text,0));
        PERFORM pg_advisory_xact_lock(hashtextextended('replay-instrument:'||target::text,0));
        PERFORM 1 FROM instruments WHERE id=target FOR UPDATE;
        PERFORM 1 FROM gatescope_security.replay_purge_jobs
          WHERE id=job AND instrument_id=target AND status='OPEN' AND expires_at>clock_timestamp() FOR UPDATE;
        IF NOT FOUND THEN RAISE EXCEPTION 'PURGE_JOB_NOT_OPEN'; END IF;
        PERFORM gatescope_security.replay_purge_preflight(target);
        SELECT * INTO context FROM gatescope_security.replay_purge_contexts
          WHERE job_id=job AND state IN ('GUARDED','PARTIAL') ORDER BY context_id LIMIT 1 FOR UPDATE;
        IF NOT FOUND THEN RETURN jsonb_build_object('status','COMPLETE','deleted_rows',0,'counts','{}'::jsonb); END IF;
        PERFORM pg_advisory_xact_lock(hashtextextended('replay-context:'||context.context_id,0));
        refs=gatescope_security.replay_purge_references(target);
        IF context.context_id=ANY(refs) THEN
          IF context.deleted_rows>0 THEN RAISE EXCEPTION 'PURGE_PROTECTED_PARTIAL'; END IF;
          UPDATE gatescope_security.replay_purge_contexts SET state='RETAINED' WHERE context_id=context.context_id;
          RETURN jsonb_build_object('status','RETAINED_REFERENCE','deleted_rows',0,'counts','{}'::jsonb);
        END IF;
        IF EXISTS(SELECT 1 FROM market_pattern_events WHERE context_id=context.context_id
            AND (instrument_id<>target OR source_mode<>'REPLAY' OR timeframe<>context.timeframe OR version<>context.version))
          OR EXISTS(SELECT 1 FROM market_pattern_reports WHERE context_id=context.context_id AND instrument_id<>target) THEN
          RAISE EXCEPTION 'PURGE_CONTEXT_BINDING_CHANGED';
        END IF;
        prior=current_setting('gatescope.replay_purge_job',true);
        PERFORM set_config('gatescope.replay_purge_job',job::text,true);
        WHILE remaining>0 AND context.phase<>'done' LOOP
          CASE context.phase
            WHEN 'stages' THEN table_name='market_structure_stages';key_columns='context_id,candidate_id,entry_phase';next_phase='returns';
            WHEN 'returns' THEN table_name='market_structure_returns';key_columns='context_id,evidence_id,horizon';next_phase='versions';
            WHEN 'versions' THEN table_name='market_pattern_report_versions';key_columns='context_id,snapshot_at';next_phase='events';
            WHEN 'events' THEN table_name='market_pattern_events';key_columns='context_id,candidate_id,sequence';next_phase='reports';
            WHEN 'reports' THEN table_name='market_pattern_reports';key_columns='context_id';next_phase='done';
            ELSE RAISE EXCEPTION 'PURGE_INVALID_PHASE';
          END CASE;
          -- Table/column names come only from the fixed CASE above, not caller input.
          EXECUTE format('WITH selected AS (SELECT %s FROM public.%I WHERE context_id=$1 ORDER BY %s LIMIT $2 FOR UPDATE), '
            'removed AS (DELETE FROM public.%I t USING selected s WHERE (%s)=(%s) RETURNING 1) SELECT count(*) FROM removed',
            key_columns,table_name,key_columns,table_name,
            (SELECT string_agg('t.'||v,',') FROM unnest(string_to_array(key_columns,',')) v),
            (SELECT string_agg('s.'||v,',') FROM unnest(string_to_array(key_columns,',')) v)) INTO amount USING context.context_id,remaining;
          total=total+amount;remaining=remaining-amount;
          counts=counts||jsonb_build_object(table_name,amount);
          IF amount=0 THEN context.phase=next_phase; ELSE EXIT; END IF;
        END LOOP;
        UPDATE gatescope_security.replay_purge_contexts SET phase=context.phase,
          state=CASE WHEN context.phase='done' THEN 'PURGED' WHEN total>0 OR deleted_rows>0 THEN 'PARTIAL' ELSE 'GUARDED' END,
          deleted_rows=deleted_rows+total WHERE context_id=context.context_id;
        UPDATE gatescope_security.replay_purge_jobs SET deleted_rows=deleted_rows+total,updated_at=clock_timestamp() WHERE id=job;
        PERFORM set_config('gatescope.replay_purge_job',coalesce(prior,''),true);
        RETURN jsonb_build_object('status','BATCH_COMPLETE','deleted_rows',total,'counts',counts,'context_id',context.context_id,'phase',context.phase);
      END $$;

      CREATE FUNCTION gatescope_security.close_replay_purge(job uuid,completion text DEFAULT 'PAUSED') RETURNS void
        LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public SET row_security=off AS $$
      BEGIN
        PERFORM gatescope_security.replay_purge_owner();
        IF completion NOT IN ('PAUSED','DONE','FAILED') THEN RAISE EXCEPTION 'PURGE_INVALID_STATUS'; END IF;
        UPDATE gatescope_security.replay_purge_jobs SET status=completion,expires_at=clock_timestamp(),updated_at=clock_timestamp() WHERE id=job;
      END $$;
    """)
    definition = op.get_bind().exec_driver_sql("SELECT pg_get_functiondef('gatescope_security.guard_structure_event()'::regprocedure)").scalar_one()
    if "BEGIN\n" not in definition or "replay_purge_row_allowed" in definition:
        raise RuntimeError("UNEXPECTED_STRUCTURE_GUARD")
    op.execute(definition.replace("BEGIN\n", "BEGIN\n"+EVENT_BRANCH, 1))
    for table, trigger in (("market_pattern_reports", "pattern_reports_immutable"), ("market_pattern_report_versions", "pattern_report_versions_immutable")):
        op.execute(f"DROP TRIGGER {trigger} ON {table}; CREATE TRIGGER {trigger} BEFORE UPDATE OR DELETE ON {table} FOR EACH ROW EXECUTE FUNCTION gatescope_security.guard_replay_report()")
    for table in REFERENCE_TABLES:
        action = "INSERT OR UPDATE" if table == "coinglass_pattern_reviews" else "INSERT"
        op.execute(f"CREATE TRIGGER replay_reference_guard BEFORE {action} ON {table} FOR EACH ROW EXECUTE FUNCTION gatescope_security.guard_replay_reference()")
    for table in SOURCE_TABLES:
        op.execute(f"CREATE TRIGGER replay_source_guard BEFORE INSERT OR UPDATE ON {table} FOR EACH ROW EXECUTE FUNCTION gatescope_security.guard_replay_source()")
    signatures = (
        "replay_purge_owner()", "replay_reference_contexts(text,jsonb)", "replay_purge_preflight(uuid)",
        "replay_purge_references(uuid)", "inspect_replay_purge(uuid,integer)", "open_replay_purge(uuid,uuid)",
        "replay_purge_row_allowed(oid,jsonb)", "guard_replay_report()", "guard_replay_reference()", "guard_replay_source()",
        "replay_purge_batch(uuid,integer)", "close_replay_purge(uuid,text)",
    )
    for signature in signatures:
        op.execute(f"REVOKE ALL ON FUNCTION gatescope_security.{signature} FROM PUBLIC,gatescope_api,gatescope_auth,gatescope_market,gatescope_strategy,gatescope_notification,gatescope_executor,gatescope_mcp,gatescope_chart,gatescope_paper")


def downgrade():
    if op.get_bind().dialect.name != "postgresql":
        return
    if op.get_bind().exec_driver_sql("SELECT EXISTS(SELECT 1 FROM gatescope_security.replay_purge_contexts WHERE deleted_rows>0)").scalar():
        raise RuntimeError("PURGED_CONTEXT_TOMBSTONES_EXIST_FORWARD_FIX_REQUIRED")
    for table in REFERENCE_TABLES:
        op.execute(f"DROP TRIGGER replay_reference_guard ON {table}")
    for table in SOURCE_TABLES:
        op.execute(f"DROP TRIGGER replay_source_guard ON {table}")
    definition = op.get_bind().exec_driver_sql("SELECT pg_get_functiondef('gatescope_security.guard_structure_event()'::regprocedure)").scalar_one()
    if EVENT_BRANCH not in definition:
        raise RuntimeError("UNEXPECTED_STRUCTURE_GUARD")
    op.execute(definition.replace(EVENT_BRANCH, "", 1))
    for table, trigger in (("market_pattern_reports", "pattern_reports_immutable"), ("market_pattern_report_versions", "pattern_report_versions_immutable")):
        op.execute(f"DROP TRIGGER {trigger} ON {table}; CREATE TRIGGER {trigger} BEFORE UPDATE OR DELETE ON {table} FOR EACH ROW EXECUTE FUNCTION gatescope_security.reject_fact_mutation()")
    op.execute("""DROP FUNCTION gatescope_security.close_replay_purge(uuid,text),gatescope_security.replay_purge_batch(uuid,integer),
      gatescope_security.guard_replay_reference(),gatescope_security.guard_replay_source(),gatescope_security.guard_replay_report(),gatescope_security.replay_purge_row_allowed(oid,jsonb),
      gatescope_security.open_replay_purge(uuid,uuid),gatescope_security.inspect_replay_purge(uuid,integer),
      gatescope_security.replay_purge_references(uuid),gatescope_security.replay_purge_preflight(uuid),
      gatescope_security.replay_reference_contexts(text,jsonb),gatescope_security.replay_purge_owner();
      DROP TABLE gatescope_security.replay_purge_contexts,gatescope_security.replay_purge_jobs;""")
