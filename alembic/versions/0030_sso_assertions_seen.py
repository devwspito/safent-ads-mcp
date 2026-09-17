"""0030_sso_assertions_seen: puente de sesion Safent -> safent-ads (026,
contracts/sso.md). Dos concerns bajo una migracion, mismo patron que 0029
(offering_economics + conversion_webhook_tokens en un unico fichero):

1. `sso_assertions_seen (jti UNIQUE, seen_at)`: antirrepetición de la
   aserción Ed25519 que firma el daemon (sso.md §3/§7 S-2). `jti` como
   PRIMARY KEY ya es UNIQUE; la purga >24h la hace
   `SqlAssertionReplayRepository.claim()` en cada canje (DELETE + INSERT en
   la misma llamada, sin depender de un cron nuevo) -- el índice sobre
   `seen_at` es lo único que esa purga necesita para ser barata.
2. `owners.bridge_subject`: TOFU del propietario único contra el `sub`
   firmado por el daemon (sso.md §4 "Resolución del propietario"). Nullable
   -- un propietario existente sin puente todavía no lo tiene; UNIQUE
   porque dos `sub` nunca pueden resolver al mismo propietario ni viceversa.

Revision ID: 0030_sso_assertions_seen
Revises: 0029_economics_inputs
Create Date: 2026-09-10
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0030_sso_assertions_seen"
down_revision: str | None = "0029_economics_inputs"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    _upgrade_sso_assertions_seen()
    _upgrade_owner_bridge_subject()


def downgrade() -> None:
    _downgrade_owner_bridge_subject()
    _downgrade_sso_assertions_seen()


def _upgrade_sso_assertions_seen() -> None:
    op.execute("""
        CREATE TABLE sso_assertions_seen (
            jti      TEXT PRIMARY KEY,
            seen_at  TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    op.execute("""
        CREATE INDEX sso_assertions_seen_seen_at_idx ON sso_assertions_seen (seen_at)
    """)


def _downgrade_sso_assertions_seen() -> None:
    op.execute("DROP TABLE sso_assertions_seen")


def _upgrade_owner_bridge_subject() -> None:
    op.execute("""
        ALTER TABLE owners ADD COLUMN bridge_subject TEXT UNIQUE
    """)


def _downgrade_owner_bridge_subject() -> None:
    op.execute("ALTER TABLE owners DROP COLUMN bridge_subject")
