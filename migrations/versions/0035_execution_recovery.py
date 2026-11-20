"""Keep frozen TESTNET exposure in the durable reconciliation queue."""

from alembic import op

revision = "0035_execution_recovery"
down_revision = "0034_performance_observation"
branch_labels = depends_on = None


def _due_function(states: str) -> None:
    op.execute(f"""
        CREATE OR REPLACE FUNCTION gatescope_security.testnet_execution_due_tenants(
          requested_limit integer
        ) RETURNS TABLE(tenant_id uuid)
        LANGUAGE sql SECURITY DEFINER STABLE
        SET search_path = pg_catalog, public AS $$
          SELECT DISTINCT drill.tenant_id FROM public.testnet_execution_drills drill
          WHERE drill.state='QUEUED'
             OR (drill.state='PROCESSING' AND drill.lease_expires_at <= now())
             OR (drill.state IN ({states}) AND drill.next_reconcile_at <= now()
                 AND (drill.monitor_lease_expires_at IS NULL OR drill.monitor_lease_expires_at <= now()))
          ORDER BY drill.tenant_id LIMIT LEAST(GREATEST(requested_limit,1),100)
        $$;
        REVOKE ALL ON FUNCTION gatescope_security.testnet_execution_due_tenants(integer) FROM PUBLIC;
        GRANT EXECUTE ON FUNCTION gatescope_security.testnet_execution_due_tenants(integer) TO gatescope_executor;
    """)


def _state_constraint(include_pending: bool) -> None:
    extra = ",'RECONCILIATION_PENDING'" if include_pending else ""
    op.execute(f"""
        ALTER TABLE testnet_execution_drills ADD CONSTRAINT testnet_execution_drills_state_check
        CHECK (state IN ('QUEUED','PROCESSING','ENTRY_UNFILLED','ENTRY_UNKNOWN','PROTECTED',
                        'CLOSED_PROTECTED','CLOSED_FAIL_SAFE','PREFLIGHT_REJECTED',
                        'MANUAL_INTERVENTION_REQUIRED'{extra}));
    """)


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("""
        DO $$ DECLARE item record;
        BEGIN
          FOR item IN SELECT conname FROM pg_constraint
            WHERE conrelid='public.testnet_execution_drills'::regclass AND contype='c'
              AND (conname='testnet_execution_drills_state_check'
                   OR pg_get_constraintdef(oid) LIKE '%next_reconcile_at%')
          LOOP
            EXECUTE format('ALTER TABLE public.testnet_execution_drills DROP CONSTRAINT %I', item.conname);
          END LOOP;
        END $$;
    """)
    _state_constraint(True)
    op.execute("""
        UPDATE testnet_execution_drills
        SET state='RECONCILIATION_PENDING',frozen=true,next_reconcile_at=now(),
            monitor_lease_owner=NULL,monitor_lease_expires_at=NULL,version=version+1,updated_at=now()
        WHERE state='ENTRY_UNKNOWN' OR (state='MANUAL_INTERVENTION_REQUIRED' AND safe_code IN (
          'EXECUTOR_LEASE_EXPIRED','UNCLASSIFIED_EXECUTION_FAILURE',
          'UNCLASSIFIED_PROTECTION_MONITOR_FAILURE','PROTECTED_POSITION_SIZE_MISMATCH',
          'FAIL_SAFE_EXIT_FACT_UNPROVABLE','FAIL_SAFE_POSITION_STILL_OPEN',
          'DEGRADED_PROTECTION_EXIT_NOT_PROVABLE','ORPHAN_PROTECTION_CANCEL_NOT_PROVABLE'));
        UPDATE exchange_accounts account SET status='EXECUTION_FROZEN',trading_ready=false,
          execution_freeze_code=coalesce(drill.safe_code,'EXECUTION_RECONCILIATION_REQUIRED'),
          etag=account.etag+1,updated_at=now()
        FROM testnet_execution_drills drill
        WHERE drill.exchange_account_id=account.id AND drill.state='RECONCILIATION_PENDING';
        ALTER TABLE testnet_execution_drills ADD CONSTRAINT testnet_execution_drills_reconcile_state_check
          CHECK ((state IN ('PROTECTED','RECONCILIATION_PENDING')) = (next_reconcile_at IS NOT NULL));
        ALTER TABLE testnet_execution_drills ADD CONSTRAINT testnet_execution_drills_pending_frozen_check
          CHECK (state<>'RECONCILIATION_PENDING' OR frozen);
        CREATE INDEX testnet_execution_reconcile_due_idx ON testnet_execution_drills(next_reconcile_at,id)
          WHERE state IN ('PROTECTED','RECONCILIATION_PENDING');
    """)
    _due_function("'PROTECTED','RECONCILIATION_PENDING'")


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("""
        DROP INDEX testnet_execution_reconcile_due_idx;
        ALTER TABLE testnet_execution_drills DROP CONSTRAINT testnet_execution_drills_reconcile_state_check;
        ALTER TABLE testnet_execution_drills DROP CONSTRAINT testnet_execution_drills_pending_frozen_check;
        ALTER TABLE testnet_execution_drills DROP CONSTRAINT testnet_execution_drills_state_check;
        UPDATE testnet_execution_drills SET state='MANUAL_INTERVENTION_REQUIRED',
          next_reconcile_at=NULL,monitor_lease_owner=NULL,monitor_lease_expires_at=NULL,
          frozen=true,version=version+1,updated_at=now() WHERE state='RECONCILIATION_PENDING';
        ALTER TABLE testnet_execution_drills ADD CONSTRAINT testnet_execution_drills_reconcile_state_check
          CHECK ((state='PROTECTED') = (next_reconcile_at IS NOT NULL));
    """)
    _state_constraint(False)
    _due_function("'PROTECTED'")
