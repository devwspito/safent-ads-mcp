"""0019_autonomy_confirmations: `autonomy_confirmations` (contracts/rest-api.md
`GET/POST /rules/autonomy-gate*`).

La puerta de autonomia (spec.md preguntas abiertas 2/3/8) se confirma **por
cuenta de plataforma**, no por negocio: dos cuentas del mismo negocio pueden
estar en distinto punto de la conversacion con el propietario. Estado
actual (UPSERT por `(platform_account_id, key)`, igual que `guardrails`):
el historico de que se confirmo y cuando ya lo deja `decision_log`
(`AutonomyConfirmationRecorded`, contracts/rest-api.md), esta tabla no
necesita ser solo-anexable.

`value` en TEXT: las tres preguntas (`q2_autonomous_decrease` bool,
`q3_monthly_cap` importe, `q8_browser_path` enum) no comparten tipo, y el
endpoint no valida el contenido de la respuesta del propietario mas alla de
su presencia -- ampliar a JSONB tipado es trabajo de una migracion futura si
`rules` necesita leerlo, no solo guardarlo.

Aditiva sobre `0018_confirmed_host` (head actual).

Revision ID: 0019_autonomy_confirmations
Revises: 0018_confirmed_host
Create Date: 2026-09-09
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0019_autonomy_confirmations"
down_revision: str | None = "0018_confirmed_host"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

_ALLOWED_KEYS = "q2_autonomous_decrease', 'q3_monthly_cap', 'q8_browser_path"


def upgrade() -> None:
    op.execute(f"""
        CREATE TABLE autonomy_confirmations (
            id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            business_id          UUID NOT NULL REFERENCES businesses (id) ON DELETE RESTRICT,
            platform_account_id  UUID NOT NULL REFERENCES platform_accounts (id)
                                   ON DELETE RESTRICT,
            key                  TEXT NOT NULL CHECK (key IN ('{_ALLOWED_KEYS}')),
            value                TEXT NOT NULL,
            comment              TEXT,
            confirmed_by         UUID NOT NULL REFERENCES owners (id) ON DELETE RESTRICT,
            confirmed_at         TIMESTAMPTZ NOT NULL DEFAULT now(),

            CONSTRAINT autonomy_confirmations_unique UNIQUE (platform_account_id, key)
        )
    """)
    op.execute("""
        COMMENT ON TABLE autonomy_confirmations IS
        'Estado vigente de la puerta de autonomia por cuenta (spec.md
        preguntas abiertas 2/3/8). UPSERT: el historico de confirmaciones
        vive en decision_log (AutonomyConfirmationRecorded), no aqui.'
    """)
    op.execute(
        "CREATE INDEX ix_autonomy_confirmations_business "
        "ON autonomy_confirmations (business_id)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS autonomy_confirmations")
