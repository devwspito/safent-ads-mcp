"""ADS-01: durable budget reservations and unknown remote outcomes."""

from alembic import op

revision = "0034_execution_reservations"
down_revision = "0033_crm_bridge_health"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM executions WHERE outcome = 'RUNNING'
                   OR (outcome = 'CLAIMED' AND started_at IS NOT NULL)
                   OR (outcome = 'FAILED' AND error_code LIKE 'platform_write_error:%')) THEN
            RAISE EXCEPTION 'ADS-01: reconcile legacy in-flight writes before upgrade';
        END IF;
    END $$""")
    op.execute("ALTER TABLE executions DROP CONSTRAINT executions_outcome_check")
    op.execute("""ALTER TABLE executions ADD CONSTRAINT executions_outcome_check
        CHECK (outcome IN ('CLAIMED','RUNNING','UNKNOWN','SUCCEEDED','FAILED',
                          'SKIPPED_DRIFT','BLOCKED_GUARDRAIL','BLOCKED_BRAKE'))""")
    op.execute("ALTER TABLE executions DROP CONSTRAINT executions_terminal_is_finished_check")
    op.execute("""ALTER TABLE executions ADD CONSTRAINT executions_terminal_is_finished_check
        CHECK (outcome IN ('CLAIMED','RUNNING','UNKNOWN') OR finished_at IS NOT NULL)""")
    op.execute("""CREATE TABLE execution_reservations (
        execution_id UUID PRIMARY KEY REFERENCES executions(id) ON DELETE RESTRICT,
        business_id UUID NOT NULL REFERENCES businesses(id) ON DELETE RESTRICT,
        platform_account_id UUID NOT NULL REFERENCES platform_accounts(id) ON DELETE RESTRICT,
        entity_ref TEXT NOT NULL,
        currency TEXT NOT NULL CHECK (char_length(currency) = 3),
        positive_delta_minor BIGINT NOT NULL CHECK (positive_delta_minor >= 0),
        parameter TEXT NOT NULL,
        previous_value JSONB NOT NULL,
        proposed_value JSONB NOT NULL,
        diff_hash TEXT NOT NULL CHECK (diff_hash ~ '^[a-f0-9]{64}$'),
        state TEXT NOT NULL DEFAULT 'ACTIVE' CHECK (state IN ('ACTIVE','SETTLED','RELEASED')),
        created_at TIMESTAMPTZ NOT NULL,
        resolved_at TIMESTAMPTZ,
        FOREIGN KEY (business_id, entity_ref) REFERENCES ad_entities(business_id, entity_ref),
        CHECK ((state = 'ACTIVE') = (resolved_at IS NULL))
    )""")
    op.execute("""CREATE INDEX ix_execution_reservations_active_account
        ON execution_reservations(platform_account_id) WHERE state = 'ACTIVE'""")
    op.execute("DROP INDEX ix_executions_claim")
    op.execute("""CREATE INDEX ix_executions_claim ON executions(outcome, scheduled_at)
        WHERE outcome IN ('CLAIMED','RUNNING','UNKNOWN')""")


def downgrade() -> None:
    # Never erase unresolved protection on a rolling rollback.
    op.execute("""DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM execution_reservations WHERE state = 'ACTIVE')
        OR EXISTS (SELECT 1 FROM executions WHERE outcome = 'UNKNOWN') THEN
            RAISE EXCEPTION 'Cannot downgrade with unresolved executions';
        END IF;
    END $$""")
    op.execute("DROP TABLE execution_reservations")
    op.execute("DROP INDEX ix_executions_claim")
    op.execute("""CREATE INDEX ix_executions_claim ON executions(outcome, scheduled_at)
        WHERE outcome IN ('CLAIMED','RUNNING')""")
    op.execute("ALTER TABLE executions DROP CONSTRAINT executions_outcome_check")
    op.execute("""ALTER TABLE executions ADD CONSTRAINT executions_outcome_check
        CHECK (outcome IN ('CLAIMED','RUNNING','SUCCEEDED','FAILED',
                          'SKIPPED_DRIFT','BLOCKED_GUARDRAIL','BLOCKED_BRAKE'))""")
    op.execute("ALTER TABLE executions DROP CONSTRAINT executions_terminal_is_finished_check")
    op.execute("""ALTER TABLE executions ADD CONSTRAINT executions_terminal_is_finished_check
        CHECK (outcome IN ('CLAIMED','RUNNING') OR finished_at IS NOT NULL)""")
