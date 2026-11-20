"""Append-only alert facts and their transactional notification outbox.

Revision ID: 0003_alert_facts
Revises: 0002_postgres_foundation
"""

from __future__ import annotations

from alembic import op

revision = "0003_alert_facts"
down_revision = "0002_postgres_foundation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute(
        """
        CREATE TABLE alert_event_facts (
          id uuid PRIMARY KEY,
          tenant_id uuid NOT NULL REFERENCES tenants(id),
          alert_rule_id uuid NOT NULL REFERENCES alert_rules(id),
          alert_rule_version_id uuid NOT NULL REFERENCES alert_rule_versions(id),
          event_key text NOT NULL,
          instrument_id uuid NOT NULL REFERENCES instruments(id),
          timeframe text NOT NULL,
          available_at timestamptz NOT NULL,
          matched_fact_ids text[] NOT NULL,
          payload_json jsonb NOT NULL,
          payload_hash text NOT NULL,
          created_at timestamptz NOT NULL,
          UNIQUE (tenant_id, event_key)
        );
        CREATE INDEX alert_event_facts_time_idx
          ON alert_event_facts(tenant_id, available_at DESC, id DESC);
        ALTER TABLE alert_event_facts ENABLE ROW LEVEL SECURITY;
        ALTER TABLE alert_event_facts FORCE ROW LEVEL SECURITY;
        CREATE POLICY tenant_isolation ON alert_event_facts
          USING (tenant_id = gatescope_security.current_tenant_id())
          WITH CHECK (tenant_id = gatescope_security.current_tenant_id());

        CREATE OR REPLACE FUNCTION gatescope_security.reject_fact_mutation()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          RAISE EXCEPTION 'immutable fact cannot be updated or deleted' USING ERRCODE = '55000';
        END
        $$;
        CREATE TRIGGER alert_event_facts_immutable
          BEFORE UPDATE OR DELETE ON alert_event_facts
          FOR EACH ROW EXECUTE FUNCTION gatescope_security.reject_fact_mutation();

        GRANT EXECUTE ON FUNCTION gatescope_security.reject_fact_mutation() TO gatescope_api, gatescope_strategy;
        GRANT SELECT ON alert_event_facts TO gatescope_api;
        GRANT SELECT, INSERT ON alert_event_facts TO gatescope_strategy;
        """
    )


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("DROP TABLE IF EXISTS alert_event_facts CASCADE")
    op.execute("DROP FUNCTION IF EXISTS gatescope_security.reject_fact_mutation()")
