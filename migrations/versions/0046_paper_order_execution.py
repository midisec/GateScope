"""Owner-scoped, fully collateralised PAPER order execution (never live orders)."""

from alembic import op

revision = "0046_paper_order_execution"
down_revision = "0045_mcp_research_read"
branch_labels = depends_on = None


def _scopes(paper: bool) -> None:
    for table in ("public.mcp_tokens", "public.oauth_grants", "gatescope_security.oauth_registered_clients"):
        rows = op.get_bind().exec_driver_sql(
            "SELECT conname,pg_get_constraintdef(oid) FROM pg_constraint WHERE conrelid=%s::regclass AND contype='c'",
            (table,),
        ).fetchall()
        selected = [name for name, definition in rows if "scopes <@" in definition or "cardinality(scopes)" in definition]
        if len(selected) != (2 if table == "public.mcp_tokens" else 1):
            raise RuntimeError("UNEXPECTED_MCP_SCOPE_CONSTRAINTS")
        for name in selected:
            op.execute(f'ALTER TABLE {table} DROP CONSTRAINT "{name.replace(chr(34), chr(34) * 2)}"')
        values = ["mcp:connect", "market:read", "analysis:read", "research:read"]
        if table == "public.mcp_tokens":
            values.append("alerts:read")
            op.execute(f"ALTER TABLE {table} ADD CONSTRAINT mcp_scope_cardinality CHECK(cardinality(scopes) BETWEEN 1 AND {7 if paper else 5})")
        if paper:
            values += ["paper:read", "paper:trade"]
        allowed = ",".join("'" + value + "'" for value in values)
        op.execute(f"ALTER TABLE {table} ADD CONSTRAINT mcp_scope_allowlist CHECK(scopes <@ ARRAY[{allowed}]::text[])")
        if paper:
            op.execute(f"ALTER TABLE {table} ADD CONSTRAINT paper_trade_requires_read CHECK(NOT ('paper:trade'=ANY(scopes)) OR 'paper:read'=ANY(scopes))")
        else:
            op.execute(f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS paper_trade_requires_read")


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    _scopes(True)  # Existing grants/clients/tokens are deliberately not upgraded.
    rows = op.get_bind().exec_driver_sql("SELECT conname,pg_get_constraintdef(oid) FROM pg_constraint WHERE conrelid='public.mcp_audit_facts'::regclass AND contype='c'").fetchall()
    checks = [name for name, definition in rows if "outcome" in definition]
    if len(checks) != 1:
        raise RuntimeError("UNEXPECTED_MCP_AUDIT_CONSTRAINT")
    op.execute(f'ALTER TABLE mcp_audit_facts DROP CONSTRAINT "{checks[0].replace(chr(34), chr(34) * 2)}"')
    op.execute("ALTER TABLE mcp_audit_facts ADD CONSTRAINT mcp_audit_outcome CHECK(outcome IN ('REQUESTED','SUCCESS','DENIED','ERROR'))")
    op.execute("""
      ALTER TABLE paper_accounts ADD CONSTRAINT paper_accounts_owner_identity UNIQUE(tenant_id,owner_user_id,id);
      CREATE TABLE paper_orders (
        id uuid PRIMARY KEY, tenant_id uuid NOT NULL, owner_user_id uuid NOT NULL,
        account_id uuid NOT NULL, instrument_id uuid NOT NULL REFERENCES instruments(id),
        symbol text NOT NULL, timeframe text NOT NULL, position_id uuid,
        environment text NOT NULL DEFAULT 'PAPER' CHECK(environment='PAPER'),
        effect text NOT NULL CHECK(effect IN ('OPEN','CLOSE')),
        side text NOT NULL CHECK(side IN ('LONG','SHORT')),
        execution_side text NOT NULL CHECK(execution_side IN ('BUY','SELL')),
        order_type text NOT NULL CHECK(order_type IN ('MARKET','LIMIT')),
        time_in_force text NOT NULL CHECK(time_in_force IN ('IOC','GTC','FOK')),
        status text NOT NULL CHECK(status IN ('NEW','OPEN','PARTIALLY_FILLED','FILLED','CANCELED','REJECTED','EXPIRED')),
        contracts bigint NOT NULL CHECK(contracts>0), filled_contracts bigint NOT NULL DEFAULT 0,
        remaining_contracts bigint NOT NULL CHECK(remaining_contracts>=0),
        average_fill_price numeric(38,18), multiplier numeric(38,18) NOT NULL CHECK(multiplier>0),
        limit_price numeric(38,18), reference_price numeric(38,18) NOT NULL CHECK(reference_price>0),
        stop_price numeric(38,18), take_profit_price numeric(38,18),
        max_slippage_bps numeric(10,4) NOT NULL CHECK(max_slippage_bps BETWEEN 0 AND 100),
        fee_rate numeric(18,12) NOT NULL CHECK(fee_rate BETWEEN 0 AND .01),
        reserved_notional numeric(38,18) NOT NULL DEFAULT 0 CHECK(reserved_notional>=0),
        reserved_risk numeric(38,18) NOT NULL DEFAULT 0 CHECK(reserved_risk>=0),
        reserved_fees numeric(38,18) NOT NULL DEFAULT 0 CHECK(reserved_fees>=0),
        idempotency_key text NOT NULL CHECK(length(idempotency_key) BETWEEN 8 AND 128),
        request_hash text NOT NULL CHECK(request_hash ~ '^[0-9a-f]{64}$'), request_json jsonb NOT NULL,
        source text NOT NULL CHECK(source IN ('WEB','MCP','SYSTEM')), reason text,
        last_quote_id text, last_checked_at timestamptz,
        expires_at timestamptz NOT NULL, created_at timestamptz NOT NULL, updated_at timestamptz NOT NULL,
        version bigint NOT NULL DEFAULT 1,
        FOREIGN KEY(tenant_id,owner_user_id,account_id) REFERENCES paper_accounts(tenant_id,owner_user_id,id),
        UNIQUE(tenant_id,owner_user_id,id), UNIQUE(tenant_id,owner_user_id,idempotency_key),
        CHECK(filled_contracts>=0 AND filled_contracts+remaining_contracts<=contracts),
        CHECK(order_type='LIMIT' OR time_in_force='IOC'),
        CHECK((order_type='LIMIT')=(limit_price IS NOT NULL)),
        CHECK(effect='CLOSE' OR (stop_price>0 AND take_profit_price>0)),
        CHECK(expires_at>created_at AND expires_at<=created_at+interval '24 hours')
      );
      CREATE INDEX paper_orders_owner_time ON paper_orders(tenant_id,owner_user_id,account_id,created_at DESC,id DESC);
      CREATE INDEX paper_orders_pending ON paper_orders(last_checked_at NULLS FIRST,created_at,id)
        WHERE status IN ('NEW','OPEN','PARTIALLY_FILLED');
      CREATE TABLE paper_positions (
        id uuid PRIMARY KEY, tenant_id uuid NOT NULL, owner_user_id uuid NOT NULL,
        account_id uuid NOT NULL, opening_order_id uuid NOT NULL UNIQUE,
        instrument_id uuid NOT NULL REFERENCES instruments(id), symbol text NOT NULL, timeframe text NOT NULL,
        environment text NOT NULL DEFAULT 'PAPER' CHECK(environment='PAPER'),
        side text NOT NULL CHECK(side IN ('LONG','SHORT')),
        opened_contracts bigint NOT NULL CHECK(opened_contracts>0), closed_contracts bigint NOT NULL DEFAULT 0,
        remaining_contracts bigint NOT NULL CHECK(remaining_contracts>=0),
        reserved_close_contracts bigint NOT NULL DEFAULT 0,
        multiplier numeric(38,18) NOT NULL CHECK(multiplier>0),
        average_entry_price numeric(38,18) NOT NULL CHECK(average_entry_price>0), average_exit_price numeric(38,18),
        stop_price numeric(38,18) NOT NULL CHECK(stop_price>0), take_profit_price numeric(38,18) NOT NULL CHECK(take_profit_price>0),
        fee_rate numeric(18,12) NOT NULL,
        realized_pnl numeric(38,18) NOT NULL DEFAULT 0, fees numeric(38,18) NOT NULL DEFAULT 0,
        status text NOT NULL CHECK(status IN ('OPEN','CLOSED')),
        protection_status text NOT NULL DEFAULT 'ACTIVE' CHECK(protection_status IN ('ACTIVE','TRIGGERED','WAITING_MARKET','EXECUTION_RETRY','COMPLETED')),
        trigger_reason text CHECK(trigger_reason IN ('STOP_LOSS','TAKE_PROFIT')), triggered_at timestamptz,
        last_quote_id text, last_checked_at timestamptz, mark_price numeric(38,18), mark_as_of timestamptz,
        opened_at timestamptz NOT NULL, closed_at timestamptz, updated_at timestamptz NOT NULL,
        FOREIGN KEY(tenant_id,owner_user_id,account_id) REFERENCES paper_accounts(tenant_id,owner_user_id,id),
        FOREIGN KEY(tenant_id,owner_user_id,opening_order_id) REFERENCES paper_orders(tenant_id,owner_user_id,id),
        UNIQUE(tenant_id,owner_user_id,id),
        CHECK(opened_contracts=closed_contracts+remaining_contracts AND closed_contracts>=0),
        CHECK(reserved_close_contracts BETWEEN 0 AND remaining_contracts),
        CHECK((status='CLOSED')=(remaining_contracts=0))
      );
      ALTER TABLE paper_orders ADD CONSTRAINT paper_orders_position_owner
        FOREIGN KEY(tenant_id,owner_user_id,position_id) REFERENCES paper_positions(tenant_id,owner_user_id,id);
      CREATE INDEX paper_positions_owner_time ON paper_positions(tenant_id,owner_user_id,account_id,opened_at DESC,id DESC);
      CREATE INDEX paper_positions_open ON paper_positions(last_checked_at NULLS FIRST,id) WHERE status='OPEN';
      CREATE TABLE paper_order_fills (
        id uuid PRIMARY KEY, tenant_id uuid NOT NULL, owner_user_id uuid NOT NULL,
        account_id uuid NOT NULL, order_id uuid NOT NULL, position_id uuid NOT NULL,
        instrument_id uuid NOT NULL REFERENCES instruments(id), symbol text NOT NULL, timeframe text NOT NULL,
        environment text NOT NULL DEFAULT 'PAPER' CHECK(environment='PAPER'),
        effect text NOT NULL CHECK(effect IN ('OPEN','CLOSE')), side text NOT NULL CHECK(side IN ('LONG','SHORT')),
        execution_side text NOT NULL CHECK(execution_side IN ('BUY','SELL')),
        contracts bigint NOT NULL CHECK(contracts>0), price numeric(38,18) NOT NULL CHECK(price>0),
        base_quantity numeric(38,18) NOT NULL CHECK(base_quantity>0), notional numeric(38,18) NOT NULL CHECK(notional>0),
        fee numeric(38,18) NOT NULL CHECK(fee>=0), realized_pnl numeric(38,18) NOT NULL,
        quote_id text NOT NULL, quote_snapshot jsonb NOT NULL, reason text NOT NULL, filled_at timestamptz NOT NULL,
        FOREIGN KEY(tenant_id,owner_user_id,order_id) REFERENCES paper_orders(tenant_id,owner_user_id,id),
        FOREIGN KEY(tenant_id,owner_user_id,position_id) REFERENCES paper_positions(tenant_id,owner_user_id,id)
      );
      CREATE INDEX paper_fills_owner_time ON paper_order_fills(tenant_id,owner_user_id,account_id,filled_at DESC,id DESC);
      CREATE INDEX paper_fills_order ON paper_order_fills(order_id,filled_at,id);
      CREATE TABLE paper_order_events (
        id uuid PRIMARY KEY, tenant_id uuid NOT NULL, owner_user_id uuid NOT NULL,
        order_id uuid NOT NULL, event_type text NOT NULL, payload_json jsonb NOT NULL, created_at timestamptz NOT NULL,
        FOREIGN KEY(tenant_id,owner_user_id,order_id) REFERENCES paper_orders(tenant_id,owner_user_id,id)
      );
      CREATE INDEX paper_events_order ON paper_order_events(order_id,created_at,id);
      CREATE TABLE paper_order_commands (
        id uuid PRIMARY KEY, tenant_id uuid NOT NULL, owner_user_id uuid NOT NULL,
        order_id uuid NOT NULL, idempotency_key text NOT NULL, request_hash text NOT NULL,
        created_at timestamptz NOT NULL,
        FOREIGN KEY(tenant_id,owner_user_id,order_id) REFERENCES paper_orders(tenant_id,owner_user_id,id),
        UNIQUE(tenant_id,owner_user_id,idempotency_key)
      );
      CREATE TABLE paper_quote_consumption (
        quote_id text NOT NULL CHECK(length(quote_id) BETWEEN 1 AND 128),
        execution_side text NOT NULL CHECK(execution_side IN ('BUY','SELL')),
        price numeric(38,18) NOT NULL CHECK(price>0), contracts bigint NOT NULL CHECK(contracts>=0),
        updated_at timestamptz NOT NULL,
        PRIMARY KEY(quote_id,execution_side,price)
      );
      GRANT SELECT,INSERT ON paper_orders,paper_positions,paper_quote_consumption TO gatescope_api,gatescope_mcp,gatescope_paper;
      GRANT UPDATE(position_id,status,filled_contracts,remaining_contracts,average_fill_price,reserved_notional,reserved_risk,
        reserved_fees,fee_rate,reason,last_quote_id,last_checked_at,updated_at,version)
        ON paper_orders TO gatescope_api,gatescope_mcp,gatescope_paper;
      GRANT UPDATE(opened_contracts,closed_contracts,remaining_contracts,reserved_close_contracts,average_entry_price,average_exit_price,
        realized_pnl,fees,status,protection_status,trigger_reason,triggered_at,last_quote_id,last_checked_at,mark_price,mark_as_of,
        closed_at,updated_at) ON paper_positions TO gatescope_api,gatescope_mcp,gatescope_paper;
      GRANT UPDATE(contracts,updated_at) ON paper_quote_consumption TO gatescope_api,gatescope_mcp,gatescope_paper;
      GRANT SELECT,INSERT ON paper_order_fills,paper_order_events,paper_order_commands TO gatescope_api,gatescope_mcp,gatescope_paper;
      GRANT UPDATE(realized_pnl,etag,updated_at) ON paper_accounts TO gatescope_mcp;
      GRANT INSERT ON outbox_events TO gatescope_mcp;
      GRANT SELECT ON paper_orders,paper_positions TO gatescope_notification;
      CREATE POLICY mcp_paper_account_owner ON paper_accounts AS RESTRICTIVE TO gatescope_mcp
        USING(owner_user_id=gatescope_security.current_subject_id())
        WITH CHECK(owner_user_id=gatescope_security.current_subject_id());
      CREATE FUNCTION gatescope_security.paper_actor_can_trade() RETURNS boolean
        LANGUAGE sql SECURITY DEFINER STABLE SET search_path=pg_catalog,public AS $$
          SELECT EXISTS(SELECT 1 FROM public.memberships m JOIN public.users u ON u.id=m.user_id
            JOIN public.tenants t ON t.id=m.tenant_id
            WHERE m.tenant_id=gatescope_security.current_tenant_id()
              AND m.user_id=gatescope_security.current_subject_id()
              AND m.status='ACTIVE' AND u.status='ACTIVE' AND t.status='ACTIVE' AND m.role IN ('OWNER','OPERATOR'))
        $$;
      REVOKE ALL ON FUNCTION gatescope_security.paper_actor_can_trade() FROM PUBLIC;
      GRANT EXECUTE ON FUNCTION gatescope_security.paper_actor_can_trade() TO gatescope_api,gatescope_mcp,gatescope_paper;
      CREATE FUNCTION gatescope_security.paper_order_due_targets(requested_limit integer)
        RETURNS TABLE(tenant_id uuid,owner_user_id uuid,account_id uuid,instrument_id uuid,symbol text,timeframe text)
        LANGUAGE sql SECURITY DEFINER STABLE SET search_path=pg_catalog,public AS $$
          SELECT q.tenant_id,q.owner_user_id,q.account_id,q.instrument_id,q.symbol,q.timeframe FROM (
            SELECT o.tenant_id,o.owner_user_id,o.account_id,o.instrument_id,o.symbol,o.timeframe,o.last_checked_at AS checked_at
              FROM public.paper_orders o WHERE o.status IN ('NEW','OPEN','PARTIALLY_FILLED')
            UNION ALL
            SELECT p.tenant_id,p.owner_user_id,p.account_id,p.instrument_id,p.symbol,p.timeframe,p.last_checked_at
              FROM public.paper_positions p WHERE p.status='OPEN'
          ) q GROUP BY q.tenant_id,q.owner_user_id,q.account_id,q.instrument_id,q.symbol,q.timeframe
            ORDER BY min(coalesce(q.checked_at,'-infinity'::timestamptz)),q.tenant_id,q.owner_user_id,q.account_id,q.instrument_id,q.symbol,q.timeframe
            LIMIT LEAST(GREATEST(requested_limit,1),100)
        $$;
      REVOKE ALL ON FUNCTION gatescope_security.paper_order_due_targets(integer) FROM PUBLIC;
      GRANT EXECUTE ON FUNCTION gatescope_security.paper_order_due_targets(integer) TO gatescope_paper;
      CREATE OR REPLACE FUNCTION gatescope_security.notification_outbox_due_tenants(requested_limit integer)
        RETURNS TABLE(tenant_id uuid) LANGUAGE sql SECURITY DEFINER STABLE
        SET search_path=pg_catalog,public AS $$
          SELECT DISTINCT oe.tenant_id FROM public.outbox_events oe
          WHERE oe.published_at IS NULL AND oe.dead_lettered_at IS NULL
            AND oe.next_attempt_at<=statement_timestamp()
            AND (oe.lease_expires_at IS NULL OR oe.lease_expires_at<statement_timestamp())
            AND oe.event_type IN ('notification.requested.v1','notification.test.requested.v1','paper.order.fill.v1')
          ORDER BY oe.tenant_id LIMIT LEAST(GREATEST(requested_limit,1),100)
        $$;
    """)
    for table in ("paper_orders", "paper_positions", "paper_order_fills", "paper_order_events", "paper_order_commands"):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY; ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(f"""CREATE POLICY own_paper_orders ON {table} USING (
          tenant_id=gatescope_security.current_tenant_id() AND owner_user_id=gatescope_security.current_subject_id()
        ) WITH CHECK(tenant_id=gatescope_security.current_tenant_id() AND owner_user_id=gatescope_security.current_subject_id())""")
    for table in ("paper_order_fills", "paper_order_events", "paper_order_commands"):
        op.execute(f"""CREATE TRIGGER {table}_immutable BEFORE UPDATE OR DELETE ON {table}
          FOR EACH ROW EXECUTE FUNCTION gatescope_security.reject_fact_mutation()""")


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    if op.get_bind().exec_driver_sql("SELECT EXISTS(SELECT 1 FROM paper_orders) OR EXISTS(SELECT 1 FROM mcp_audit_facts WHERE outcome='REQUESTED')").scalar():
        raise RuntimeError("PAPER_FACTS_EXIST_FORWARD_FIX_REQUIRED")
    op.execute("""
      UPDATE mcp_tokens t SET revoked_at=coalesce(t.revoked_at,now()) WHERE t.scopes && ARRAY['paper:read','paper:trade']::text[]
        OR EXISTS(SELECT 1 FROM oauth_access_tokens a JOIN oauth_grants g ON g.id=a.grant_id
          WHERE a.token_id=t.id AND g.scopes && ARRAY['paper:read','paper:trade']::text[]);
      UPDATE oauth_codes c SET used_at=coalesce(c.used_at,now()) FROM oauth_grants g
        WHERE c.grant_id=g.id AND g.scopes && ARRAY['paper:read','paper:trade']::text[];
      UPDATE oauth_refresh_tokens r SET used_at=coalesce(r.used_at,now()) FROM oauth_grants g
        WHERE r.grant_id=g.id AND g.scopes && ARRAY['paper:read','paper:trade']::text[];
      UPDATE gatescope_security.oauth_pending_requests SET consumed_at=coalesce(consumed_at,now())
        WHERE scopes && ARRAY['paper:read','paper:trade']::text[];
      UPDATE oauth_grants SET revoked_at=coalesce(revoked_at,now()),scopes=array_remove(array_remove(scopes,'paper:read'),'paper:trade')
        WHERE scopes && ARRAY['paper:read','paper:trade']::text[];
      UPDATE mcp_tokens SET scopes=array_remove(array_remove(scopes,'paper:read'),'paper:trade')
        WHERE scopes && ARRAY['paper:read','paper:trade']::text[];
      UPDATE gatescope_security.oauth_registered_clients SET scopes=array_remove(array_remove(scopes,'paper:read'),'paper:trade')
        WHERE scopes && ARRAY['paper:read','paper:trade']::text[];
      DROP FUNCTION gatescope_security.paper_order_due_targets(integer);
      DROP FUNCTION gatescope_security.paper_actor_can_trade();
      DROP POLICY mcp_paper_account_owner ON paper_accounts;
      REVOKE UPDATE(realized_pnl,etag,updated_at) ON paper_accounts FROM gatescope_mcp;
      REVOKE INSERT ON outbox_events FROM gatescope_mcp;
      ALTER TABLE paper_orders DROP CONSTRAINT paper_orders_position_owner;
      DROP TABLE paper_order_commands,paper_order_events,paper_order_fills,paper_positions,paper_orders,paper_quote_consumption;
      ALTER TABLE paper_accounts DROP CONSTRAINT paper_accounts_owner_identity;
    """)
    _scopes(False)
    op.execute("ALTER TABLE mcp_audit_facts DROP CONSTRAINT mcp_audit_outcome; ALTER TABLE mcp_audit_facts ADD CONSTRAINT mcp_audit_outcome CHECK(outcome IN ('SUCCESS','DENIED','ERROR'))")
    op.execute("""CREATE OR REPLACE FUNCTION gatescope_security.notification_outbox_due_tenants(requested_limit integer)
        RETURNS TABLE(tenant_id uuid) LANGUAGE sql SECURITY DEFINER STABLE SET search_path=pg_catalog,public AS $$
          SELECT DISTINCT oe.tenant_id FROM public.outbox_events oe
          WHERE oe.published_at IS NULL AND oe.dead_lettered_at IS NULL AND oe.next_attempt_at<=statement_timestamp()
            AND (oe.lease_expires_at IS NULL OR oe.lease_expires_at<statement_timestamp())
            AND oe.event_type IN ('notification.requested.v1','notification.test.requested.v1')
          ORDER BY oe.tenant_id LIMIT LEAST(GREATEST(requested_limit,1),100)
        $$""")
