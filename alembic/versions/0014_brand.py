"""0014_brand: `brand_kits`, un kit de marca por negocio (tool-surface.md
§2.2, §6: "Biblioteca de marca ... build; es el mayor diferencial por
euro"). Aditiva sobre `0010_notifications` (head actual en `main`): no
toca `0001`-`0010`, solo tabla nueva -- incluso si `0011_creative` o
`0013_panel_contract` (otras ramas, aun no fusionadas) llegan primero a
`main`, este fichero solo necesita que alguien reencadene `down_revision`,
nunca que se reescriba (data-model.md §Migration plan).

Un kit por negocio (UNIQUE en `business_id`): `save()` es upsert, coherente
con recargar `config/brand/<business>.yaml` completo en vez de editar
campo a campo. Subestructuras (`assets`, `palette`, `platform_constraints`,
...) en JSONB, mismo patron que `signals.gate_verdicts`/`proposals.evidence`
en migraciones anteriores -- son listas de VOs sin necesidad de consulta
relacional propia.

Revision ID: 0014_brand
Revises: 0010_notifications
Create Date: 2026-09-09
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0014_brand"
down_revision: str | None = "0012_domain_alignment"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE brand_kits (
            id                    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            business_id           UUID NOT NULL UNIQUE
                                     REFERENCES businesses (id) ON DELETE RESTRICT,
            primary_font          TEXT NOT NULL CHECK (char_length(primary_font) BETWEEN 1 AND 128),
            secondary_font        TEXT,
            font_licence_note     TEXT NOT NULL CHECK (char_length(font_licence_note) >= 1),
            font_weights          JSONB NOT NULL DEFAULT '[]',
            palette               JSONB NOT NULL DEFAULT '[]',
            tone_description      TEXT NOT NULL CHECK (char_length(tone_description) >= 1),
            tone_adjectives       JSONB NOT NULL DEFAULT '[]',
            tone_avoid            JSONB NOT NULL DEFAULT '[]',
            assets                JSONB NOT NULL DEFAULT '[]',
            claims_allowlist      JSONB NOT NULL DEFAULT '[]',
            forbidden_claims      JSONB NOT NULL DEFAULT '[]',
            legal_disclaimers     JSONB NOT NULL DEFAULT '[]',
            platform_constraints  JSONB NOT NULL DEFAULT '[]',
            created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at            TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    op.execute("""
        CREATE TRIGGER brand_kits_set_updated_at
        BEFORE UPDATE ON brand_kits FOR EACH ROW EXECUTE FUNCTION set_updated_at()
    """)
    op.execute("""
        COMMENT ON TABLE brand_kits IS
        'Kit de marca por negocio: logos, tipografia, paleta, tono, '
        'reclamos permitidos/prohibidos, avisos legales y restricciones '
        'por plataforma (tool-surface.md P1). Un kit por negocio.'
    """)


def downgrade() -> None:
    op.execute("DROP TABLE brand_kits")
