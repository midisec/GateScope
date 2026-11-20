"""Immutable, user-isolated mining reports without adding executable strategies."""
from alembic import op

revision="0027_discovery_reports"
down_revision="0026_performance_admission"
branch_labels=depends_on=None


def upgrade():
    if op.get_bind().dialect.name!="postgresql":return
    op.execute("""
      CREATE TABLE research_discovery_reports (
        id uuid PRIMARY KEY,tenant_id uuid NOT NULL REFERENCES tenants(id),
        owner_user_id uuid NOT NULL REFERENCES users(id),name text NOT NULL,
        trial_count integer NOT NULL CHECK(trial_count>0),report_json jsonb NOT NULL,
        artifact_digest text NOT NULL CHECK(artifact_digest ~ '^[a-f0-9]{64}$'),
        created_at timestamptz NOT NULL DEFAULT now(),UNIQUE(tenant_id,owner_user_id,artifact_digest)
      );
      CREATE INDEX discovery_owner_created ON research_discovery_reports(tenant_id,owner_user_id,created_at DESC);
      ALTER TABLE research_discovery_reports ENABLE ROW LEVEL SECURITY;
      ALTER TABLE research_discovery_reports FORCE ROW LEVEL SECURITY;
      CREATE POLICY own_discovery ON research_discovery_reports USING (
        tenant_id=gatescope_security.current_tenant_id() AND owner_user_id=gatescope_security.current_subject_id()
      ) WITH CHECK (
        tenant_id=gatescope_security.current_tenant_id() AND owner_user_id=gatescope_security.current_subject_id()
      );
      CREATE TRIGGER discovery_immutable BEFORE UPDATE OR DELETE ON research_discovery_reports
        FOR EACH ROW EXECUTE FUNCTION gatescope_security.reject_fact_mutation();
      GRANT SELECT ON research_discovery_reports TO gatescope_api;
      GRANT SELECT,INSERT ON research_discovery_reports TO gatescope_strategy;
    """)


def downgrade():
    if op.get_bind().dialect.name=="postgresql":op.execute("DROP TABLE research_discovery_reports")
