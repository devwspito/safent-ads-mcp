"""0020_totp_replay_guard: `totp_reauth_confirmations` (security review
F2/F3, finding 2a -- threat-model.md "TOTP re-auth" FAIL, CWE-294).

`_require_reauth` (composition/execution_rest.py) verificaba el codigo TOTP
y lo descartaba, nunca lo quemaba: el mismo codigo, valido ~90s por la
tolerancia de `PyotpTotpVerifier`, se podia repetir contra cualquier ruta
`_require_reauth` tantas veces como cupiese en la ventana. Esta tabla
registra el contador RFC 6238 (`time_step`, paso de 30s) que de verdad hizo
match para un propietario -- `UNIQUE (owner_id, time_step)` hace que un
segundo intento con el MISMO codigo choque en el INSERT sin importar para
que accion se use, cerrando a la vez el reuso puro (mismo codigo, misma
peticion repetida) y el reuso cruzado (mismo codigo, otra accion: p. ej.
confirmar los tres limites de autonomia con una sola pulsacion del
propietario). `action_hash` no participa en la clave de unicidad a
proposito -- si lo hiciera, el mismo codigo podria quemarse una vez por
accion distinta y el reuso cruzado seguiria abierto; se guarda solo para
auditoria (que accion exacta confirmo ese codigo).

Aditiva sobre `0019_autonomy_confirmations` (head actual).

Revision ID: 0020_totp_replay_guard
Revises: 0019_autonomy_confirmations
Create Date: 2026-09-09
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0020_totp_replay_guard"
down_revision: str | None = "0019_autonomy_confirmations"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE totp_reauth_confirmations (
            id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            owner_id      UUID NOT NULL REFERENCES owners (id) ON DELETE RESTRICT,
            time_step     BIGINT NOT NULL,
            action_hash   TEXT NOT NULL,
            confirmed_at  TIMESTAMPTZ NOT NULL DEFAULT now(),

            CONSTRAINT totp_reauth_confirmations_unique UNIQUE (owner_id, time_step)
        )
    """)
    op.execute("""
        COMMENT ON TABLE totp_reauth_confirmations IS
        'Quema de codigos TOTP de re-autenticacion (_require_reauth):
        UNIQUE (owner_id, time_step) hace que un codigo ya usado nunca
        vuelva a verificar, ni para la misma accion ni para otra
        (threat-model.md TOTP re-auth, defecto 1/2, security review F2/F3).'
    """)
    op.execute(
        "CREATE INDEX ix_totp_reauth_confirmations_owner "
        "ON totp_reauth_confirmations (owner_id)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS totp_reauth_confirmations")
