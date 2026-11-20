"""Immutable owner-scoped decision evidence for the shared PAPER ledger."""
from alembic import op

revision = "0048_paper_decision_evidence"
down_revision = "0047_structure_storage"
branch_labels = depends_on = None


def upgrade():
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("""
      CREATE TABLE paper_decision_snapshots (
        id uuid PRIMARY KEY, tenant_id uuid NOT NULL REFERENCES tenants(id),
        owner_user_id uuid NOT NULL REFERENCES users(id),
        instrument_id uuid NOT NULL REFERENCES instruments(id), timeframe text NOT NULL,
        observed_at timestamptz NOT NULL, captured_at timestamptz NOT NULL,
        evidence_digest text NOT NULL CHECK(evidence_digest ~ '^[0-9a-f]{64}$'),
        evidence_json jsonb NOT NULL CHECK(jsonb_typeof(evidence_json)='object'),
        UNIQUE(tenant_id,owner_user_id,id), CHECK(observed_at<=captured_at),
        CHECK(octet_length(evidence_json::text)<=524288)
      );
      CREATE INDEX paper_decisions_owner_time ON paper_decision_snapshots
        (tenant_id,owner_user_id,captured_at DESC,id DESC);
      ALTER TABLE paper_decision_snapshots ENABLE ROW LEVEL SECURITY;
      ALTER TABLE paper_decision_snapshots FORCE ROW LEVEL SECURITY;
      CREATE POLICY own_paper_decisions ON paper_decision_snapshots USING (
        tenant_id=gatescope_security.current_tenant_id() AND owner_user_id=gatescope_security.current_subject_id()
      ) WITH CHECK(tenant_id=gatescope_security.current_tenant_id() AND owner_user_id=gatescope_security.current_subject_id());
      CREATE TRIGGER paper_decisions_immutable BEFORE UPDATE OR DELETE ON paper_decision_snapshots
        FOR EACH ROW EXECUTE FUNCTION gatescope_security.reject_fact_mutation();
      GRANT SELECT,INSERT ON paper_decision_snapshots TO gatescope_api,gatescope_mcp;
      GRANT SELECT ON paper_decision_snapshots TO gatescope_paper,gatescope_notification;
      GRANT SELECT ON worker_heartbeats TO gatescope_mcp,gatescope_notification;
      GRANT SELECT ON paper_accounts,trades,trade_state_current,paper_order_fills,paper_order_events
        TO gatescope_notification;
      CREATE POLICY notification_paper_account_owner ON paper_accounts AS RESTRICTIVE
        FOR SELECT TO gatescope_notification USING(owner_user_id=gatescope_security.current_subject_id());
      CREATE POLICY notification_paper_trade_owner ON trades AS RESTRICTIVE
        FOR SELECT TO gatescope_notification USING(owner_user_id=gatescope_security.current_subject_id());
      CREATE POLICY notification_paper_trade_state_owner ON trade_state_current AS RESTRICTIVE
        FOR SELECT TO gatescope_notification USING(EXISTS(SELECT 1 FROM public.trades t
          WHERE t.id=trade_id AND t.tenant_id=trade_state_current.tenant_id
            AND t.owner_user_id=gatescope_security.current_subject_id()));
      ALTER TABLE paper_orders ADD COLUMN decision_snapshot_id uuid;
      ALTER TABLE paper_orders ADD CONSTRAINT paper_order_decision_owner
        FOREIGN KEY(tenant_id,owner_user_id,decision_snapshot_id)
        REFERENCES paper_decision_snapshots(tenant_id,owner_user_id,id);
    """)


def downgrade():
    if op.get_bind().dialect.name != "postgresql":
        return
    if op.get_bind().exec_driver_sql("SELECT EXISTS(SELECT 1 FROM paper_decision_snapshots)").scalar():
        raise RuntimeError("PAPER_DECISION_FACTS_EXIST_FORWARD_FIX_REQUIRED")
    op.execute("""ALTER TABLE paper_orders DROP COLUMN decision_snapshot_id; DROP TABLE paper_decision_snapshots;
      DROP POLICY notification_paper_account_owner ON paper_accounts;
      DROP POLICY notification_paper_trade_owner ON trades;
      DROP POLICY notification_paper_trade_state_owner ON trade_state_current;
      REVOKE SELECT ON paper_accounts,trades,trade_state_current,paper_order_fills,paper_order_events
        FROM gatescope_notification;
      REVOKE SELECT ON worker_heartbeats FROM gatescope_mcp,gatescope_notification""")
