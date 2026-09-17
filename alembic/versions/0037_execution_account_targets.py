"""Real account targets for creation, without synthetic campaign entities.

Historical refs, signed payloads and receipts are not rewritten. New typed FK
columns are a projection of the existing reference, not a new target registry.
"""

from alembic import op

# ruff: noqa: S608 -- migration identifiers come exclusively from _TABLES below.

revision = "0037_execution_account_targets"
down_revision = "0036_physical_metric_views"
branch_labels = None
depends_on = None

_TABLES = ("executions", "execution_reservations", "spend_ledger")


def upgrade() -> None:
    _proposal_guard(allow_revision=True)
    op.execute("""CREATE VIEW ads_execution_targets AS
        SELECT e.business_id, e.platform_account_id, e.entity_ref, e.level, e.external_id,
               e.platform, e.connection_id, e.entity_ref AS ad_entity_ref,
               NULL::uuid AS account_target_id
          FROM ad_entities e
        UNION ALL
        SELECT a.business_id, a.id,
               CASE WHEN a.connection_id IS NULL
                    THEN a.platform || ':account:' || a.external_account_id
                    ELSE a.account_ref END,
               'account', a.external_account_id, a.platform, a.connection_id, NULL::text, a.id
          FROM platform_accounts a
    """)
    for table in _TABLES:
        # Fixed migration identifiers, never supplied by clients.
        op.execute(
            f"ALTER TABLE {table} ADD COLUMN target_ad_entity_ref TEXT, "
            "ADD COLUMN target_account_id UUID"
        )
        if table == "executions":
            # Only the derived target column changes. Retain historical timestamps;
            # ALTER TABLE already holds the migration's exclusive transaction lock.
            op.execute("ALTER TABLE executions DISABLE TRIGGER executions_set_updated_at")
        op.execute(f"UPDATE {table} SET target_ad_entity_ref = entity_ref")
        if table == "executions":
            op.execute("ALTER TABLE executions ENABLE TRIGGER executions_set_updated_at")
        op.execute(f"""ALTER TABLE {table}
            ADD CONSTRAINT {table}_typed_entity_fk FOREIGN KEY (business_id, target_ad_entity_ref)
                REFERENCES ad_entities(business_id, entity_ref) ON DELETE RESTRICT,
            ADD CONSTRAINT {table}_typed_account_fk FOREIGN KEY (target_account_id)
                REFERENCES platform_accounts(id) ON DELETE RESTRICT,
            ADD CONSTRAINT {table}_one_target CHECK (
                (target_ad_entity_ref IS NULL) <> (target_account_id IS NULL))
        """)
        # Only parent constraints: partition descendants must not be dropped twice.
        op.execute(f"""DO $$ DECLARE c RECORD; BEGIN
            FOR c IN SELECT conname FROM pg_constraint
                WHERE conrelid = '{table}'::regclass AND confrelid = 'ad_entities'::regclass
                  AND contype = 'f' AND conparentid = 0
                  AND conname <> '{table}_typed_entity_fk'
            LOOP EXECUTE format('ALTER TABLE {table} DROP CONSTRAINT %I', c.conname); END LOOP;
        END $$""")
    op.execute("""CREATE FUNCTION validate_execution_target() RETURNS trigger AS $$
        DECLARE target RECORD; related RECORD;
        BEGIN
            IF TG_OP = 'UPDATE' AND (NEW.business_id, NEW.entity_ref)
                IS DISTINCT FROM (OLD.business_id, OLD.entity_ref) THEN
                RAISE EXCEPTION 'execution_target_immutable';
            END IF;
            SELECT * INTO target FROM ads_execution_targets
             WHERE entity_ref = NEW.entity_ref AND business_id = NEW.business_id;
            IF NOT FOUND THEN RAISE EXCEPTION 'execution_target_scope_missing'; END IF;
            NEW.target_ad_entity_ref := target.ad_entity_ref;
            NEW.target_account_id := target.account_target_id;
            IF TG_TABLE_NAME <> 'executions' THEN
                IF NEW.platform_account_id <> target.platform_account_id THEN
                    RAISE EXCEPTION 'execution_target_account_mismatch';
                END IF;
            END IF;
            IF target.account_target_id IS NOT NULL THEN
                IF TG_TABLE_NAME = 'execution_reservations' THEN
                    SELECT p.* INTO related FROM proposals p
                      JOIN executions e ON e.proposal_id = p.id
                     WHERE e.id = NEW.execution_id;
                ELSE
                    SELECT p.* INTO related FROM proposals p WHERE p.id = NEW.proposal_id;
                END IF;
                IF related.id IS NULL OR related.business_id <> NEW.business_id
                   OR related.entity_ref <> NEW.entity_ref
                   OR related.parameter NOT LIKE 'new_campaign:%' THEN
                    RAISE EXCEPTION 'execution_account_target_requires_creation';
                END IF;
            END IF;
            RETURN NEW;
        END $$ LANGUAGE plpgsql""")
    for table in _TABLES:
        op.execute(
            f"CREATE TRIGGER {table}_validate_target BEFORE INSERT OR UPDATE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION validate_execution_target()"
        )
    op.execute("ALTER TABLE executions ALTER COLUMN platform_state_hash_before DROP NOT NULL")
    op.execute("""ALTER TABLE executions ADD CONSTRAINT executions_prior_state_required
        CHECK (target_account_id IS NOT NULL OR platform_state_hash_before IS NOT NULL)""")


def downgrade() -> None:
    for table in _TABLES:
        op.execute(f"""DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM {table} WHERE target_account_id IS NOT NULL) THEN
                RAISE EXCEPTION 'Cannot downgrade with account-target execution history';
            END IF; END $$""")
    op.execute("ALTER TABLE executions DROP CONSTRAINT executions_prior_state_required")
    op.execute("ALTER TABLE executions ALTER COLUMN platform_state_hash_before SET NOT NULL")
    for table in _TABLES:
        op.execute(f"DROP TRIGGER {table}_validate_target ON {table}")
        constraint = (
            "execution_reservations_business_id_entity_ref_fkey"
            if table == "execution_reservations"
            else f"{table}_entity_fk"
        )
        on_delete = "NO ACTION" if table == "execution_reservations" else "RESTRICT"
        op.execute(f"""ALTER TABLE {table} ADD CONSTRAINT {constraint}
            FOREIGN KEY (business_id, entity_ref)
            REFERENCES ad_entities(business_id, entity_ref) ON DELETE {on_delete}""")
        op.execute(
            f"ALTER TABLE {table} DROP COLUMN target_ad_entity_ref, DROP COLUMN target_account_id"
        )
    op.execute("DROP FUNCTION validate_execution_target()")
    op.execute("DROP VIEW ads_execution_targets")
    _proposal_guard(allow_revision=False)


def _proposal_guard(*, allow_revision: bool) -> None:
    revision_gate = (
        """IF OLD.state IN ('approved','scheduled') AND NEW.state='pending'
        AND NEW.proposed_value IS DISTINCT FROM OLD.proposed_value
        AND NEW.diff_hash <> OLD.diff_hash AND NEW.execution_scheduled_at IS NULL THEN
        IF EXISTS (SELECT 1 FROM executions e
            LEFT JOIN execution_reservations r ON r.execution_id=e.id
            WHERE e.proposal_id=OLD.id AND (e.outcome IN ('RUNNING','UNKNOWN') OR r.state='ACTIVE'))
        THEN RAISE EXCEPTION 'proposal_revision_execution_unresolved'; END IF;
        RETURN NEW;
    END IF;"""
        if allow_revision
        else ""
    )
    op.execute(f"""CREATE OR REPLACE FUNCTION proposals_guard_diff_hash() RETURNS TRIGGER AS $$
        DECLARE allowed TEXT[];
        BEGIN
            IF NEW.proposed_value IS DISTINCT FROM OLD.proposed_value
               AND NEW.diff_hash = OLD.diff_hash THEN
                RAISE EXCEPTION 'proposals: cambiar proposed_value obliga a rotar el diff_hash';
            END IF;
            {revision_gate}
            IF NEW.state <> OLD.state THEN
                allowed := CASE OLD.state
                    WHEN 'pending' THEN
                        ARRAY['postponed','approved','rejected','expired','invalidated']
                    WHEN 'postponed' THEN
                        ARRAY['pending','approved','rejected','expired','invalidated']
                    WHEN 'approved' THEN ARRAY['scheduled','executing','invalidated','expired']
                    WHEN 'scheduled' THEN ARRAY['executing','invalidated','rejected']
                    WHEN 'executing' THEN ARRAY['executed','failed']
                    ELSE ARRAY[]::TEXT[] END;
                IF NOT (NEW.state = ANY(allowed)) THEN
                    RAISE EXCEPTION 'proposals: transicion % -> % no permitida',
                        OLD.state, NEW.state;
                END IF;
            END IF;
            RETURN NEW;
        END; $$ LANGUAGE plpgsql""")
