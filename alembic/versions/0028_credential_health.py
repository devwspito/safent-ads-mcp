"""0028_credential_health: cron de salud de credenciales (tasks.md T126,
threat-model.md C-21).

`credential_refs` gana dos columnas:

- `checked_at` — ultima vez que `CheckCredentialHealth` pregunto al broker,
  se moviera o no `status` (distinto de `last_validated_at`, 0013: esa solo
  se mueve cuando el chequeo confirma `CONNECTED`).
- `last_error_code` — motivo corto del ultimo chequeo no saludable
  ("TOKEN_EXPIRED"/"TOKEN_EXPIRING_SOON"/"CREDENTIAL_REVOKED"/
  "CREDENTIAL_NOT_FOUND"), nunca el detalle crudo del proveedor. Cierra el
  hueco de `GET /platform-accounts` documentado en
  checklists/panel-contract-followups.md #4 ("... `last_error_code` sin
  fuente persistida -> null").

Revision ID: 0028_credential_health
Revises: 0027_us5_opportunities
Create Date: 2026-09-10
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0028_credential_health"
down_revision: str | None = "0027_us5_opportunities"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE credential_refs
            ADD COLUMN checked_at TIMESTAMPTZ,
            ADD COLUMN last_error_code TEXT
    """)


def downgrade() -> None:
    op.execute("""
        ALTER TABLE credential_refs
            DROP COLUMN checked_at,
            DROP COLUMN last_error_code
    """)
