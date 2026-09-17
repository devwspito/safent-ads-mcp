"""Add signed Enterprise context without rewriting any historical payload."""

from alembic import op

revision = "0039_managed_signed_context"
down_revision = "0038_owner_action_confirmations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for table in ("proposals", "approvals", "execution_reservations"):
        op.execute(
            f"ALTER TABLE {table} ADD COLUMN managed_binding JSONB "
            "CHECK (managed_binding IS NULL OR jsonb_typeof(managed_binding) = 'object')"
        )
    op.execute("""CREATE FUNCTION ads_guard_managed_context() RETURNS trigger AS $$
    DECLARE expected JSONB; account_row RECORD;
    BEGIN
        IF TG_OP = 'UPDATE' AND NEW.managed_binding IS DISTINCT FROM OLD.managed_binding THEN
            RAISE EXCEPTION 'managed_binding_immutable';
        END IF;
        IF TG_TABLE_NAME = 'proposals' THEN
            IF NEW.managed_binding IS NOT NULL THEN
                SELECT a.* INTO account_row FROM ads_execution_targets t
                    JOIN platform_accounts a ON a.id = t.platform_account_id
                    WHERE t.entity_ref = NEW.entity_ref AND t.business_id = NEW.business_id;
                IF NOT FOUND OR NEW.managed_binding->>'business_id'
                        IS DISTINCT FROM NEW.business_id::text
                    OR NEW.managed_binding->>'connection_id'
                        IS DISTINCT FROM account_row.connection_id::text
                    OR NEW.managed_binding->>'platform' IS DISTINCT FROM account_row.platform::text
                    OR NEW.managed_binding->>'external_account_id'
                        IS DISTINCT FROM account_row.external_account_id THEN
                    RAISE EXCEPTION 'managed_binding_account_mismatch';
                END IF;
            END IF;
        ELSIF TG_TABLE_NAME = 'approvals' THEN
            SELECT p.managed_binding INTO expected FROM proposals p WHERE p.id = NEW.proposal_id;
            IF NEW.managed_binding IS DISTINCT FROM expected THEN
                RAISE EXCEPTION 'managed_binding_approval_mismatch';
            END IF;
            IF NEW.managed_binding IS NOT NULL AND NEW.kind <> 'human_approval' THEN
                RAISE EXCEPTION 'managed_binding_human_required';
            END IF;
        ELSE
            SELECT a.managed_binding INTO expected FROM executions e
                JOIN approvals a ON a.id = e.authorization_id WHERE e.id = NEW.execution_id;
            IF NEW.managed_binding IS DISTINCT FROM expected THEN
                RAISE EXCEPTION 'managed_binding_reservation_mismatch';
            END IF;
        END IF;
        RETURN NEW;
    END $$ LANGUAGE plpgsql""")
    for table in ("proposals", "approvals", "execution_reservations"):
        op.execute(
            f"CREATE TRIGGER {table}_managed_context BEFORE INSERT OR UPDATE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION ads_guard_managed_context()"
        )


def downgrade() -> None:
    # Acquire the same exclusive DDL locks BEFORE inspecting rows. Otherwise a
    # concurrent managed INSERT can commit after EXISTS and lose signed context.
    op.execute("LOCK TABLE proposals, approvals, execution_reservations IN ACCESS EXCLUSIVE MODE")
    op.execute("""DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM proposals WHERE managed_binding IS NOT NULL)
            OR EXISTS (SELECT 1 FROM approvals WHERE managed_binding IS NOT NULL)
            OR EXISTS (SELECT 1 FROM execution_reservations WHERE managed_binding IS NOT NULL) THEN
            RAISE EXCEPTION 'managed_context_history_requires_current_schema';
        END IF;
    END $$""")
    for table in ("proposals", "approvals", "execution_reservations"):
        op.execute(f"DROP TRIGGER {table}_managed_context ON {table}")
        op.execute(f"ALTER TABLE {table} DROP COLUMN managed_binding")
    op.execute("DROP FUNCTION ads_guard_managed_context()")
