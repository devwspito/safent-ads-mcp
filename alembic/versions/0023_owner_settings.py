"""0023_owner_settings: `PUT /settings` (contracts/rest-api.md §Ajustes) y
las dos columnas de `proposals` que el panel necesita para
owner-context/postpone (§Propuestas).

Aditiva sobre `0020_totp_replay_guard` (head de esta rama): otra rama en
paralelo anade `0021_telegram_pairing` sobre la misma base -- `main`
rencadena ambas al fusionar, este fichero no asume ser el head final.

- `businesses.active_hours_start`/`active_hours_end`: horario activo por
  negocio (`ActiveHoursWindow`); si estan a NULL, `ads-worker` sigue
  usando el valor por defecto de `ADS_ACTIVE_HOURS` (env). Los dos viajan
  juntos o ninguno (`num_nulls` = 0 o 2), igual que el patron ya usado en
  `ad_entities.budget_*`/`ad_entities.bid_target_*` (0003/0011).
- `businesses.digest_hour`: hora del digest diario (0-23); todavia sin
  consumidor propio en `orchestration/` (el digest hoy se envia a la
  primera hora activa, no a una hora fija) -- se persiste porque el
  contrato del panel lo pide, el cableado de un disparador propio queda
  fuera de esta rama.
- `proposals.postponed_reason`: `owner|attention_budget`
  (`proposals.domain.proposal.PostponedReason`); exigido en vivo cuando
  `state = 'postponed'`, igual que `postponed_until` ya lo exige
  (`proposals_postponed_needs_time_check`, 0008_proposals).
- `proposals.owner_context`: nota libre del propietario, <=500 caracteres
  (tambien validado en el agregado, `Proposal.set_owner_context` --
  defensa en profundidad, nunca solo el borde HTTP).
- `owner_preferences`: `theme` por `owner_id` (PUT /settings), no por
  negocio -- un unico propietario ve el mismo tema en todos sus negocios.

Revision ID: 0023_owner_settings
Revises: 0020_totp_replay_guard
Create Date: 2026-09-09
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0023_owner_settings"
down_revision: str | None = "0022_telegram_pairing"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE businesses
            ADD COLUMN active_hours_start TIME,
            ADD COLUMN active_hours_end   TIME,
            ADD COLUMN digest_hour        SMALLINT CHECK (digest_hour BETWEEN 0 AND 23)
    """)
    op.execute("""
        ALTER TABLE businesses
            ADD CONSTRAINT businesses_active_hours_pair_check
            CHECK (num_nulls(active_hours_start, active_hours_end) IN (0, 2))
    """)

    op.execute("""
        ALTER TABLE proposals
            ADD COLUMN postponed_reason TEXT
                CHECK (postponed_reason IN ('owner', 'attention_budget')),
            ADD COLUMN owner_context    TEXT
                CHECK (char_length(owner_context) <= 500)
    """)
    # Simetrico a `proposals_postponed_needs_time_check` (0008_proposals):
    # pospuesta sin motivo seria un estado a medias.
    op.execute("""
        ALTER TABLE proposals
            ADD CONSTRAINT proposals_postponed_needs_reason_check
            CHECK (state <> 'postponed' OR postponed_reason IS NOT NULL)
    """)

    op.execute("""
        CREATE TABLE owner_preferences (
            owner_id    UUID PRIMARY KEY REFERENCES owners (id),
            theme       TEXT NOT NULL DEFAULT 'system'
                         CHECK (theme IN ('light', 'dark', 'system')),
            updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    op.execute("""
        CREATE TRIGGER owner_preferences_set_updated_at
        BEFORE UPDATE ON owner_preferences FOR EACH ROW EXECUTE FUNCTION set_updated_at()
    """)


def downgrade() -> None:
    op.execute("DROP TABLE owner_preferences")
    op.execute("""
        ALTER TABLE proposals
            DROP CONSTRAINT proposals_postponed_needs_reason_check
    """)
    op.execute("""
        ALTER TABLE proposals
            DROP COLUMN postponed_reason,
            DROP COLUMN owner_context
    """)
    op.execute("""
        ALTER TABLE businesses
            DROP CONSTRAINT businesses_active_hours_pair_check
    """)
    op.execute("""
        ALTER TABLE businesses
            DROP COLUMN active_hours_start,
            DROP COLUMN active_hours_end,
            DROP COLUMN digest_hour
    """)
