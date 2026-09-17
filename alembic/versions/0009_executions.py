"""0009_executions: `executions` (un intento idempotente por propuesta y
diff) y `spend_ledger` (cambios aplicados + gasto reportado, base de los
topes diario y mensual).

data-model.md §ExecutionAttempt: "idempotency_key =
exec-<proposal_id>-<diff_hash[:12]> UNIQUE -- reintentar nunca duplica el
cambio; toda excepcion o timeout deja el intento en FALLIDA".

threat-model.md C-17: los topes se calculan "sobre el ledger de cambios
aplicados + gasto reportado por la plataforma, mas contador por entidad y
dia". Sin ese ledger, N cambios pequenos por debajo del salto maximo suman
por encima del tope (bypass por goteo).

Revision ID: 0009_executions
Revises: 0008_proposals
Create Date: 2026-09-09
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0009_executions"
down_revision: str | None = "0008_proposals"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

_UUID = "[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"


def upgrade() -> None:
    op.execute(f"""
        CREATE TABLE executions (
            id                          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            proposal_id                 UUID NOT NULL REFERENCES proposals (id)
                                          ON DELETE RESTRICT,
            authorization_id            UUID NOT NULL REFERENCES approvals (id)
                                          ON DELETE RESTRICT,
            business_id                 UUID NOT NULL REFERENCES businesses (id)
                                          ON DELETE RESTRICT,
            entity_ref                  TEXT NOT NULL,
            idempotency_key             TEXT NOT NULL
                                          CHECK (idempotency_key ~
                                                 '^exec-{_UUID}-[a-f0-9]{{12}}$'),
            previous_value              JSONB NOT NULL,
            applied_value               JSONB,
            platform_state_hash_before  TEXT NOT NULL
                                          CHECK (platform_state_hash_before ~
                                                 '^[a-f0-9]{{64}}$'),
            platform_state_hash_after   TEXT
                                          CHECK (platform_state_hash_after ~
                                                 '^[a-f0-9]{{64}}$'),
            attempt_count               SMALLINT NOT NULL DEFAULT 0
                                          CHECK (attempt_count >= 0),
            outcome                     TEXT NOT NULL DEFAULT 'CLAIMED' CHECK (outcome IN
                                          ('CLAIMED', 'RUNNING', 'SUCCEEDED', 'FAILED',
                                           'SKIPPED_DRIFT', 'BLOCKED_GUARDRAIL',
                                           'BLOCKED_BRAKE')),
            error_code                  TEXT,
            error_message               TEXT,
            scheduled_at                TIMESTAMPTZ NOT NULL DEFAULT now(),
            started_at                  TIMESTAMPTZ,
            finished_at                 TIMESTAMPTZ,
            undo_deadline               TIMESTAMPTZ,
            undone_at                   TIMESTAMPTZ,
            undo_reason                 TEXT,
            compensating_proposal_id    UUID REFERENCES proposals (id) ON DELETE SET NULL,
            created_at                  TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at                  TIMESTAMPTZ NOT NULL DEFAULT now(),

            -- Reintentar el mismo cambio no lo aplica dos veces (C-8).
            CONSTRAINT executions_idempotency_key_unique UNIQUE (idempotency_key),
            CONSTRAINT executions_entity_fk
                FOREIGN KEY (business_id, entity_ref)
                REFERENCES ad_entities (business_id, entity_ref) ON DELETE RESTRICT,
            CONSTRAINT executions_terminal_is_finished_check CHECK (
                outcome IN ('CLAIMED', 'RUNNING') OR finished_at IS NOT NULL
            ),
            -- Un exito sin estado remoto posterior no es un exito verificado.
            CONSTRAINT executions_success_is_verified_check CHECK (
                outcome <> 'SUCCEEDED'
                OR (applied_value IS NOT NULL AND platform_state_hash_after IS NOT NULL)
            ),
            CONSTRAINT executions_failure_has_code_check CHECK (
                outcome NOT IN ('FAILED', 'SKIPPED_DRIFT', 'BLOCKED_GUARDRAIL', 'BLOCKED_BRAKE')
                OR error_code IS NOT NULL
            ),
            CONSTRAINT executions_undo_check CHECK (
                undone_at IS NULL OR (undo_reason IS NOT NULL AND outcome = 'SUCCEEDED')
            )
        )
    """)
    op.execute("""
        CREATE TRIGGER executions_set_updated_at
        BEFORE UPDATE ON executions FOR EACH ROW EXECUTE FUNCTION set_updated_at()
    """)
    # ClaimNextExecutable: FOR UPDATE SKIP LOCKED sobre lo reclamable.
    op.execute("""
        CREATE INDEX ix_executions_claim ON executions (outcome, scheduled_at)
        WHERE outcome IN ('CLAIMED', 'RUNNING')
    """)
    # GET /executions?business_id&outcome&since.
    op.execute("""
        CREATE INDEX ix_executions_business_time ON executions (business_id, created_at DESC)
    """)
    op.execute("CREATE INDEX ix_executions_proposal ON executions (proposal_id)")
    op.execute("CREATE INDEX ix_executions_authorization ON executions (authorization_id)")

    # Unidades menores enteras: la suma del tope es exacta y no depende de
    # como redondee cada plataforma. `delta_minor` firmado: una bajada resta.
    op.execute("""
        CREATE TABLE spend_ledger (
            id                    BIGSERIAL PRIMARY KEY,
            business_id           UUID NOT NULL REFERENCES businesses (id) ON DELETE RESTRICT,
            platform_account_id   UUID NOT NULL REFERENCES platform_accounts (id)
                                    ON DELETE RESTRICT,
            entity_ref            TEXT NOT NULL,
            ledger_date           DATE NOT NULL,
            currency              TEXT NOT NULL CHECK (char_length(currency) = 3),
            kind                  TEXT NOT NULL
                                    CHECK (kind IN ('applied_change', 'platform_spend')),
            delta_minor           BIGINT NOT NULL,
            previous_value_minor  BIGINT CHECK (previous_value_minor >= 0),
            new_value_minor       BIGINT CHECK (new_value_minor >= 0),
            reported_spend_minor  BIGINT CHECK (reported_spend_minor >= 0),
            execution_id          UUID REFERENCES executions (id) ON DELETE RESTRICT,
            proposal_id           UUID REFERENCES proposals (id) ON DELETE SET NULL,
            recorded_at           TIMESTAMPTZ NOT NULL DEFAULT now(),

            CONSTRAINT spend_ledger_entity_fk
                FOREIGN KEY (business_id, entity_ref)
                REFERENCES ad_entities (business_id, entity_ref) ON DELETE RESTRICT,
            CONSTRAINT spend_ledger_applied_change_check CHECK (
                kind <> 'applied_change'
                OR (execution_id IS NOT NULL AND previous_value_minor IS NOT NULL
                    AND new_value_minor IS NOT NULL
                    AND delta_minor = new_value_minor - previous_value_minor)
            ),
            CONSTRAINT spend_ledger_platform_spend_check CHECK (
                kind <> 'platform_spend'
                OR (execution_id IS NULL AND reported_spend_minor IS NOT NULL
                    AND delta_minor = reported_spend_minor)
            )
        )
    """)
    op.execute("""
        COMMENT ON TABLE spend_ledger IS
        'Base de los topes diario y mensual (C-17): cambios aplicados con delta '
        'firmado en unidades menores mas instantaneas de gasto reportado.'
    """)
    # Un apunte por ejecucion: reejecutar el registro no infla el tope.
    op.execute("""
        CREATE UNIQUE INDEX ix_spend_ledger_execution
        ON spend_ledger (execution_id) WHERE execution_id IS NOT NULL
    """)
    # Una instantanea de gasto por entidad y dia, actualizable por UPSERT.
    op.execute("""
        CREATE UNIQUE INDEX ix_spend_ledger_daily_spend
        ON spend_ledger (entity_ref, ledger_date) WHERE kind = 'platform_spend'
    """)
    # Tope diario y mensual: SUM(delta_minor) por cuenta sobre un rango de
    # dias. El INCLUDE lo resuelve sin tocar la tabla.
    op.execute("""
        CREATE INDEX ix_spend_ledger_account_date
        ON spend_ledger (platform_account_id, ledger_date, kind) INCLUDE (delta_minor)
    """)
    # Cambios maximos por entidad y dia.
    op.execute("""
        CREATE INDEX ix_spend_ledger_entity_changes
        ON spend_ledger (entity_ref, ledger_date) WHERE kind = 'applied_change'
    """)


def downgrade() -> None:
    op.execute("DROP TABLE spend_ledger")
    op.execute("DROP TABLE executions")
