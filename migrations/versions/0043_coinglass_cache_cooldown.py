"""Persistent private provider cooldown and bounded owner-scoped MCP warming."""

from alembic import op

revision = "0043_coinglass_cache_cooldown"
down_revision = "0042_mcp_oauth"
branch_labels = depends_on = None


def upgrade():
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("""
      CREATE TABLE gatescope_security.coinglass_provider_cooldowns (
        key_fingerprint text PRIMARY KEY CHECK(key_fingerprint ~ '^SHA256:[0-9A-F]{12}$'),
        pause_until timestamptz NOT NULL, updated_at timestamptz NOT NULL DEFAULT now()
      );
      REVOKE ALL ON gatescope_security.coinglass_provider_cooldowns FROM PUBLIC;
      CREATE FUNCTION gatescope_security.coinglass_provider_wait(t uuid,u uuid,cid uuid,version integer)
      RETURNS integer LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,public AS $$
        SELECT COALESCE((SELECT greatest(0,ceil(extract(epoch FROM (p.pause_until-statement_timestamp()))))::integer
          FROM public.coinglass_configs c JOIN gatescope_security.coinglass_provider_cooldowns p USING(key_fingerprint)
          WHERE c.id=cid AND c.tenant_id=t AND c.created_by=u AND c.secret_version=version AND c.status<>'DISABLED'
            AND t=gatescope_security.current_tenant_id() AND u=gatescope_security.current_subject_id()),0)
      $$;
      CREATE FUNCTION gatescope_security.pause_coinglass_provider(t uuid,u uuid,cid uuid,version integer,seconds integer)
      RETURNS integer LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
      DECLARE fingerprint text;
      BEGIN
        IF seconds IS NULL OR seconds<1 THEN RAISE EXCEPTION 'INVALID_PROVIDER_COOLDOWN'; END IF;
        SELECT c.key_fingerprint INTO fingerprint FROM public.coinglass_configs c
          WHERE c.id=cid AND c.tenant_id=t AND c.created_by=u AND c.secret_version=version AND c.status<>'DISABLED'
            AND t=gatescope_security.current_tenant_id() AND u=gatescope_security.current_subject_id();
        IF fingerprint IS NULL THEN RETURN 0; END IF;
        INSERT INTO gatescope_security.coinglass_provider_cooldowns(key_fingerprint,pause_until)
          VALUES(fingerprint,statement_timestamp()+make_interval(secs=>seconds))
          ON CONFLICT(key_fingerprint) DO UPDATE SET pause_until=greatest(coinglass_provider_cooldowns.pause_until,EXCLUDED.pause_until),updated_at=now();
        RETURN gatescope_security.coinglass_provider_wait(t,u,cid,version);
      END $$;
      REVOKE ALL ON FUNCTION gatescope_security.coinglass_provider_wait(uuid,uuid,uuid,integer),
        gatescope_security.pause_coinglass_provider(uuid,uuid,uuid,integer,integer) FROM PUBLIC;
      GRANT EXECUTE ON FUNCTION gatescope_security.coinglass_provider_wait(uuid,uuid,uuid,integer)
        TO gatescope_api,gatescope_mcp,gatescope_market;
      GRANT EXECUTE ON FUNCTION gatescope_security.pause_coinglass_provider(uuid,uuid,uuid,integer,integer)
        TO gatescope_market;

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
        INSERT INTO public.coinglass_chart_cache(tenant_id,user_id,instrument_id,timeframe,config_id,secret_version)
        SELECT t,u,i,tf,c.id,c.secret_version FROM public.coinglass_configs c
        WHERE c.tenant_id=t AND c.created_by=u AND c.status='ACTIVE'
        ON CONFLICT(tenant_id,user_id,instrument_id,timeframe) DO UPDATE SET
          requested_at=now(),config_id=EXCLUDED.config_id,secret_version=EXCLUDED.secret_version,
          payload=CASE WHEN coinglass_chart_cache.config_id=EXCLUDED.config_id AND coinglass_chart_cache.secret_version=EXCLUDED.secret_version THEN coinglass_chart_cache.payload ELSE '{}'::jsonb END,
          due_at=CASE WHEN coinglass_chart_cache.config_id=EXCLUDED.config_id AND coinglass_chart_cache.secret_version=EXCLUDED.secret_version THEN coinglass_chart_cache.due_at ELSE now() END,
          lease_id=CASE WHEN coinglass_chart_cache.config_id=EXCLUDED.config_id AND coinglass_chart_cache.secret_version=EXCLUDED.secret_version THEN coinglass_chart_cache.lease_id ELSE NULL END,
          lease_until=CASE WHEN coinglass_chart_cache.config_id=EXCLUDED.config_id AND coinglass_chart_cache.secret_version=EXCLUDED.secret_version THEN coinglass_chart_cache.lease_until ELSE NULL END;
      END $$;
      GRANT EXECUTE ON FUNCTION gatescope_security.request_coinglass_chart(uuid,uuid,uuid,text) TO gatescope_mcp;

      CREATE OR REPLACE FUNCTION gatescope_security.seed_coinglass_charts() RETURNS integer
      LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
      DECLARE added integer; remaining integer;
      BEGIN
        PERFORM pg_advisory_xact_lock(728429143);
        UPDATE public.coinglass_chart_cache cc SET config_id=c.id,secret_version=c.secret_version,payload='{}',due_at=now(),lease_id=NULL,lease_until=NULL
          FROM public.coinglass_configs c WHERE c.tenant_id=cc.tenant_id AND c.created_by=cc.user_id AND c.status='ACTIVE'
            AND (cc.config_id<>c.id OR cc.secret_version<>c.secret_version);
        SELECT greatest(0,8192-count(*))::integer INTO remaining FROM public.coinglass_chart_cache;
        WITH missing AS (
          SELECT DISTINCT c.tenant_id,c.created_by AS user_id,wi.instrument_id,wi.default_timeframe AS timeframe,c.id AS config_id,c.secret_version
          FROM public.coinglass_configs c JOIN public.watchlists w ON w.tenant_id=c.tenant_id AND w.owner_user_id=c.created_by AND w.deleted_at IS NULL
          JOIN public.watchlist_items wi ON wi.watchlist_id=w.id AND wi.tenant_id=w.tenant_id
          JOIN public.instruments instrument ON instrument.id=wi.instrument_id AND instrument.status='ACTIVE'
          WHERE c.status='ACTIVE' AND wi.default_timeframe IN ('1m','5m','15m','30m','1h','4h','8h','1d')
            AND NOT EXISTS(SELECT 1 FROM public.coinglass_chart_cache cc WHERE cc.tenant_id=c.tenant_id AND cc.user_id=c.created_by
              AND cc.instrument_id=wi.instrument_id AND cc.timeframe=wi.default_timeframe)
        ), ranked AS (
          SELECT m.*,row_number() OVER(PARTITION BY tenant_id,user_id ORDER BY instrument_id,timeframe) AS rank
            FROM missing m
        ) INSERT INTO public.coinglass_chart_cache(tenant_id,user_id,instrument_id,timeframe,config_id,secret_version,requested_at)
          SELECT tenant_id,user_id,instrument_id,timeframe,config_id,secret_version,'epoch'::timestamptz FROM ranked r
          WHERE rank<=greatest(0,256-(SELECT count(*) FROM public.coinglass_chart_cache cc WHERE cc.tenant_id=r.tenant_id AND cc.user_id=r.user_id))
          ORDER BY tenant_id,user_id,instrument_id,timeframe LIMIT least(4096,remaining)
          ON CONFLICT DO NOTHING;
        GET DIAGNOSTICS added=ROW_COUNT;
        RETURN added;
      END $$;

      CREATE OR REPLACE FUNCTION gatescope_security.claim_coinglass_chart()
      RETURNS TABLE(tenant_id uuid,user_id uuid,instrument_id uuid,timeframe text,lease_id uuid)
      LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
        WITH candidate AS (
          SELECT cc.tenant_id,cc.user_id,cc.instrument_id,cc.timeframe FROM public.coinglass_chart_cache cc
          JOIN public.coinglass_configs c ON c.id=cc.config_id AND c.created_by=cc.user_id AND c.tenant_id=cc.tenant_id
            AND c.status='ACTIVE' AND c.secret_version=cc.secret_version
          LEFT JOIN gatescope_security.coinglass_provider_cooldowns p ON p.key_fingerprint=c.key_fingerprint
          WHERE cc.due_at<=now() AND (cc.lease_until IS NULL OR cc.lease_until<now())
            AND (p.pause_until IS NULL OR p.pause_until<=now())
            AND (cc.requested_at>now()-interval '2 hours' OR EXISTS(
              SELECT 1 FROM public.watchlist_items wi JOIN public.watchlists w ON wi.watchlist_id=w.id
              WHERE w.tenant_id=cc.tenant_id AND w.owner_user_id=cc.user_id AND w.deleted_at IS NULL
                AND wi.instrument_id=cc.instrument_id AND wi.default_timeframe=cc.timeframe))
          ORDER BY (cc.requested_at>now()-interval '5 minutes') DESC,cc.due_at,cc.requested_at DESC,cc.instrument_id,cc.timeframe
          FOR UPDATE OF cc SKIP LOCKED LIMIT 1
        ) UPDATE public.coinglass_chart_cache cc SET lease_id=gen_random_uuid(),lease_until=now()+interval '180 seconds'
          FROM candidate x WHERE (cc.tenant_id,cc.user_id,cc.instrument_id,cc.timeframe)=(x.tenant_id,x.user_id,x.instrument_id,x.timeframe)
          RETURNING cc.tenant_id,cc.user_id,cc.instrument_id,cc.timeframe,cc.lease_id
      $$;
    """)


def downgrade():
    if op.get_bind().dialect.name != "postgresql":
        return
    # Restore the prior claim/seed definitions before dropping their dependency.
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location(
        "previous_coinglass_scheduling", Path(__file__).with_name("0041_coinglass_scheduling.py")
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module._install(True)
    op.execute("""
      CREATE OR REPLACE FUNCTION gatescope_security.request_coinglass_chart(t uuid,u uuid,i uuid,tf text) RETURNS void
      LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
        INSERT INTO public.coinglass_chart_cache(tenant_id,user_id,instrument_id,timeframe,config_id,secret_version)
        SELECT t,u,i,tf,c.id,c.secret_version FROM public.coinglass_configs c
        WHERE c.tenant_id=t AND c.created_by=u AND c.status='ACTIVE'
          AND t=gatescope_security.current_tenant_id() AND u=gatescope_security.current_subject_id()
        ON CONFLICT(tenant_id,user_id,instrument_id,timeframe) DO UPDATE SET
          requested_at=now(),config_id=EXCLUDED.config_id,secret_version=EXCLUDED.secret_version,
          payload=CASE WHEN coinglass_chart_cache.config_id=EXCLUDED.config_id AND coinglass_chart_cache.secret_version=EXCLUDED.secret_version THEN coinglass_chart_cache.payload ELSE '{}'::jsonb END,
          due_at=CASE WHEN coinglass_chart_cache.config_id=EXCLUDED.config_id AND coinglass_chart_cache.secret_version=EXCLUDED.secret_version THEN coinglass_chart_cache.due_at ELSE now() END,
          lease_id=CASE WHEN coinglass_chart_cache.config_id=EXCLUDED.config_id AND coinglass_chart_cache.secret_version=EXCLUDED.secret_version THEN coinglass_chart_cache.lease_id ELSE NULL END,
          lease_until=CASE WHEN coinglass_chart_cache.config_id=EXCLUDED.config_id AND coinglass_chart_cache.secret_version=EXCLUDED.secret_version THEN coinglass_chart_cache.lease_until ELSE NULL END
      $$;
      REVOKE EXECUTE ON FUNCTION gatescope_security.request_coinglass_chart(uuid,uuid,uuid,text) FROM gatescope_mcp;
      DROP FUNCTION gatescope_security.pause_coinglass_provider(uuid,uuid,uuid,integer,integer);
      DROP FUNCTION gatescope_security.coinglass_provider_wait(uuid,uuid,uuid,integer);
      DROP TABLE gatescope_security.coinglass_provider_cooldowns;
    """)
