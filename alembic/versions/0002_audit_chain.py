"""0002_audit_chain: decision_log solo-anexable con cadena de hash por
trigger, denegacion de UPDATE/DELETE/TRUNCATE, y revocacion de privilegios
al rol de aplicacion.

data-model.md §DecisionLogEntry: "entry_hash = H(seq || prev_hash ||
canonical_payload) calculado por trigger BEFORE INSERT; triggers que
deniegan UPDATE/DELETE/TRUNCATE; el rol de aplicacion solo tiene INSERT y
SELECT". threat-model.md C-19.

Revision ID: 0002_audit_chain
Revises: 0001_bootstrap
Create Date: 2026-09-09
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0002_audit_chain"
down_revision: str | None = "0001_bootstrap"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE decision_log (
            seq          BIGSERIAL PRIMARY KEY,
            business_id  UUID NOT NULL,
            event_type   TEXT NOT NULL,
            entity_ref   TEXT,
            actor_kind   TEXT NOT NULL
                            CHECK (actor_kind IN ('owner', 'rule_engine', 'agent', 'system')),
            actor_id     TEXT,
            proposal_id  UUID,
            payload      JSONB NOT NULL DEFAULT '{}'::jsonb,
            prev_hash    TEXT NOT NULL,
            entry_hash   TEXT NOT NULL UNIQUE,
            occurred_at  TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    # Sin FK dura hacia businesses/proposals a proposito (data-model.md
    # §Relationships: "DecisionLogEntry (solo-anexable, sin FK dura)"): el
    # log sobrevive aunque el agregado de origen cambie o se reindexe.
    op.execute(
        "CREATE INDEX ix_decision_log_business_time ON decision_log (business_id, occurred_at DESC)"
    )
    op.execute("CREATE INDEX ix_decision_log_payload_gin ON decision_log USING GIN (payload)")

    op.execute("""
        CREATE FUNCTION decision_log_chain() RETURNS TRIGGER AS $$
        DECLARE
            previous_hash TEXT;
            canonical     TEXT;
        BEGIN
            SELECT entry_hash INTO previous_hash FROM decision_log ORDER BY seq DESC LIMIT 1;
            IF previous_hash IS NULL THEN
                previous_hash := '';
            END IF;
            NEW.prev_hash := previous_hash;
            canonical := NEW.seq::text || '|' || previous_hash || '|' || NEW.payload::text;
            NEW.entry_hash := encode(digest(canonical, 'sha256'), 'hex');
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
    """)
    op.execute("""
        CREATE TRIGGER decision_log_chain_hash
        BEFORE INSERT ON decision_log
        FOR EACH ROW EXECUTE FUNCTION decision_log_chain()
    """)

    op.execute("""
        CREATE FUNCTION decision_log_immutable() RETURNS TRIGGER AS $$
        BEGIN
            RAISE EXCEPTION 'decision_log es append-only: % no permitido', TG_OP;
        END;
        $$ LANGUAGE plpgsql
    """)
    op.execute("""
        CREATE TRIGGER decision_log_no_update
        BEFORE UPDATE ON decision_log
        FOR EACH ROW EXECUTE FUNCTION decision_log_immutable()
    """)
    op.execute("""
        CREATE TRIGGER decision_log_no_delete
        BEFORE DELETE ON decision_log
        FOR EACH ROW EXECUTE FUNCTION decision_log_immutable()
    """)
    op.execute("""
        CREATE TRIGGER decision_log_no_truncate
        BEFORE TRUNCATE ON decision_log
        FOR EACH STATEMENT EXECUTE FUNCTION decision_log_immutable()
    """)

    # Defensa en profundidad: el rol de aplicacion pierde UPDATE/DELETE/
    # TRUNCATE ademas de los triggers. Guardado tras EXISTS porque el rol
    # `ads` puede no existir aun en un contenedor de test recien creado.
    # Literal fijo, sin interpolacion Python: el rol es una constante de
    # esta migracion, nunca entrada externa (ruff S608 exige SQL estatico).
    op.execute("""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ads') THEN
                REVOKE UPDATE, DELETE, TRUNCATE ON decision_log FROM ads;
            END IF;
        END
        $$
    """)


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS decision_log_no_truncate ON decision_log")
    op.execute("DROP TRIGGER IF EXISTS decision_log_no_delete ON decision_log")
    op.execute("DROP TRIGGER IF EXISTS decision_log_no_update ON decision_log")
    op.execute("DROP TRIGGER IF EXISTS decision_log_chain_hash ON decision_log")
    op.execute("DROP FUNCTION IF EXISTS decision_log_immutable()")
    op.execute("DROP FUNCTION IF EXISTS decision_log_chain()")
    op.execute("DROP TABLE decision_log")
