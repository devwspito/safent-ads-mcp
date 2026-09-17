"""0016_economics: `unit_economics_profiles` (solo-anexable, versionado),
`lag_curve_snapshots` (materializado, se sobrescribe) y
`platform_divergence_snapshots` (historico, para detectar saltos).

profitability-engine.md §1: "UnitEconomicsProfile: agregado por
(business_id, course_id), versionado con effective_from y solo-anexable —
cambiar el precio crea version, no reescribe historia (NFR-7)". `product_id`
referencia `courses(id)`: no existe todavia una tabla `products` generica
(regla del producto: vocabulario generico en codigo nuevo, adaptado en el
borde a lo que ya existe en el esquema).

`lag_curve_snapshots` SI se sobrescribe (UPSERT por `MaintenanceCycle`, es
cache, no fuente de verdad); `platform_divergence_snapshots` es historico
solo-anexable porque `is_jump_anomaly` compara contra el periodo anterior
(profitability-engine.md §2).

Aditiva: no toca `0001`-`0015`.

Rechain (us3-wiring-b, integration-engineer): `0014_brand` tenia DOS hijos
directos (`0013_oauth_connect`->`0015_brand_discovery` y este fichero),
osea dos heads de alembic (`0015_brand_discovery`, `0017_optimization`) que
rompian cualquier `alembic upgrade head` con "Multiple head revisions" --
bloqueaba TODA la suite de integracion, no solo esta rama. Mismo patron que
`c3178ef fix(migrations): rechain 0015_brand_discovery after 0013_oauth_connect`:
esta migracion es aditiva y no depende de nada de `0013`/`0015` (oauth/brand),
asi que encadenarla despues de ellas en vez de en paralelo es seguro.

Revision ID: 0016_economics
Revises: 0015_brand_discovery
Create Date: 2026-09-09
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0016_economics"
down_revision: str | None = "0015_brand_discovery"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    _create_unit_economics_profiles()
    _create_lag_curve_snapshots()
    _create_platform_divergence_snapshots()


def _create_unit_economics_profiles() -> None:
    op.execute("""
        CREATE TABLE unit_economics_profiles (
            id                       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            business_id              UUID NOT NULL REFERENCES businesses (id) ON DELETE RESTRICT,
            product_id               UUID NOT NULL REFERENCES courses (id) ON DELETE RESTRICT,
            version                  INTEGER NOT NULL CHECK (version >= 1),
            effective_from           DATE NOT NULL,
            status                   TEXT NOT NULL
                                       CHECK (status IN ('provisional', 'confirmed')),
            list_price_amount        NUMERIC(14, 2) NOT NULL CHECK (list_price_amount >= 0),
            list_price_currency      TEXT NOT NULL CHECK (char_length(list_price_currency) = 3),
            vat_rate                 NUMERIC(5, 4) NOT NULL CHECK (vat_rate BETWEEN 0 AND 1),
            discount_rate            NUMERIC(5, 4) NOT NULL CHECK (discount_rate BETWEEN 0 AND 1),
            refund_rate              NUMERIC(5, 4) NOT NULL CHECK (refund_rate BETWEEN 0 AND 1),
            delivery_cost_amount     NUMERIC(14, 2) NOT NULL CHECK (delivery_cost_amount >= 0),
            sales_cost_per_close_amount NUMERIC(14, 2) NOT NULL
                                       CHECK (sales_cost_per_close_amount >= 0),
            collection_rate          NUMERIC(5, 4) NOT NULL CHECK (collection_rate BETWEEN 0 AND 1),
            cvr_lead_to_enrolment    NUMERIC(6, 5) NOT NULL
                                       CHECK (cvr_lead_to_enrolment BETWEEN 0 AND 1),
            theta                    NUMERIC(5, 4) NOT NULL CHECK (theta BETWEEN 0.20 AND 1),
            margin_horizon_days      INTEGER NOT NULL CHECK (margin_horizon_days >= 0),
            contribution_margin_override_amount NUMERIC(14, 2),
            created_at               TIMESTAMPTZ NOT NULL DEFAULT now(),

            CONSTRAINT unit_economics_profiles_version_unique
                UNIQUE (business_id, product_id, version),
            CONSTRAINT unit_economics_profiles_effective_from_unique
                UNIQUE (business_id, product_id, effective_from)
        )
    """)
    op.execute("""
        COMMENT ON TABLE unit_economics_profiles IS
        'Solo-anexable (NFR-7): cambiar el precio crea una version nueva con
        effective_from posterior, nunca reescribe la vigente.'
    """)
    op.execute("""
        CREATE INDEX ix_unit_economics_profiles_current
        ON unit_economics_profiles (business_id, product_id, effective_from DESC)
    """)

    op.execute("""
        CREATE FUNCTION unit_economics_profiles_immutable() RETURNS TRIGGER AS $$
        BEGIN
            RAISE EXCEPTION
                'unit_economics_profiles es append-only: % no permitido', TG_OP;
        END;
        $$ LANGUAGE plpgsql
    """)
    op.execute("""
        CREATE TRIGGER unit_economics_profiles_no_update
        BEFORE UPDATE ON unit_economics_profiles
        FOR EACH ROW EXECUTE FUNCTION unit_economics_profiles_immutable()
    """)
    op.execute("""
        CREATE TRIGGER unit_economics_profiles_no_delete
        BEFORE DELETE ON unit_economics_profiles
        FOR EACH ROW EXECUTE FUNCTION unit_economics_profiles_immutable()
    """)


def _create_lag_curve_snapshots() -> None:
    op.execute("""
        CREATE TABLE lag_curve_snapshots (
            id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            business_id       UUID NOT NULL REFERENCES businesses (id) ON DELETE RESTRICT,
            product_id        UUID NOT NULL REFERENCES courses (id) ON DELETE RESTRICT,
            platform          TEXT NOT NULL CHECK (platform IN ('google', 'meta')),
            d_max             INTEGER NOT NULL CHECK (d_max > 0),
            sample_size       INTEGER NOT NULL CHECK (sample_size >= 0),
            cumulative_by_day JSONB NOT NULL,
            computed_at       TIMESTAMPTZ NOT NULL DEFAULT now(),

            CONSTRAINT lag_curve_snapshots_unique UNIQUE (business_id, product_id, platform)
        )
    """)
    op.execute("""
        COMMENT ON TABLE lag_curve_snapshots IS
        'Materializada, no solo-anexable: el MaintenanceCycle la recalcula
        (UPSERT) porque re-ejecutar Kaplan-Meier en cada lectura es caro.'
    """)


def _create_platform_divergence_snapshots() -> None:
    op.execute("""
        CREATE TABLE platform_divergence_snapshots (
            id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            business_id          UUID NOT NULL REFERENCES businesses (id) ON DELETE RESTRICT,
            platform_account_id  UUID NOT NULL REFERENCES platform_accounts (id)
                                    ON DELETE RESTRICT,
            crm_conversions      INTEGER NOT NULL CHECK (crm_conversions >= 0),
            platform_conversions INTEGER NOT NULL CHECK (platform_conversions >= 0),
            shrinkage_m          INTEGER NOT NULL CHECK (shrinkage_m > 0),
            value                NUMERIC(8, 4) NOT NULL,
            window_start         DATE NOT NULL,
            window_end           DATE NOT NULL,
            computed_at          TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),

            CONSTRAINT platform_divergence_window_check CHECK (window_start < window_end)
        )
    """)
    op.execute("""
        COMMENT ON COLUMN platform_divergence_snapshots.computed_at IS
        'clock_timestamp(), no now(): dos snapshots guardados en la misma
        transaccion deben ordenar por insercion real, no por el instante
        congelado de inicio de transaccion (is_jump_anomaly compara contra
        el anterior).'
    """)
    op.execute("""
        CREATE INDEX ix_platform_divergence_snapshots_latest
        ON platform_divergence_snapshots (business_id, platform_account_id, computed_at DESC)
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS platform_divergence_snapshots")
    op.execute("DROP TABLE IF EXISTS lag_curve_snapshots")
    op.execute(
        "DROP TRIGGER IF EXISTS unit_economics_profiles_no_delete ON unit_economics_profiles"
    )
    op.execute(
        "DROP TRIGGER IF EXISTS unit_economics_profiles_no_update ON unit_economics_profiles"
    )
    op.execute("DROP FUNCTION IF EXISTS unit_economics_profiles_immutable()")
    op.execute("DROP TABLE IF EXISTS unit_economics_profiles")
