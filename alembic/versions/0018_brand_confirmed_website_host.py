"""0016_brand_confirmed_website_host: `brand_kits.confirmed_website_host`
(F-8, checklists/website-brand-extractor-review.md: "guardar el sitio
declarado del negocio y que la herramienta MCP no acepte URL"). Aditiva
sobre `0015_brand_discovery.py`: una columna nueva, nula por defecto --
ningun negocio existente tiene un dominio confirmado hasta su primer
rastreo exitoso por REST (`IngestBrandFromWebsite.execute`).

Revision ID: 0016_confirmed_host
Revises: 0015_brand_discovery
Create Date: 2026-09-09
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0018_confirmed_host"
down_revision: str | None = "0017_optimization"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE brand_kits
        ADD COLUMN confirmed_website_host TEXT
    """)
    op.execute("""
        COMMENT ON COLUMN brand_kits.confirmed_website_host IS
        'Host del sitio web que un propietario autenticado confirmo por REST '
        '(POST /api/v1/brand/discover). La herramienta MCP ingest_brand_from_website '
        'solo puede rastrear este dominio -- nunca acepta una URL propia (F-8).'
    """)


def downgrade() -> None:
    op.execute("ALTER TABLE brand_kits DROP COLUMN confirmed_website_host")
