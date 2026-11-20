"""Forward-only, owner-isolated shadow observations; never exchange orders."""
from alembic import op

revision = "0038_shadow_ledger"
down_revision = "0037_structure_research"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("""
      CREATE TABLE shadow_runtime (
        singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
        enabled_at timestamptz NOT NULL DEFAULT clock_timestamp()
      );
      INSERT INTO shadow_runtime(singleton) VALUES(true);
      CREATE TABLE shadow_alert_captures (
        alert_event_id uuid PRIMARY KEY REFERENCES alert_event_facts(id),
        tenant_id uuid NOT NULL REFERENCES tenants(id),
        owner_user_id uuid NOT NULL REFERENCES users(id),
        created_at timestamptz NOT NULL DEFAULT now()
      );
      CREATE TABLE shadow_trade_facts (
        id uuid PRIMARY KEY, tenant_id uuid NOT NULL REFERENCES tenants(id),
        owner_user_id uuid NOT NULL REFERENCES users(id),
        alert_event_id uuid NOT NULL REFERENCES alert_event_facts(id),
        matched_index integer NOT NULL CHECK(matched_index >= 0),
        instrument_id uuid NOT NULL REFERENCES instruments(id), timeframe text NOT NULL,
        pattern_id text NOT NULL, pattern_name text NOT NULL,
        cohort text NOT NULL CHECK(cohort IN ('SIGNAL','DELIVERED')),
        signal_at timestamptz NOT NULL, expires_at timestamptz NOT NULL,
        engine_version text NOT NULL, source_event_json jsonb NOT NULL,
        plan_json jsonb NOT NULL, policy_json jsonb NOT NULL,
        environment text NOT NULL DEFAULT 'SHADOW' CHECK(environment='SHADOW'),
        forward_only boolean NOT NULL DEFAULT true CHECK(forward_only),
        created_at timestamptz NOT NULL DEFAULT now(),
        UNIQUE(tenant_id,owner_user_id,id),
        UNIQUE(alert_event_id,matched_index,cohort)
      );
      CREATE INDEX shadow_facts_owner_time ON shadow_trade_facts(tenant_id,owner_user_id,created_at DESC,id DESC);
      CREATE INDEX shadow_facts_alert ON shadow_trade_facts(alert_event_id);
      CREATE TABLE shadow_trade_states (
        trade_id uuid PRIMARY KEY,
        tenant_id uuid NOT NULL, owner_user_id uuid NOT NULL,
        state text NOT NULL CHECK(state IN ('WAITING_DELIVERY','PENDING','OPEN','CLOSED','SKIPPED','EXPIRED')),
        reason_code text, eligible_at timestamptz, delivered_at timestamptz,
        last_bar_time timestamptz, state_json jsonb NOT NULL DEFAULT '{}',
        result_json jsonb NOT NULL DEFAULT '{}',
        sequence integer NOT NULL DEFAULT 1 CHECK(sequence > 0),
        checked_at timestamptz NOT NULL DEFAULT '-infinity',
        updated_at timestamptz NOT NULL DEFAULT now(),
        FOREIGN KEY(tenant_id,owner_user_id,trade_id) REFERENCES shadow_trade_facts(tenant_id,owner_user_id,id)
      );
      CREATE INDEX shadow_states_active ON shadow_trade_states(checked_at,trade_id)
        WHERE state IN ('WAITING_DELIVERY','PENDING','OPEN');
      CREATE TABLE shadow_trade_events (
        id uuid PRIMARY KEY, tenant_id uuid NOT NULL, owner_user_id uuid NOT NULL,
        trade_id uuid NOT NULL, sequence integer NOT NULL CHECK(sequence > 0),
        event_type text NOT NULL, observed_at timestamptz NOT NULL,
        payload_json jsonb NOT NULL,
        FOREIGN KEY(tenant_id,owner_user_id,trade_id) REFERENCES shadow_trade_facts(tenant_id,owner_user_id,id),
        UNIQUE(trade_id,sequence)
      );
      CREATE INDEX shadow_alert_created_idx ON alert_event_facts(created_at,id);
      GRANT SELECT ON shadow_runtime TO gatescope_api,gatescope_paper;
      GRANT SELECT ON alert_event_facts,alert_rules,notification_deliveries TO gatescope_paper;
      GRANT SELECT ON shadow_alert_captures,shadow_trade_facts,shadow_trade_states,shadow_trade_events TO gatescope_api;
      GRANT SELECT,INSERT ON shadow_alert_captures,shadow_trade_facts,shadow_trade_states,shadow_trade_events TO gatescope_paper;
      GRANT UPDATE ON shadow_trade_states TO gatescope_paper;
      GRANT SELECT,INSERT,UPDATE ON worker_heartbeats TO gatescope_paper;
      CREATE FUNCTION gatescope_security.shadow_due_owners(requested_limit integer)
        RETURNS TABLE(tenant_id uuid,owner_user_id uuid)
        LANGUAGE sql SECURITY DEFINER STABLE SET search_path=pg_catalog,public AS $$
          SELECT q.tenant_id,q.owner_user_id FROM (
            SELECT af.tenant_id,ar.owner_user_id,min(af.created_at) AS due_at
            FROM public.alert_event_facts af JOIN public.alert_rules ar ON ar.id=af.alert_rule_id
            CROSS JOIN public.shadow_runtime rt
            WHERE af.created_at >= rt.enabled_at
              AND NOT EXISTS(SELECT 1 FROM public.shadow_alert_captures sc WHERE sc.alert_event_id=af.id)
            GROUP BY af.tenant_id,ar.owner_user_id
            UNION ALL
            SELECT ss.tenant_id,ss.owner_user_id,min(ss.checked_at) AS due_at
            FROM public.shadow_trade_states ss
            WHERE ss.state IN ('WAITING_DELIVERY','PENDING','OPEN')
            GROUP BY ss.tenant_id,ss.owner_user_id
          ) q GROUP BY q.tenant_id,q.owner_user_id ORDER BY min(q.due_at),q.tenant_id,q.owner_user_id
          LIMIT LEAST(GREATEST(requested_limit,1),100)
        $$;
      REVOKE ALL ON FUNCTION gatescope_security.shadow_due_owners(integer) FROM PUBLIC;
      GRANT EXECUTE ON FUNCTION gatescope_security.shadow_due_owners(integer) TO gatescope_paper;
    """)
    for table in ("shadow_alert_captures", "shadow_trade_facts", "shadow_trade_states", "shadow_trade_events"):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(f"""CREATE POLICY own_shadow ON {table} USING (
          tenant_id=gatescope_security.current_tenant_id() AND owner_user_id=gatescope_security.current_subject_id()
        ) WITH CHECK (
          tenant_id=gatescope_security.current_tenant_id() AND owner_user_id=gatescope_security.current_subject_id()
        )""")
    for table in ("shadow_alert_captures", "shadow_trade_facts", "shadow_trade_events", "shadow_runtime"):
        op.execute(f"""CREATE TRIGGER {table}_immutable BEFORE UPDATE OR DELETE ON {table}
          FOR EACH ROW EXECUTE FUNCTION gatescope_security.reject_fact_mutation()""")


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("""
      DROP FUNCTION gatescope_security.shadow_due_owners(integer);
      DROP TABLE shadow_trade_events,shadow_trade_states,shadow_trade_facts,shadow_alert_captures,shadow_runtime;
      DROP INDEX shadow_alert_created_idx;
      REVOKE SELECT ON alert_event_facts,alert_rules,notification_deliveries FROM gatescope_paper;
      REVOKE SELECT,INSERT,UPDATE ON worker_heartbeats FROM gatescope_paper;
    """)
