"""Owner-scoped bounded Coinglass cache and immutable review snapshots."""
from alembic import op

revision = "0040_coinglass_context"
down_revision = "0039_structure_reference_cover"
branch_labels = depends_on = None


def upgrade():
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("""
      CREATE TABLE coinglass_chart_cache (
        tenant_id uuid NOT NULL REFERENCES tenants(id),
        user_id uuid NOT NULL REFERENCES users(id),
        instrument_id uuid NOT NULL REFERENCES instruments(id),
        timeframe text NOT NULL CHECK(timeframe IN ('1m','5m','15m','30m','1h','4h','8h','1d')),
        config_id uuid NOT NULL REFERENCES coinglass_configs(id),
        secret_version integer NOT NULL,
        payload jsonb NOT NULL DEFAULT '{}' CHECK(jsonb_typeof(payload)='object'),
        requested_at timestamptz NOT NULL DEFAULT now(),
        due_at timestamptz NOT NULL DEFAULT now(),
        lease_id uuid, lease_until timestamptz,
        updated_at timestamptz,
        PRIMARY KEY(tenant_id,user_id,instrument_id,timeframe)
      );
      CREATE INDEX coinglass_chart_due ON coinglass_chart_cache(due_at);
      CREATE TABLE coinglass_pattern_reviews (
        id uuid PRIMARY KEY, tenant_id uuid NOT NULL REFERENCES tenants(id),
        user_id uuid NOT NULL REFERENCES users(id),
        instrument_id uuid NOT NULL REFERENCES instruments(id),
        timeframe text NOT NULL, context_id text NOT NULL, candidate_id text NOT NULL,
        payload jsonb NOT NULL CHECK(jsonb_typeof(payload)='object'),
        created_at timestamptz NOT NULL DEFAULT now()
      );
      CREATE INDEX coinglass_review_owner ON coinglass_pattern_reviews(tenant_id,user_id,created_at DESC);
      ALTER TABLE coinglass_chart_cache ENABLE ROW LEVEL SECURITY;
      ALTER TABLE coinglass_chart_cache FORCE ROW LEVEL SECURITY;
      CREATE POLICY owner_isolation ON coinglass_chart_cache
        USING(tenant_id=gatescope_security.current_tenant_id() AND user_id=gatescope_security.current_subject_id())
        WITH CHECK(tenant_id=gatescope_security.current_tenant_id() AND user_id=gatescope_security.current_subject_id());
      ALTER TABLE coinglass_pattern_reviews ENABLE ROW LEVEL SECURITY;
      ALTER TABLE coinglass_pattern_reviews FORCE ROW LEVEL SECURITY;
      CREATE POLICY owner_isolation ON coinglass_pattern_reviews
        USING(tenant_id=gatescope_security.current_tenant_id() AND user_id=gatescope_security.current_subject_id())
        WITH CHECK(tenant_id=gatescope_security.current_tenant_id() AND user_id=gatescope_security.current_subject_id());
      GRANT SELECT ON coinglass_chart_cache TO gatescope_api;
      GRANT SELECT,UPDATE ON coinglass_chart_cache TO gatescope_market;
      GRANT SELECT ON coinglass_chart_cache TO gatescope_mcp;
      GRANT SELECT,INSERT ON coinglass_pattern_reviews TO gatescope_api;
      GRANT SELECT ON market_sync_state,market_live_candles TO gatescope_mcp;

      CREATE FUNCTION gatescope_security.coinglass_chart_access(t uuid,u uuid)
      RETURNS TABLE(id uuid,secret_version integer)
      LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,public AS $$
        SELECT c.id,c.secret_version FROM public.coinglass_configs c
        WHERE c.tenant_id=t AND c.created_by=u AND c.status='ACTIVE'
          AND t=gatescope_security.current_tenant_id() AND u=gatescope_security.current_subject_id()
      $$;
      REVOKE ALL ON FUNCTION gatescope_security.coinglass_chart_access(uuid,uuid) FROM PUBLIC;
      GRANT EXECUTE ON FUNCTION gatescope_security.coinglass_chart_access(uuid,uuid) TO gatescope_api,gatescope_mcp,gatescope_market;

      CREATE FUNCTION gatescope_security.request_coinglass_chart(t uuid,u uuid,i uuid,tf text) RETURNS void
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
      REVOKE ALL ON FUNCTION gatescope_security.request_coinglass_chart(uuid,uuid,uuid,text) FROM PUBLIC;
      GRANT EXECUTE ON FUNCTION gatescope_security.request_coinglass_chart(uuid,uuid,uuid,text) TO gatescope_api;

      CREATE FUNCTION gatescope_security.seed_coinglass_charts() RETURNS integer
      LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
      DECLARE added integer;
      BEGIN
        INSERT INTO public.coinglass_chart_cache(tenant_id,user_id,instrument_id,timeframe,config_id,secret_version,requested_at)
        SELECT DISTINCT c.tenant_id,c.created_by,i.instrument_id,t.tf,c.id,c.secret_version,'epoch'::timestamptz
        FROM public.coinglass_configs c
        JOIN public.watchlists w ON w.tenant_id=c.tenant_id AND w.owner_user_id=c.created_by AND w.deleted_at IS NULL
        JOIN public.watchlist_items i ON i.watchlist_id=w.id AND i.tenant_id=w.tenant_id
        CROSS JOIN (VALUES('1m'),('5m'),('15m'),('30m'),('1h'),('4h'),('8h'),('1d')) t(tf)
        WHERE c.status='ACTIVE'
        LIMIT 4096
        ON CONFLICT(tenant_id,user_id,instrument_id,timeframe) DO UPDATE
          SET config_id=EXCLUDED.config_id,secret_version=EXCLUDED.secret_version,payload='{}',due_at=now(),lease_id=NULL,lease_until=NULL
          WHERE coinglass_chart_cache.config_id<>EXCLUDED.config_id OR coinglass_chart_cache.secret_version<>EXCLUDED.secret_version;
        GET DIAGNOSTICS added=ROW_COUNT;
        RETURN added;
      END $$;
      CREATE FUNCTION gatescope_security.claim_coinglass_chart()
      RETURNS TABLE(tenant_id uuid,user_id uuid,instrument_id uuid,timeframe text,lease_id uuid)
      LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
        WITH candidate AS (
          SELECT cc.tenant_id,cc.user_id,cc.instrument_id,cc.timeframe FROM public.coinglass_chart_cache cc
          JOIN public.coinglass_configs c ON c.id=cc.config_id AND c.created_by=cc.user_id AND c.status='ACTIVE'
          WHERE cc.due_at<=now() AND (cc.lease_until IS NULL OR cc.lease_until<now())
            AND (cc.requested_at>now()-interval '2 hours' OR EXISTS(
              SELECT 1 FROM public.watchlist_items wi JOIN public.watchlists w ON wi.watchlist_id=w.id
              WHERE w.tenant_id=cc.tenant_id AND w.owner_user_id=cc.user_id AND w.deleted_at IS NULL AND wi.instrument_id=cc.instrument_id))
          ORDER BY (cc.requested_at>now()-interval '5 minutes') DESC,cc.due_at,cc.requested_at DESC,cc.instrument_id,cc.timeframe
          FOR UPDATE OF cc SKIP LOCKED LIMIT 1
        ) UPDATE public.coinglass_chart_cache cc SET lease_id=gen_random_uuid(),lease_until=now()+interval '90 seconds'
          FROM candidate x WHERE (cc.tenant_id,cc.user_id,cc.instrument_id,cc.timeframe)=(x.tenant_id,x.user_id,x.instrument_id,x.timeframe)
          RETURNING cc.tenant_id,cc.user_id,cc.instrument_id,cc.timeframe,cc.lease_id
      $$;
      REVOKE ALL ON FUNCTION gatescope_security.seed_coinglass_charts(),gatescope_security.claim_coinglass_chart() FROM PUBLIC;
      GRANT EXECUTE ON FUNCTION gatescope_security.seed_coinglass_charts(),gatescope_security.claim_coinglass_chart() TO gatescope_market;
    """)


def downgrade():
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("""
      DROP FUNCTION gatescope_security.claim_coinglass_chart();
      DROP FUNCTION gatescope_security.seed_coinglass_charts();
      DROP FUNCTION gatescope_security.coinglass_chart_access(uuid,uuid);
      DROP FUNCTION gatescope_security.request_coinglass_chart(uuid,uuid,uuid,text);
      DROP TABLE coinglass_pattern_reviews;
      DROP TABLE coinglass_chart_cache;
      REVOKE SELECT ON market_sync_state,market_live_candles FROM gatescope_mcp;
    """)
