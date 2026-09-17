"""0021_creative_review: `creative_briefs`, `creative_assets`,
`creative_jobs` (gap-creatives lane, rest-api.md §Creatividades,
creative-port.md).

Ninguna tabla de `creative` habia aterrizado todavia: el contexto solo
tenia repositorios en memoria
(`creative/infrastructure/in_memory_repositories.py`) -- "0011_creative"
citado en varios docstrings de ese paquete nunca se creo (el hueco 0011 lo
ocupo `0011_ad_entity_money`, de otra rama). Esta es la primera migracion
real de `creative`: sin fila previa que migrar, `upgrade`/`downgrade` no
tienen que preservar datos.

`review_state` NO es una columna propia: `CreativeAsset.review_state`
(dominio) se deriva de `state` (`DRAFT/READY/PROPOSED` -> `pending`,
`APPROVED/PUBLISHED` -> `approved`, `REJECTED` -> `rejected`) -- anadir una
segunda columna la desincronizaria de `state` en el primer `UPDATE` que se
olvide de tocarla las dos. Subestructuras sin consulta relacional propia
(`shots`, `brand_kit`, `ad_copy`, `policy_findings`) en JSONB, mismo patron
que `brand_kits` (0014_brand.py).

Revision ID: 0021_creative_review
Revises: 0020_totp_replay_guard
Create Date: 2026-09-09
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0021_creative_review"
down_revision: str | None = "0020_totp_replay_guard"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

_CREATIVE_ASSET_STATES = (
    "'draft'", "'ready'", "'proposed'", "'approved'", "'rejected'", "'published'",
)
_CREATIVE_JOB_STATES = (
    "'queued'", "'rendering'", "'composing'", "'checking'", "'ready'", "'failed'",
    "'fallback_cloud'",
)
_MEDIA_KINDS = ("'image'", "'video'", "'banner'", "'audio'")
_GENERATION_STATUSES = ("'model_generated'", "'composer_only_fallback'")
_POLICY_VERDICTS = ("'PASS'", "'WARN'", "'FAIL'")
_OUTCOMES = ("'pending'", "'winner'", "'loser'", "'fatigue'")


def upgrade() -> None:
    op.execute("""
        CREATE TABLE creative_briefs (
            id                  TEXT PRIMARY KEY,
            business_id         UUID NOT NULL REFERENCES businesses (id) ON DELETE RESTRICT,
            convocatoria_id     UUID,
            objective           TEXT NOT NULL,
            audience_summary    TEXT NOT NULL,
            hook                TEXT NOT NULL,
            shots               JSONB NOT NULL,
            on_screen_text      JSONB NOT NULL DEFAULT '[]',
            cta                 TEXT NOT NULL,
            voiceover_lines     JSONB NOT NULL DEFAULT '[]',
            brand_kit           JSONB NOT NULL,
            source_signal_id    UUID,
            variant_count       INT NOT NULL CHECK (variant_count BETWEEN 1 AND 8),
            language            TEXT NOT NULL DEFAULT 'es-ES',
            created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    op.execute("CREATE INDEX creative_briefs_business_id_idx ON creative_briefs (business_id)")

    op.execute(f"""
        CREATE TABLE creative_assets (
            id                      TEXT PRIMARY KEY,
            business_id             UUID NOT NULL REFERENCES businesses (id) ON DELETE RESTRICT,
            media_kind              TEXT NOT NULL CHECK (media_kind IN ({", ".join(_MEDIA_KINDS)})),
            format                  TEXT,
            duration_seconds        NUMERIC,
            storage_uri             TEXT NOT NULL,
            checksum                TEXT NOT NULL CHECK (checksum ~ '^[0-9a-f]{{64}}$'),
            cost_estimate_amount    NUMERIC NOT NULL,
            cost_estimate_currency  TEXT NOT NULL CHECK (char_length(cost_estimate_currency) = 3),
            renderer_used           TEXT NOT NULL,
            model_name              TEXT NOT NULL,
            seed                    INT,
            brief_id                TEXT NOT NULL
                                       REFERENCES creative_briefs (id) ON DELETE RESTRICT,
            source_signal_id        UUID,
            generation_status       TEXT NOT NULL
                                       CHECK (generation_status
                                              IN ({", ".join(_GENERATION_STATUSES)})),
            generated_at            TIMESTAMPTZ NOT NULL,
            ad_copy                 JSONB,
            destination_url         TEXT,
            state                   TEXT NOT NULL DEFAULT 'draft'
                                       CHECK (state IN ({", ".join(_CREATIVE_ASSET_STATES)})),
            policy_verdict          TEXT CHECK (policy_verdict IN ({", ".join(_POLICY_VERDICTS)})),
            policy_findings         JSONB,
            outcome                 TEXT NOT NULL DEFAULT 'pending'
                                       CHECK (outcome IN ({", ".join(_OUTCOMES)})),
            created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at              TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    op.execute("CREATE INDEX creative_assets_business_id_idx ON creative_assets (business_id)")
    op.execute("CREATE INDEX creative_assets_brief_id_idx ON creative_assets (brief_id)")
    op.execute("""
        CREATE TRIGGER creative_assets_set_updated_at
        BEFORE UPDATE ON creative_assets FOR EACH ROW EXECUTE FUNCTION set_updated_at()
    """)

    op.execute(f"""
        CREATE TABLE creative_jobs (
            id                  TEXT PRIMARY KEY,
            business_id         UUID NOT NULL REFERENCES businesses (id) ON DELETE RESTRICT,
            brief_id            TEXT NOT NULL REFERENCES creative_briefs (id) ON DELETE RESTRICT,
            brief_hash          TEXT NOT NULL,
            variant_index       INT NOT NULL CHECK (variant_index >= 0),
            state               TEXT NOT NULL DEFAULT 'queued'
                                   CHECK (state IN ({", ".join(_CREATIVE_JOB_STATES)})),
            progress            NUMERIC NOT NULL DEFAULT 0 CHECK (progress BETWEEN 0 AND 1),
            asset_ids           JSONB NOT NULL DEFAULT '[]',
            failure_reason      TEXT,
            created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
            UNIQUE (brief_hash, variant_index)
        )
    """)
    op.execute("CREATE INDEX creative_jobs_business_id_idx ON creative_jobs (business_id)")
    op.execute("""
        CREATE TRIGGER creative_jobs_set_updated_at
        BEFORE UPDATE ON creative_jobs FOR EACH ROW EXECUTE FUNCTION set_updated_at()
    """)

    op.execute("""
        COMMENT ON TABLE creative_briefs IS
        'Brief creativo inmutable (data-model.md): un CreativeJob renderiza variantes de este.'
    """)
    op.execute("""
        COMMENT ON TABLE creative_assets IS
        'Pieza creativa generada/importada (FR-34): review_state se deriva de state, no es columna.'
    """)
    op.execute("""
        COMMENT ON TABLE creative_jobs IS
        'Trabajo de render (T098): idempotente por (brief_hash, variant_index).'
    """)


def downgrade() -> None:
    op.execute("DROP TABLE creative_jobs")
    op.execute("DROP TABLE creative_assets")
    op.execute("DROP TABLE creative_briefs")
