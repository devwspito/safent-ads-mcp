"""0015_brand_discovery: `brand_kits.is_confirmed` mas `brand_discovery_drafts`
(owner request: "El MCP debe pedir el sitio web del cliente (OPCIONAL)
para sacar la identidad de marca ... El usuario puede subir manual o
poner el enlace y que se rastree desde la web"). Aditiva sobre
`0014_brand.py`: no toca columnas existentes salvo anadir una con
`DEFAULT true` (todo kit ya guardado, autoria directa del propietario, se
considera confirmado por definicion).

Un borrador por negocio (UNIQUE en `business_id`, mismo patron upsert que
`brand_kits`): candidatos con confianza y procedencia por categoria en
JSONB -- listas de VOs sin consulta relacional propia, igual que
`brand_kits.assets`/`brand_kits.palette` en la migracion anterior.

Encadenada tras `0013_oauth_connect` (no `0014_brand`): esa migracion
llego a `main` despues, tambien encadenada tras `0014_brand`, mientras
esta rama trabajaba en paralelo -- mismo patron que ya documenta
`0013_oauth_connect` sobre `0014_brand`/`0012_domain_alignment` ("solo
necesita que alguien reencadene down_revision, nunca que se reescriba").
Ambas son aditivas y mutuamente independientes (tablas/columnas
distintas, sin FKs cruzadas). `alembic heads` debe imprimir una unica
cabeza tras esto.

Revision ID: 0015_brand_discovery
Revises: 0013_oauth_connect
Create Date: 2026-09-09
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0015_brand_discovery"
down_revision: str | None = "0013_oauth_connect"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE brand_kits
        ADD COLUMN is_confirmed BOOLEAN NOT NULL DEFAULT true
    """)
    op.execute("""
        COMMENT ON COLUMN brand_kits.is_confirmed IS
        'false solo para el kit que produce BrandDiscoveryDraft.merge_into_kit: '
        'un rastreo automatico nunca se usa como fuente de verdad hasta que '
        'el propietario lo confirma (ConfirmBrandDraft).'
    """)
    op.execute("""
        CREATE TABLE brand_discovery_drafts (
            id                        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            business_id               UUID NOT NULL UNIQUE
                                         REFERENCES businesses (id) ON DELETE RESTRICT,
            source_url                TEXT,
            discovered_at             TIMESTAMPTZ NOT NULL,
            logo_candidates           JSONB NOT NULL DEFAULT '[]',
            color_candidates          JSONB NOT NULL DEFAULT '[]',
            typography_candidates     JSONB NOT NULL DEFAULT '[]',
            business_name_candidates  JSONB NOT NULL DEFAULT '[]',
            social_links              JSONB NOT NULL DEFAULT '[]',
            contact_channels          JSONB NOT NULL DEFAULT '[]',
            copy_samples              JSONB NOT NULL DEFAULT '[]',
            created_at                TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at                TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    op.execute("""
        CREATE TRIGGER brand_discovery_drafts_set_updated_at
        BEFORE UPDATE ON brand_discovery_drafts FOR EACH ROW EXECUTE FUNCTION set_updated_at()
    """)
    op.execute("""
        COMMENT ON TABLE brand_discovery_drafts IS
        'Borrador de identidad de marca rastreado desde un sitio web (opcional) '
        'y/o subido a mano, pendiente de que el propietario lo confirme en un '
        'BrandKit real (ConfirmBrandDraft). Un borrador por negocio.'
    """)


def downgrade() -> None:
    op.execute("DROP TABLE brand_discovery_drafts")
    op.execute("ALTER TABLE brand_kits DROP COLUMN is_confirmed")
