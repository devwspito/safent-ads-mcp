"""0003_ad_entities: jerarquia publicitaria unica (campaign -> ad_set ->
ad -> creative) con self-FK, CHECK de nivel padre y trigger de coherencia.

data-model.md §AdEntity: "(platform, level, external_id) unico; parent_id
apunta exactamente al nivel inmediatamente superior (CHECK + trigger);
platform_state_hash [...] la entidad queda drifted [...]; una entidad
is_controllable = false solo admite lectura y presupuesto".

El par (level, parent_level) es lo que valida el CHECK: un CHECK no puede
mirar otra fila, asi que el trigger BEFORE copia el nivel real del padre a
`parent_level` y el CHECK decide si la pareja es legal. El trigger ademas
congela `level` (si el padre pudiera cambiar de nivel, `parent_level` de
los hijos quedaria mintiendo).

Revision ID: 0003_ad_entities
Revises: 0002_audit_chain
Create Date: 2026-09-09
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0003_ad_entities"
down_revision: str | None = "0002_audit_chain"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    # Funcion compartida de `updated_at`, definida aqui porque esta es la
    # primera revision con tablas mutables. Las revisiones posteriores la
    # reutilizan (el downgrade va en orden inverso, nunca la deja huerfana).
    op.execute("""
        CREATE FUNCTION set_updated_at() RETURNS TRIGGER AS $$
        BEGIN
            NEW.updated_at := now();
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
    """)

    # Objetivo de las FK compuestas: una entidad no puede heredar un
    # business_id ni un platform distintos de los de su cuenta. Sin esto la
    # desnormalizacion de `business_id` seria una promesa de la aplicacion.
    op.execute("""
        ALTER TABLE platform_accounts
            ADD CONSTRAINT platform_accounts_id_business_platform_unique
            UNIQUE (id, business_id, platform)
    """)

    op.execute("""
        CREATE TABLE ad_entities (
            id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            business_id          UUID NOT NULL,
            platform_account_id  UUID NOT NULL,
            platform             TEXT NOT NULL CHECK (platform IN ('google', 'meta')),
            level                TEXT NOT NULL
                                    CHECK (level IN ('campaign', 'ad_set', 'ad', 'creative')),
            external_id          TEXT NOT NULL CHECK (char_length(external_id) BETWEEN 1 AND 128),
            entity_ref           TEXT GENERATED ALWAYS AS
                                    (platform || ':' || level || ':' || external_id) STORED,
            parent_id            UUID REFERENCES ad_entities (id) ON DELETE RESTRICT,
            parent_level         TEXT,
            name                 TEXT NOT NULL,
            status               TEXT NOT NULL CHECK (status IN
                                    ('ACTIVE', 'PAUSED', 'REMOVED', 'DRIFTED', 'LEARNING')),
            budget_amount        NUMERIC(14, 2) CHECK (budget_amount >= 0),
            budget_currency      TEXT CHECK (char_length(budget_currency) = 3),
            budget_kind          TEXT CHECK (budget_kind IN ('daily', 'lifetime', 'shared')),
            shared_budget_ref    TEXT,
            bid_strategy         TEXT,
            bid_target_amount    NUMERIC(14, 4) CHECK (bid_target_amount >= 0),
            learning_state       TEXT NOT NULL DEFAULT 'UNKNOWN' CHECK (learning_state IN
                                    ('LEARNING', 'SUCCESS', 'FAIL', 'NOT_APPLICABLE', 'UNKNOWN')),
            platform_state_hash  TEXT NOT NULL CHECK (platform_state_hash ~ '^[a-f0-9]{64}$'),
            is_controllable      BOOLEAN NOT NULL DEFAULT true,
            last_synced_at       TIMESTAMPTZ,
            created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at           TIMESTAMPTZ NOT NULL DEFAULT now(),

            CONSTRAINT ad_entities_platform_level_external_unique
                UNIQUE (platform, level, external_id),
            -- Blanco de las FK de metrics/signals/crm: `entity_ref` es EL
            -- identificador que cruza contextos (data-model.md §EntityRef).
            CONSTRAINT ad_entities_entity_ref_unique UNIQUE (entity_ref),
            -- Blanco de las FK compuestas con business_id: impide que una
            -- senal/metrica de un negocio apunte a la entidad de otro (C-27).
            CONSTRAINT ad_entities_business_entity_ref_unique UNIQUE (business_id, entity_ref),

            CONSTRAINT ad_entities_account_fk
                FOREIGN KEY (platform_account_id, business_id, platform)
                REFERENCES platform_accounts (id, business_id, platform) ON DELETE RESTRICT,

            -- El nivel del padre debe ser el inmediatamente superior.
            -- `parent_level` lo escribe el trigger, nunca la aplicacion.
            CONSTRAINT ad_entities_parent_level_check CHECK (
                (level = 'campaign' AND parent_id IS NULL AND parent_level IS NULL)
                OR (level = 'ad_set'   AND parent_id IS NOT NULL AND parent_level = 'campaign')
                OR (level = 'ad'       AND parent_id IS NOT NULL AND parent_level = 'ad_set')
                OR (level = 'creative' AND parent_id IS NOT NULL AND parent_level = 'ad')
            ),
            CONSTRAINT ad_entities_budget_triplet_check
                CHECK (num_nulls(budget_amount, budget_currency, budget_kind) IN (0, 3)),
            CONSTRAINT ad_entities_shared_budget_check
                CHECK (shared_budget_ref IS NULL OR budget_kind = 'shared')
        )
    """)

    op.execute("""
        COMMENT ON COLUMN ad_entities.parent_level IS
        'Nivel real del padre, copiado por trigger. No lo escribe la aplicacion.'
    """)
    op.execute("""
        COMMENT ON COLUMN ad_entities.is_controllable IS
        'false = Advantage+/PMax sin palanca: solo lectura y presupuesto.'
    """)

    op.execute("""
        CREATE FUNCTION ad_entities_parent_coherence() RETURNS TRIGGER AS $$
        DECLARE
            parent_row RECORD;
        BEGIN
            IF TG_OP = 'UPDATE' AND NEW.level <> OLD.level THEN
                RAISE EXCEPTION
                    'ad_entities.level es inmutable: % -> % romperia parent_level de los hijos',
                    OLD.level, NEW.level;
            END IF;

            IF NEW.parent_id IS NULL THEN
                NEW.parent_level := NULL;
                RETURN NEW;
            END IF;

            IF NEW.parent_id = NEW.id THEN
                RAISE EXCEPTION 'ad_entities: una entidad no puede ser su propio padre';
            END IF;

            SELECT level, platform, platform_account_id
              INTO parent_row
              FROM ad_entities
             WHERE id = NEW.parent_id;

            IF NOT FOUND THEN
                RAISE EXCEPTION 'ad_entities: el padre % no existe', NEW.parent_id;
            END IF;

            IF parent_row.platform <> NEW.platform
               OR parent_row.platform_account_id <> NEW.platform_account_id THEN
                RAISE EXCEPTION
                    'ad_entities: el padre pertenece a otra cuenta o plataforma';
            END IF;

            NEW.parent_level := parent_row.level;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
    """)
    op.execute("""
        CREATE TRIGGER ad_entities_parent_coherence
        BEFORE INSERT OR UPDATE ON ad_entities
        FOR EACH ROW EXECUTE FUNCTION ad_entities_parent_coherence()
    """)
    op.execute("""
        CREATE TRIGGER ad_entities_set_updated_at
        BEFORE UPDATE ON ad_entities
        FOR EACH ROW EXECUTE FUNCTION set_updated_at()
    """)

    # GET /entities/{entity_ref}/children (rest-api.md) y la FK self-referente.
    op.execute("CREATE INDEX ix_ad_entities_parent ON ad_entities (parent_id)")
    # GET /portfolio: campanas activas de una cuenta.
    op.execute("""
        CREATE INDEX ix_ad_entities_account_level
        ON ad_entities (platform_account_id, level, status)
    """)
    # Reconciliacion de deriva: "que entidades no admiten escritura ahora".
    op.execute("""
        CREATE INDEX ix_ad_entities_drifted
        ON ad_entities (business_id, platform_account_id)
        WHERE status = 'DRIFTED'
    """)


def downgrade() -> None:
    op.execute("DROP TABLE ad_entities")
    op.execute("DROP FUNCTION IF EXISTS ad_entities_parent_coherence()")
    op.execute("""
        ALTER TABLE platform_accounts
            DROP CONSTRAINT platform_accounts_id_business_platform_unique
    """)
    op.execute("DROP FUNCTION IF EXISTS set_updated_at()")
