"""0008_proposals: `proposals` (maquina de 9 estados + `scheduled` para la
gracia de deshacer) y `approvals` solo-anexable.

data-model.md §PropuestaDeAccion: "(2) editar valor_propuesto cambia el
diff_hash e invalida toda autorizacion previa; (4) Clasificacion.IMPORTANTE
exige autorizacion human_approval; (5) una sola propuesta abierta por
(entity_ref, parametro): la nueva actualiza la existente (FR-20)".

Las tres van al esquema: UNIQUE parcial sobre los estados vivos, trigger que
prohibe cambiar el valor propuesto sin rotar el `diff_hash`, y trigger que
rechaza una autorizacion de regla sobre una propuesta important o critical.

Identificadores tecnicos en ingles (NFR-12); el castellano vive en la
interfaz. `convocatoria` se queda: es termino del dominio, no jerga tecnica.

Revision ID: 0008_proposals
Revises: 0007_rules_guardrails
Create Date: 2026-09-09
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0008_proposals"
down_revision: str | None = "0007_rules_guardrails"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

# Estados vivos: mientras la propuesta este en uno de ellos, una equivalente
# no crea fila nueva, actualiza esta (FR-20).
_LIVE_STATES = "('pending', 'postponed', 'approved', 'scheduled')"


def upgrade() -> None:
    op.execute("""
        CREATE TABLE proposals (
            id                        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            business_id               UUID NOT NULL REFERENCES businesses (id)
                                        ON DELETE RESTRICT,
            entity_ref                TEXT NOT NULL,
            parameter                 TEXT NOT NULL
                                        CHECK (char_length(parameter) BETWEEN 1 AND 64),
            current_value             JSONB NOT NULL,
            proposed_value            JSONB NOT NULL,
            diff_hash                 TEXT NOT NULL CHECK (diff_hash ~ '^[a-f0-9]{64}$'),
            classification            TEXT NOT NULL CHECK (classification IN
                                        ('routine', 'important', 'critical')),
            cause_key                 TEXT NOT NULL,
            cause                     TEXT NOT NULL
                                        CHECK (char_length(cause) BETWEEN 1 AND 280),
            evidence                  JSONB NOT NULL DEFAULT '{}'::jsonb,
            estimated_impact_amount   NUMERIC(14, 2) NOT NULL,
            estimated_impact_currency TEXT NOT NULL
                                        CHECK (char_length(estimated_impact_currency) = 3),
            urgency                   TEXT NOT NULL CHECK (urgency IN
                                        ('critical', 'recommended', 'minor')),
            convocatoria_id           UUID REFERENCES convocatorias (id) ON DELETE SET NULL,
            signal_id                 UUID REFERENCES signals (id) ON DELETE SET NULL,
            rule_id                   UUID REFERENCES rules (id) ON DELETE SET NULL,
            state                     TEXT NOT NULL DEFAULT 'pending' CHECK (state IN
                                        ('pending', 'postponed', 'approved', 'scheduled',
                                         'executing', 'executed', 'rejected', 'expired',
                                         'invalidated', 'failed')),
            postponed_until           TIMESTAMPTZ,
            expires_at                TIMESTAMPTZ NOT NULL,
            execution_scheduled_at    TIMESTAMPTZ,
            resolved_at               TIMESTAMPTZ,
            created_at                TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at                TIMESTAMPTZ NOT NULL DEFAULT now(),

            CONSTRAINT proposals_entity_fk
                FOREIGN KEY (business_id, entity_ref)
                REFERENCES ad_entities (business_id, entity_ref) ON DELETE RESTRICT,
            -- La gracia solo existe si hay hora de fin de gracia.
            CONSTRAINT proposals_scheduled_needs_time_check
                CHECK (state <> 'scheduled' OR execution_scheduled_at IS NOT NULL),
            CONSTRAINT proposals_postponed_needs_time_check
                CHECK (state <> 'postponed' OR postponed_until IS NOT NULL),
            CONSTRAINT proposals_diff_changes_value_check
                CHECK (current_value IS DISTINCT FROM proposed_value)
        )
    """)
    op.execute("""
        COMMENT ON COLUMN proposals.state IS
        'pending|postponed|approved|scheduled|executing|executed|rejected|expired|'
        'invalidated|failed. `scheduled` = aprobada esperando a que venza la gracia.'
    """)
    op.execute("""
        CREATE TRIGGER proposals_set_updated_at
        BEFORE UPDATE ON proposals FOR EACH ROW EXECUTE FUNCTION set_updated_at()
    """)

    # FR-20: una sola propuesta abierta por (entity_ref, parameter).
    op.execute(f"""
        CREATE UNIQUE INDEX ix_proposals_open_per_parameter
        ON proposals (entity_ref, parameter)
        WHERE state IN {_LIVE_STATES}
    """)
    # Cola del panel y caducador del MaintenanceCycle.
    op.execute(f"""
        CREATE INDEX ix_proposals_live_queue
        ON proposals (business_id, state, expires_at)
        WHERE state IN {_LIVE_STATES}
    """)
    # Agrupacion por causa y aprobacion por lote (FR-17).
    op.execute("CREATE INDEX ix_proposals_cause_key ON proposals (business_id, cause_key)")
    # Lente de convocatoria (FR-19).
    op.execute("""
        CREATE INDEX ix_proposals_convocatoria ON proposals (convocatoria_id)
        WHERE convocatoria_id IS NOT NULL
    """)
    # ExecutionCycle: que hay que programar cuando venza la gracia.
    op.execute("""
        CREATE INDEX ix_proposals_scheduled ON proposals (execution_scheduled_at)
        WHERE state = 'scheduled'
    """)

    # INV-1 en la base: si cambia el valor propuesto, el diff_hash tiene que
    # rotar. Sin esto, una autorizacion vieja seguiria valiendo para un
    # cambio distinto.
    op.execute("""
        CREATE FUNCTION proposals_guard_diff_hash() RETURNS TRIGGER AS $$
        DECLARE
            allowed TEXT[];
        BEGIN
            IF NEW.proposed_value IS DISTINCT FROM OLD.proposed_value
               AND NEW.diff_hash = OLD.diff_hash THEN
                RAISE EXCEPTION
                    'proposals: cambiar proposed_value obliga a rotar el diff_hash';
            END IF;

            IF NEW.state <> OLD.state THEN
                allowed := CASE OLD.state
                    WHEN 'pending'   THEN
                        ARRAY['postponed', 'approved', 'rejected', 'expired', 'invalidated']
                    WHEN 'postponed' THEN
                        ARRAY['pending', 'approved', 'rejected', 'expired', 'invalidated']
                    WHEN 'approved'  THEN
                        ARRAY['scheduled', 'executing', 'invalidated', 'expired']
                    WHEN 'scheduled' THEN
                        ARRAY['executing', 'invalidated', 'rejected']
                    WHEN 'executing' THEN ARRAY['executed', 'failed']
                    ELSE ARRAY[]::TEXT[]
                END;
                IF NOT (NEW.state = ANY (allowed)) THEN
                    RAISE EXCEPTION 'proposals: transicion % -> % no permitida',
                        OLD.state, NEW.state;
                END IF;
            END IF;

            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
    """)
    op.execute("""
        CREATE TRIGGER proposals_guard_diff_hash
        BEFORE UPDATE ON proposals
        FOR EACH ROW EXECUTE FUNCTION proposals_guard_diff_hash()
    """)

    # Authorization (data-model.md §Authorization): solo-anexable, ligada a
    # proposal_id + diff_hash + expires_at + kind. Revocar = anexar.
    op.execute("""
        CREATE TABLE approvals (
            id                     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            proposal_id            UUID NOT NULL REFERENCES proposals (id) ON DELETE RESTRICT,
            kind                   TEXT NOT NULL
                                     CHECK (kind IN ('human_approval', 'rule_authorization')),
            decision               TEXT NOT NULL
                                     CHECK (decision IN ('approved', 'rejected', 'revoked')),
            diff_hash              TEXT NOT NULL CHECK (diff_hash ~ '^[a-f0-9]{64}$'),
            guardrail_verdict_hash TEXT NOT NULL
                                     CHECK (guardrail_verdict_hash ~ '^[a-f0-9]{64}$'),
            issued_by              TEXT NOT NULL
                                     CHECK (char_length(issued_by) BETWEEN 1 AND 128),
            rule_id                UUID REFERENCES rules (id) ON DELETE RESTRICT,
            channel                TEXT NOT NULL
                                     CHECK (channel IN ('panel', 'telegram', 'rule_engine')),
            signature              TEXT NOT NULL
                                     CHECK (char_length(signature) BETWEEN 16 AND 256),
            comment                TEXT,
            decided_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
            expires_at             TIMESTAMPTZ NOT NULL,

            CONSTRAINT approvals_expiry_check CHECK (expires_at > decided_at),
            CONSTRAINT approvals_rule_kind_check CHECK (
                (kind = 'rule_authorization' AND rule_id IS NOT NULL)
                OR (kind = 'human_approval' AND rule_id IS NULL)
            )
        )
    """)
    # data-model.md §Indices: UNIQUE (proposal_id, diff_hash, kind) sobre
    # decisiones vivas. Aprobar dos veces el mismo diff no crea dos
    # autorizaciones; reaprobar tras revocar exige rotar el diff.
    op.execute("""
        CREATE UNIQUE INDEX ix_approvals_live_decision
        ON approvals (proposal_id, diff_hash, kind)
        WHERE decision = 'approved'
    """)
    op.execute("""
        CREATE INDEX ix_approvals_proposal_time ON approvals (proposal_id, decided_at DESC)
    """)

    # FR-12 / invariante 4: lo important y lo critical lo firma una persona.
    op.execute("""
        CREATE FUNCTION approvals_kind_matches_classification() RETURNS TRIGGER AS $$
        DECLARE
            proposal_classification TEXT;
        BEGIN
            SELECT classification INTO proposal_classification
              FROM proposals WHERE id = NEW.proposal_id;
            IF proposal_classification IN ('important', 'critical')
               AND NEW.kind <> 'human_approval'
               AND NEW.decision = 'approved' THEN
                RAISE EXCEPTION
                    'approvals: una propuesta % exige human_approval', proposal_classification;
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
    """)
    op.execute("""
        CREATE TRIGGER approvals_kind_matches_classification
        BEFORE INSERT ON approvals
        FOR EACH ROW EXECUTE FUNCTION approvals_kind_matches_classification()
    """)

    op.execute("""
        CREATE FUNCTION approvals_immutable() RETURNS TRIGGER AS $$
        BEGIN
            RAISE EXCEPTION 'approvals es append-only: % no permitido (revocar = anexar)', TG_OP;
        END;
        $$ LANGUAGE plpgsql
    """)
    op.execute("""
        CREATE TRIGGER approvals_no_update
        BEFORE UPDATE ON approvals FOR EACH ROW EXECUTE FUNCTION approvals_immutable()
    """)
    op.execute("""
        CREATE TRIGGER approvals_no_delete
        BEFORE DELETE ON approvals FOR EACH ROW EXECUTE FUNCTION approvals_immutable()
    """)
    op.execute("""
        CREATE TRIGGER approvals_no_truncate
        BEFORE TRUNCATE ON approvals FOR EACH STATEMENT EXECUTE FUNCTION approvals_immutable()
    """)


def downgrade() -> None:
    op.execute("DROP TABLE approvals")
    op.execute("DROP FUNCTION IF EXISTS approvals_immutable()")
    op.execute("DROP FUNCTION IF EXISTS approvals_kind_matches_classification()")
    op.execute("DROP TABLE proposals")
    op.execute("DROP FUNCTION IF EXISTS proposals_guard_diff_hash()")
