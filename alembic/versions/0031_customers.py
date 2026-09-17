"""0031_customers: `customers` + `identity_mappings` (spec 027 T014,
data-model.md §Customer/§IdentityMapping): el valor de un negocio de
servicios esta en el cliente entero (LTV), no en la primera venta -- estas
dos tablas son la identidad hasheada que ancla ese valor.

`customers`: una fila por `(business_id, identity_digest)`. Prohibido el
dato personal -- ni email, ni telefono, ni nombre, ni opcional (mismo
criterio que `lead_attributions`, threat-model.md C-31, `0005_catalog_crm`
`test_no_pii_columns`). `entity_ref` es TEXT igual que en `lead_attributions`
(sin FK dura: `connectors`/`accounts` tienen ciclos de vida distintos).

`identity_mappings`: solo-anexable, fundir escribe `merged_into` sin
borrar la fila absorbida (data-model.md: "el mismo cliente por dos vias
queda trazable").

Expand puro: no toca `lead_attributions` ni ninguna tabla existente
(data-model.md §Migration plan: "Sin contract. Nada se borra").

Revision ID: 0031_customers
Revises: 0030_sso_assertions_seen
Create Date: 2026-09-10
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0031_customers"
down_revision: str | None = "0030_sso_assertions_seen"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    _upgrade_customers()
    _upgrade_identity_mappings()


def downgrade() -> None:
    _downgrade_identity_mappings()
    _downgrade_customers()


def _upgrade_customers() -> None:
    op.execute("""
        CREATE TABLE customers (
            id                        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            business_id               UUID NOT NULL REFERENCES businesses (id) ON DELETE RESTRICT,
            identity_digest           TEXT NOT NULL CHECK (identity_digest ~ '^[0-9a-fA-F]{64}$'),
            salt_version              INTEGER NOT NULL CHECK (salt_version >= 1),
            entity_ref                TEXT,
            attribution_rung          TEXT NOT NULL
                CHECK (attribution_rung IN ('click_id', 'utm', 'hashed_identity', 'aggregate')),
            first_paid_conversion_at  TIMESTAMPTZ,
            state                     TEXT NOT NULL DEFAULT 'lead'
                CHECK (state IN ('lead', 'active', 'churned')),
            currency                  TEXT NOT NULL CHECK (char_length(currency) = 3),
            first_seen_at             TIMESTAMPTZ NOT NULL,
            last_seen_at              TIMESTAMPTZ NOT NULL,
            source_connector_id       TEXT,

            CONSTRAINT customers_business_identity_unique UNIQUE (business_id, identity_digest)
        )
    """)
    op.execute("""
        COMMENT ON TABLE customers IS
        'Prohibido el dato personal (data-model.md §Customer invariante 1):
        la identidad es identity_digest (sha256 hex), nunca email/telefono/
        nombre. first_paid_conversion_at se deriva del RevenueEvent
        first_payment mas antiguo -- IngestRevenueEvents lo mantiene.'
    """)
    op.execute("""
        CREATE INDEX ix_customers_business_first_paid
        ON customers (business_id, first_paid_conversion_at DESC)
    """)
    op.execute("""
        CREATE INDEX ix_customers_active
        ON customers (business_id) WHERE state = 'active'
    """)
    op.execute("""
        CREATE INDEX ix_customers_business_entity_ref
        ON customers (business_id, entity_ref) WHERE entity_ref IS NOT NULL
    """)


def _downgrade_customers() -> None:
    op.execute("DROP TABLE customers")


def _upgrade_identity_mappings() -> None:
    op.execute("""
        CREATE TABLE identity_mappings (
            id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            business_id      UUID NOT NULL REFERENCES businesses (id) ON DELETE RESTRICT,
            identity_digest  TEXT NOT NULL CHECK (identity_digest ~ '^[0-9a-fA-F]{64}$'),
            salt_version     INTEGER NOT NULL CHECK (salt_version >= 1),
            merged_into      TEXT CHECK (merged_into ~ '^[0-9a-fA-F]{64}$'),
            observed_at      TIMESTAMPTZ NOT NULL,

            CONSTRAINT identity_mappings_unique UNIQUE (business_id, identity_digest, salt_version)
        )
    """)
    op.execute("""
        COMMENT ON TABLE identity_mappings IS
        'Solo-anexable (data-model.md §IdentityMapping): fundir dos
        identidades escribe merged_into, nunca borra la fila absorbida.'
    """)
    op.execute("CREATE INDEX ix_identity_mappings_merged_into ON identity_mappings (merged_into)")


def _downgrade_identity_mappings() -> None:
    op.execute("DROP TABLE identity_mappings")
