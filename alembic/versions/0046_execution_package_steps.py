"""Lets `executions` carry a package-step attempt (BL-4, AL-3).

Three additive changes, none touching existing rows:

1. `executions.idempotency_key`'s CHECK only accepted the `exec-<uuid>-
   <12 hex>` shape (0009). `build_package_step_idempotency_key` (BL-4)
   produces `pkg-<publication ULID>-<2-digit step index>` -- a second,
   equally strict shape, never the two confused for each other.
2. `executions.package_publication_id` (TEXT NULL, FK to
   `campaign_package_publications.id`, itself a ULID): `claim_next`'s
   generic cycle must
   never reclaim a package step (AL-3/J-T1) -- the column is the WHERE
   clause it filters on, not an `EXISTS` subquery in the claim hot path.
3. `executions.created_external_id` (TEXT NULL): the platform resource id
   a creation write confirms (`campaign_resource`/`child_resource`,
   `broker/platforms/campaign_creation.py`/`ad_child_creation.py`) was
   discarded at the `execution` boundary before this column existed --
   `WriteResult`/`ExecutionAttempt` only carried `applied_value` (the
   signed diff echoed back) and `confirmed_state_hash`. A package's
   `RunPackagePublication` needs the created id to resolve the next
   step's parent; useful for any creation write, not package-specific.
"""

from alembic import op

revision = "0046_execution_package_steps"
down_revision = "0045_package_step_auth_chain"
branch_labels = None
depends_on = None

_UUID = "[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
_ULID = "[0-9A-HJKMNP-TV-Z]{26}"


def upgrade() -> None:
    op.execute("ALTER TABLE executions DROP CONSTRAINT executions_idempotency_key_check")
    op.execute(f"""
        ALTER TABLE executions
        ADD CONSTRAINT executions_idempotency_key_check
            CHECK (idempotency_key ~ '^exec-{_UUID}-[a-f0-9]{{12}}$'
                   OR idempotency_key ~ '^pkg-{_ULID}-[0-9]{{2}}$')
    """)

    op.execute("""
        ALTER TABLE executions
        ADD COLUMN package_publication_id TEXT
            REFERENCES campaign_package_publications (id) ON DELETE RESTRICT,
        ADD COLUMN created_external_id TEXT
    """)
    op.execute("""
        CREATE INDEX ix_executions_package_publication
        ON executions (package_publication_id)
        WHERE package_publication_id IS NOT NULL
    """)
    # AL-3/J-T1: el WHERE de `claim_next` (ciclo generico) excluye pasos de
    # paquete por esta columna, no por prefijo de `idempotency_key` -- un
    # indice parcial explicito, no una subconsulta correlacionada en la ruta
    # caliente de reclamacion.
    op.execute("""
        CREATE INDEX ix_executions_claim_excludes_package_steps
        ON executions (scheduled_at)
        WHERE outcome IN ('CLAIMED', 'RUNNING', 'UNKNOWN') AND package_publication_id IS NULL
    """)


def downgrade() -> None:
    op.execute("""DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM executions WHERE package_publication_id IS NOT NULL) THEN
            RAISE EXCEPTION 'executions_package_step_history_present';
        END IF;
    END $$""")

    op.execute("DROP INDEX ix_executions_claim_excludes_package_steps")
    op.execute("DROP INDEX ix_executions_package_publication")
    op.execute("ALTER TABLE executions DROP COLUMN created_external_id")
    op.execute("ALTER TABLE executions DROP COLUMN package_publication_id")

    op.execute("ALTER TABLE executions DROP CONSTRAINT executions_idempotency_key_check")
    op.execute(f"""
        ALTER TABLE executions
        ADD CONSTRAINT executions_idempotency_key_check
            CHECK (idempotency_key ~ '^exec-{_UUID}-[a-f0-9]{{12}}$')
    """)
