"""Budget-aware default-period warming, with interactive periods on demand."""
from alembic import op

revision="0041_coinglass_scheduling"
down_revision="0040_coinglass_context"
branch_labels=depends_on=None


def _install(default_only):
    if op.get_bind().dialect.name!="postgresql":return
    selection="i.default_timeframe" if default_only else "t.tf"
    cross="" if default_only else "CROSS JOIN (VALUES('1m'),('5m'),('15m'),('30m'),('1h'),('4h'),('8h'),('1d')) t(tf)"
    selected="AND i.default_timeframe IN ('1m','5m','15m','30m','1h','4h','8h','1d')" if default_only else ""
    match="AND wi.default_timeframe=cc.timeframe" if default_only else ""
    op.execute(f"""
      CREATE OR REPLACE FUNCTION gatescope_security.seed_coinglass_charts() RETURNS integer
      LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
      DECLARE added integer;
      BEGIN
        INSERT INTO public.coinglass_chart_cache(tenant_id,user_id,instrument_id,timeframe,config_id,secret_version,requested_at)
        SELECT DISTINCT c.tenant_id,c.created_by,i.instrument_id,{selection},c.id,c.secret_version,'epoch'::timestamptz
        FROM public.coinglass_configs c
        JOIN public.watchlists w ON w.tenant_id=c.tenant_id AND w.owner_user_id=c.created_by AND w.deleted_at IS NULL
        JOIN public.watchlist_items i ON i.watchlist_id=w.id AND i.tenant_id=w.tenant_id
        {cross} WHERE c.status='ACTIVE' {selected}
        LIMIT 4096
        ON CONFLICT(tenant_id,user_id,instrument_id,timeframe) DO UPDATE
          SET config_id=EXCLUDED.config_id,secret_version=EXCLUDED.secret_version,payload='{{}}',due_at=now(),lease_id=NULL,lease_until=NULL
          WHERE coinglass_chart_cache.config_id<>EXCLUDED.config_id OR coinglass_chart_cache.secret_version<>EXCLUDED.secret_version;
        GET DIAGNOSTICS added=ROW_COUNT;
        RETURN added;
      END $$;
      CREATE OR REPLACE FUNCTION gatescope_security.claim_coinglass_chart()
      RETURNS TABLE(tenant_id uuid,user_id uuid,instrument_id uuid,timeframe text,lease_id uuid)
      LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
        WITH candidate AS (
          SELECT cc.tenant_id,cc.user_id,cc.instrument_id,cc.timeframe FROM public.coinglass_chart_cache cc
          JOIN public.coinglass_configs c ON c.id=cc.config_id AND c.created_by=cc.user_id AND c.status='ACTIVE'
          WHERE cc.due_at<=now() AND (cc.lease_until IS NULL OR cc.lease_until<now())
            AND (cc.requested_at>now()-interval '2 hours' OR EXISTS(
              SELECT 1 FROM public.watchlist_items wi JOIN public.watchlists w ON wi.watchlist_id=w.id
              WHERE w.tenant_id=cc.tenant_id AND w.owner_user_id=cc.user_id AND w.deleted_at IS NULL
                AND wi.instrument_id=cc.instrument_id {match}))
          ORDER BY (cc.requested_at>now()-interval '5 minutes') DESC,cc.due_at,cc.requested_at DESC,cc.instrument_id,cc.timeframe
          FOR UPDATE OF cc SKIP LOCKED LIMIT 1
        ) UPDATE public.coinglass_chart_cache cc SET lease_id=gen_random_uuid(),lease_until=now()+interval '90 seconds'
          FROM candidate x WHERE (cc.tenant_id,cc.user_id,cc.instrument_id,cc.timeframe)=(x.tenant_id,x.user_id,x.instrument_id,x.timeframe)
          RETURNING cc.tenant_id,cc.user_id,cc.instrument_id,cc.timeframe,cc.lease_id
      $$;
    """)


def upgrade():_install(True)
def downgrade():_install(False)
