"""Observable durable chart requests with polling-independent fair ordering."""

from alembic import op

revision = "0044_coinglass_queue_progress"
down_revision = "0043_coinglass_cache_cooldown"
branch_labels = depends_on = None


def _request(observable):
    columns = ",pending_since" if observable else ""
    values = ",now()" if observable else ""
    pending = (
        """,pending_since=CASE WHEN coinglass_chart_cache.config_id=EXCLUDED.config_id
          AND coinglass_chart_cache.secret_version=EXCLUDED.secret_version THEN coalesce(coinglass_chart_cache.pending_since,now()) ELSE now() END,
          collection_started_at=CASE WHEN coinglass_chart_cache.config_id=EXCLUDED.config_id
          AND coinglass_chart_cache.secret_version=EXCLUDED.secret_version THEN coinglass_chart_cache.collection_started_at ELSE NULL END"""
        if observable
        else ""
    )
    op.execute(f"""
      CREATE OR REPLACE FUNCTION gatescope_security.request_coinglass_chart(t uuid,u uuid,i uuid,tf text) RETURNS void
      LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
      BEGIN
        IF t IS DISTINCT FROM gatescope_security.current_tenant_id() OR u IS DISTINCT FROM gatescope_security.current_subject_id()
          OR tf IS NULL OR tf NOT IN ('1m','5m','15m','30m','1h','4h','8h','1d') THEN RETURN; END IF;
        IF NOT EXISTS(SELECT 1 FROM public.instruments WHERE id=i AND status='ACTIVE') THEN RETURN; END IF;
        IF NOT EXISTS(SELECT 1 FROM public.coinglass_configs WHERE tenant_id=t AND created_by=u AND status='ACTIVE') THEN RETURN; END IF;
        PERFORM pg_advisory_xact_lock(728429143);
        IF NOT EXISTS(SELECT 1 FROM public.coinglass_chart_cache WHERE tenant_id=t AND user_id=u AND instrument_id=i AND timeframe=tf)
          AND ((SELECT count(*) FROM public.coinglass_chart_cache WHERE tenant_id=t AND user_id=u)>=256
            OR (SELECT count(*) FROM public.coinglass_chart_cache)>=8192) THEN RETURN; END IF;
        INSERT INTO public.coinglass_chart_cache(tenant_id,user_id,instrument_id,timeframe,config_id,secret_version{columns})
        SELECT t,u,i,tf,c.id,c.secret_version{values} FROM public.coinglass_configs c
        WHERE c.tenant_id=t AND c.created_by=u AND c.status='ACTIVE'
        ON CONFLICT(tenant_id,user_id,instrument_id,timeframe) DO UPDATE SET
          requested_at=now(),config_id=EXCLUDED.config_id,secret_version=EXCLUDED.secret_version,
          payload=CASE WHEN coinglass_chart_cache.config_id=EXCLUDED.config_id AND coinglass_chart_cache.secret_version=EXCLUDED.secret_version THEN coinglass_chart_cache.payload ELSE '{{}}'::jsonb END,
          due_at=CASE WHEN coinglass_chart_cache.config_id=EXCLUDED.config_id AND coinglass_chart_cache.secret_version=EXCLUDED.secret_version THEN coinglass_chart_cache.due_at ELSE now() END,
          lease_id=CASE WHEN coinglass_chart_cache.config_id=EXCLUDED.config_id AND coinglass_chart_cache.secret_version=EXCLUDED.secret_version THEN coinglass_chart_cache.lease_id ELSE NULL END,
          lease_until=CASE WHEN coinglass_chart_cache.config_id=EXCLUDED.config_id AND coinglass_chart_cache.secret_version=EXCLUDED.secret_version THEN coinglass_chart_cache.lease_until ELSE NULL END
          {pending};
      END $$;
    """)


def _claim(observable):
    activity = (
        """owner_activity AS (
          SELECT tenant_id,user_id,max(collection_started_at) AS last_started
            FROM public.coinglass_chart_cache GROUP BY tenant_id,user_id
        ),"""
        if observable
        else ""
    )
    join = (
        "JOIN owner_activity activity ON activity.tenant_id=cc.tenant_id AND activity.user_id=cc.user_id"
        if observable
        else ""
    )
    pending = "cc.pending_since IS NOT NULL OR " if observable else ""
    ordering = (
        """activity.last_started ASC NULLS FIRST,
          least(coalesce(cc.pending_since,'infinity'::timestamptz),cc.due_at+interval '5 minutes'),
          cc.pending_since ASC NULLS LAST,cc.due_at,cc.instrument_id,cc.timeframe"""
        if observable
        else "(cc.requested_at>now()-interval '5 minutes') DESC,cc.due_at,cc.requested_at DESC,cc.instrument_id,cc.timeframe"
    )
    started = ",collection_started_at=now()" if observable else ""
    op.execute(f"""
      CREATE OR REPLACE FUNCTION gatescope_security.claim_coinglass_chart()
      RETURNS TABLE(tenant_id uuid,user_id uuid,instrument_id uuid,timeframe text,lease_id uuid)
      LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
        WITH {activity} candidate AS (
          SELECT cc.tenant_id,cc.user_id,cc.instrument_id,cc.timeframe FROM public.coinglass_chart_cache cc
          JOIN public.coinglass_configs c ON c.id=cc.config_id AND c.created_by=cc.user_id AND c.tenant_id=cc.tenant_id
            AND c.status='ACTIVE' AND c.secret_version=cc.secret_version
          LEFT JOIN gatescope_security.coinglass_provider_cooldowns p ON p.key_fingerprint=c.key_fingerprint
          {join}
          WHERE cc.due_at<=now() AND (cc.lease_until IS NULL OR cc.lease_until<now())
            AND (p.pause_until IS NULL OR p.pause_until<=now())
            AND ({pending}cc.requested_at>now()-interval '2 hours' OR EXISTS(
              SELECT 1 FROM public.watchlist_items wi JOIN public.watchlists w ON wi.watchlist_id=w.id
              WHERE w.tenant_id=cc.tenant_id AND w.owner_user_id=cc.user_id AND w.deleted_at IS NULL
                AND wi.instrument_id=cc.instrument_id AND wi.default_timeframe=cc.timeframe))
          ORDER BY {ordering}
          FOR UPDATE OF cc SKIP LOCKED LIMIT 1
        ) UPDATE public.coinglass_chart_cache cc SET lease_id=gen_random_uuid(),lease_until=now()+interval '180 seconds'{started}
          FROM candidate x WHERE (cc.tenant_id,cc.user_id,cc.instrument_id,cc.timeframe)=(x.tenant_id,x.user_id,x.instrument_id,x.timeframe)
          RETURNING cc.tenant_id,cc.user_id,cc.instrument_id,cc.timeframe,cc.lease_id
      $$;
    """)


def upgrade():
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("""
      ALTER TABLE coinglass_chart_cache ADD COLUMN pending_since timestamptz,
        ADD COLUMN collection_started_at timestamptz;
      UPDATE coinglass_chart_cache SET pending_since=requested_at
        WHERE requested_at>'epoch'::timestamptz AND (updated_at IS NULL OR requested_at>updated_at);
      UPDATE coinglass_chart_cache SET collection_started_at=lease_until-interval '180 seconds'
        WHERE lease_id IS NOT NULL AND lease_until>now();
      CREATE INDEX coinglass_chart_pending ON coinglass_chart_cache(pending_since)
        WHERE pending_since IS NOT NULL;
      CREATE FUNCTION gatescope_security.reset_coinglass_queue_identity() RETURNS trigger
      LANGUAGE plpgsql SET search_path=pg_catalog,public AS $$
      BEGIN
        NEW.pending_since=CASE WHEN OLD.pending_since IS NOT NULL OR NEW.pending_since IS NOT NULL THEN now() ELSE NULL END;
        NEW.collection_started_at=NULL;
        RETURN NEW;
      END $$;
      REVOKE ALL ON FUNCTION gatescope_security.reset_coinglass_queue_identity() FROM PUBLIC;
      CREATE TRIGGER coinglass_queue_identity BEFORE UPDATE OF config_id,secret_version ON coinglass_chart_cache
        FOR EACH ROW WHEN(OLD.config_id IS DISTINCT FROM NEW.config_id OR OLD.secret_version IS DISTINCT FROM NEW.secret_version)
        EXECUTE FUNCTION gatescope_security.reset_coinglass_queue_identity();
      CREATE FUNCTION gatescope_security.coinglass_chart_is_watched(t uuid,u uuid,i uuid,tf text)
      RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,public AS $$
        SELECT t=gatescope_security.current_tenant_id() AND u=gatescope_security.current_subject_id()
          AND EXISTS(SELECT 1 FROM public.coinglass_chart_cache cc JOIN public.coinglass_configs c
            ON c.id=cc.config_id AND c.tenant_id=cc.tenant_id AND c.created_by=cc.user_id
              AND c.status='ACTIVE' AND c.secret_version=cc.secret_version
            JOIN public.watchlists w ON w.tenant_id=cc.tenant_id AND w.owner_user_id=cc.user_id AND w.deleted_at IS NULL
            JOIN public.watchlist_items wi ON wi.watchlist_id=w.id AND wi.tenant_id=w.tenant_id
              AND wi.instrument_id=cc.instrument_id AND wi.default_timeframe=cc.timeframe
            WHERE cc.tenant_id=t AND cc.user_id=u AND cc.instrument_id=i AND cc.timeframe=tf)
      $$;
      REVOKE ALL ON FUNCTION gatescope_security.coinglass_chart_is_watched(uuid,uuid,uuid,text) FROM PUBLIC;
      GRANT EXECUTE ON FUNCTION gatescope_security.coinglass_chart_is_watched(uuid,uuid,uuid,text)
        TO gatescope_api,gatescope_mcp,gatescope_market;
    """)
    _request(True)
    _claim(True)


def downgrade():
    if op.get_bind().dialect.name != "postgresql":
        return
    _request(False)
    _claim(False)
    op.execute("""
      DROP INDEX coinglass_chart_pending;
      DROP TRIGGER coinglass_queue_identity ON coinglass_chart_cache;
      DROP FUNCTION gatescope_security.reset_coinglass_queue_identity();
      DROP FUNCTION gatescope_security.coinglass_chart_is_watched(uuid,uuid,uuid,text);
      ALTER TABLE coinglass_chart_cache DROP COLUMN pending_since,DROP COLUMN collection_started_at;
    """)
