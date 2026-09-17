"""0005_catalog_crm: catalogo comercial (`courses`, `convocatorias`) y
atribucion de leads pseudonimizada (`lead_attributions`).

data-model.md §LeadAttribution: "prohibido el dato personal. Solo
HashedIdentity con sal por negocio, rotada; la sal nunca sale del broker de
secretos". threat-model.md C-31.

Ninguna columna guarda correo, telefono, nombre ni documento: el CHECK de
formato sha256 sobre `hashed_identity` hace que un valor crudo ni siquiera
entre. `identity_salt_ref` es el alias que resuelve el broker, nunca la sal.

`is_ventana_abierta` no existe como columna: se deriva de las fechas
(data-model.md §Convocatoria).

Revision ID: 0005_catalog_crm
Revises: 0004_metrics_facts
Create Date: 2026-09-09
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0005_catalog_crm"
down_revision: str | None = "0004_metrics_facts"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

_SHA256_HEX = "^[a-f0-9]{64}$"


def upgrade() -> None:
    op.execute("""
        CREATE TABLE courses (
            id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            business_id  UUID NOT NULL REFERENCES businesses (id) ON DELETE RESTRICT,
            code         TEXT NOT NULL CHECK (char_length(code) BETWEEN 1 AND 64),
            title        TEXT NOT NULL,
            cuerpo       TEXT,
            especialidad TEXT,
            price_amount NUMERIC(12, 2) CHECK (price_amount >= 0),
            price_currency TEXT CHECK (char_length(price_currency) = 3),
            is_active    BOOLEAN NOT NULL DEFAULT true,
            created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT courses_business_code_unique UNIQUE (business_id, code),
            CONSTRAINT courses_price_pair_check
                CHECK (num_nulls(price_amount, price_currency) IN (0, 2))
        )
    """)
    op.execute("""
        CREATE TRIGGER courses_set_updated_at
        BEFORE UPDATE ON courses FOR EACH ROW EXECUTE FUNCTION set_updated_at()
    """)

    op.execute("""
        CREATE TABLE convocatorias (
            id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            business_id         UUID NOT NULL REFERENCES businesses (id) ON DELETE RESTRICT,
            course_id           UUID REFERENCES courses (id) ON DELETE SET NULL,
            denominacion        TEXT NOT NULL,
            comunidad           TEXT NOT NULL CHECK (char_length(comunidad) BETWEEN 2 AND 64),
            fecha_inicio_plazo  DATE NOT NULL,
            fecha_fin_plazo     DATE NOT NULL,
            fecha_examen        DATE,
            fuente              TEXT NOT NULL,
            fuente_url          TEXT,
            created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT convocatorias_plazo_check CHECK (fecha_inicio_plazo < fecha_fin_plazo),
            CONSTRAINT convocatorias_examen_check
                CHECK (fecha_examen IS NULL OR fecha_examen >= fecha_inicio_plazo),
            CONSTRAINT convocatorias_natural_unique
                UNIQUE (business_id, comunidad, denominacion, fecha_inicio_plazo)
        )
    """)
    op.execute("""
        CREATE TRIGGER convocatorias_set_updated_at
        BEFORE UPDATE ON convocatorias FOR EACH ROW EXECUTE FUNCTION set_updated_at()
    """)
    # Lente de convocatoria: ventanas vivas de un negocio ordenadas por cierre.
    op.execute("""
        CREATE INDEX ix_convocatorias_business_plazo
        ON convocatorias (business_id, fecha_fin_plazo)
    """)
    op.execute("CREATE INDEX ix_convocatorias_course ON convocatorias (course_id)")

    op.execute(f"""
        CREATE TABLE lead_attributions (
            id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            business_id        UUID NOT NULL REFERENCES businesses (id) ON DELETE RESTRICT,
            hashed_identity    TEXT NOT NULL CHECK (hashed_identity ~ '{_SHA256_HEX}'),
            identity_salt_ref  TEXT NOT NULL
                                 CHECK (char_length(identity_salt_ref) BETWEEN 1 AND 128),
            click_id_hash      TEXT CHECK (click_id_hash ~ '{_SHA256_HEX}'),
            entity_ref         TEXT,
            convocatoria_id    UUID REFERENCES convocatorias (id) ON DELETE SET NULL,
            attribution_rung   TEXT NOT NULL CHECK (attribution_rung IN
                                 ('click_id', 'hashed_identity', 'aggregate')),
            conversion_kind    TEXT NOT NULL CHECK (conversion_kind IN
                                 ('lead', 'whatsapp', 'call', 'enrolment')),
            value_amount       NUMERIC(12, 2) NOT NULL DEFAULT 0 CHECK (value_amount >= 0),
            value_currency     TEXT NOT NULL CHECK (char_length(value_currency) = 3),
            occurred_at        TIMESTAMPTZ NOT NULL,
            observed_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
            created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),

            CONSTRAINT lead_attributions_entity_fk
                FOREIGN KEY (business_id, entity_ref)
                REFERENCES ad_entities (business_id, entity_ref) ON DELETE RESTRICT,
            CONSTRAINT lead_attributions_click_rung_check
                CHECK (attribution_rung <> 'click_id' OR click_id_hash IS NOT NULL),
            CONSTRAINT lead_attributions_entity_rung_check
                CHECK (attribution_rung = 'aggregate' OR entity_ref IS NOT NULL),
            -- Reingesta del CRM: UPSERT sobre la clave natural, nunca duplicado.
            CONSTRAINT lead_attributions_natural_unique
                UNIQUE (business_id, hashed_identity, conversion_kind, occurred_at)
        )
    """)
    op.execute("""
        COMMENT ON TABLE lead_attributions IS
        'Atribucion pseudonimizada. Prohibida toda columna de dato personal '
        '(C-31): solo hashes salados y agregados.'
    """)
    op.execute("""
        COMMENT ON COLUMN lead_attributions.identity_salt_ref IS
        'Alias de la sal por negocio que resuelve el broker de secretos. '
        'La sal jamas se almacena aqui.'
    """)
    # Conversiones por entidad y ventana: insumo de las senales y del cuadre.
    op.execute("""
        CREATE INDEX ix_lead_attributions_entity_time
        ON lead_attributions (entity_ref, occurred_at DESC)
        WHERE entity_ref IS NOT NULL
    """)
    # Conversiones tardias: "que ha llegado desde la ultima lectura" (NFR-7).
    op.execute("""
        CREATE INDEX ix_lead_attributions_business_observed
        ON lead_attributions (business_id, observed_at DESC)
    """)
    op.execute("""
        CREATE INDEX ix_lead_attributions_convocatoria
        ON lead_attributions (convocatoria_id)
        WHERE convocatoria_id IS NOT NULL
    """)


def downgrade() -> None:
    op.execute("DROP TABLE lead_attributions")
    op.execute("DROP TABLE convocatorias")
    op.execute("DROP TABLE courses")
