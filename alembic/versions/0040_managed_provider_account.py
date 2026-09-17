"""Map numeric managed claims to exact provider references, without backfill."""

from alembic import op

revision = "0040_managed_provider_account"
down_revision = "0039_managed_signed_context"
branch_labels = None
depends_on = None


def _guard(account_expression: str) -> str:
    # Only the account comparison changes; context remains immutable and both
    # approval and reservation must retain the original signed proposal binding.
    return f"""CREATE OR REPLACE FUNCTION ads_guard_managed_context() RETURNS trigger AS $$
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
                    OR {account_expression} IS DISTINCT FROM account_row.external_account_id THEN
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
    END $$ LANGUAGE plpgsql"""  # noqa: S608 - two constant migration expressions, never user input


def upgrade() -> None:
    op.execute("""CREATE FUNCTION ads_managed_provider_account(platform TEXT, remote TEXT)
        RETURNS TEXT AS $$ SELECT CASE
            WHEN remote ~ '^[0-9]{1,128}$' AND platform = 'google' THEN remote
            WHEN remote ~ '^[0-9]{1,128}$' AND platform = 'meta' THEN 'act_' || remote
            ELSE NULL END $$ LANGUAGE SQL IMMUTABLE STRICT""")
    op.execute(
        _guard(
            "ads_managed_provider_account(NEW.managed_binding->>'platform', "
            "NEW.managed_binding->>'external_account_id')"
        )
    )


def downgrade() -> None:
    # 0039 cannot validate native Meta account refs against numeric claims.
    # Lock before inspecting, so concurrent accepted Meta history cannot race DDL.
    op.execute("LOCK TABLE proposals, approvals, execution_reservations IN ACCESS EXCLUSIVE MODE")
    op.execute("""DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM proposals WHERE managed_binding->>'platform' = 'meta')
            OR EXISTS (SELECT 1 FROM approvals WHERE managed_binding->>'platform' = 'meta')
            OR EXISTS (SELECT 1 FROM execution_reservations
                       WHERE managed_binding->>'platform' = 'meta') THEN
            RAISE EXCEPTION 'managed_meta_history_requires_provider_mapping';
        END IF;
    END $$""")
    op.execute(_guard("NEW.managed_binding->>'external_account_id'"))
    op.execute("DROP FUNCTION ads_managed_provider_account(TEXT, TEXT)")
