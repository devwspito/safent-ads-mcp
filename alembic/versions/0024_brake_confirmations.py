"""0024_brake_confirmations: `telegram_brake_confirmations`, el nonce de
segundo toque de `/freno on|off` (contracts/telegram.md: "requiere segundo
toque de confirmacion").

No reutiliza `telegram_callbacks` (0010_notifications): esa tabla exige
`proposal_id UUID NOT NULL REFERENCES proposals`, y el freno de emergencia
no es una propuesta -- es un interruptor global sin entidad de `proposals`
que asociar (mismo motivo que documenta `0022_telegram_pairing` para no
reusar `notifications` en `telegram_test_messages`).

Sin `message_id`: a diferencia de una tarjeta de aprobacion (que EDITA un
mensaje existente), el primer toque de `/freno on|off` siempre es un
mensaje nuevo -- el nonce nace ANTES de enviarlo (no hay problema de
orden como con `telegram_callbacks.create`), asi que no necesita atarse a
un `message_id` que todavia no existe. La ventana de confirmacion es
corta (5 min, fijada en `notifications/application/resolve_brake_command.py`)
a proposito: una confirmacion de freno vieja re-descubierta horas despues
seria mas peligrosa que util.

Revision ID: 0024_brake_confirmations
Revises: 0023_owner_settings
Create Date: 2026-09-10
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0024_brake_confirmations"
down_revision: str | None = "0023_owner_settings"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE telegram_brake_confirmations (
            nonce           TEXT PRIMARY KEY CHECK (nonce ~ '^[A-Za-z0-9]{10}$'),
            chat_id         BIGINT NOT NULL,
            pending_action  TEXT NOT NULL CHECK (pending_action IN ('on', 'off')),
            consumed_at     TIMESTAMPTZ,
            expires_at      TIMESTAMPTZ NOT NULL,
            created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),

            CONSTRAINT telegram_brake_confirmations_ttl_check CHECK (expires_at > created_at)
        )
    """)
    op.execute("""
        COMMENT ON TABLE telegram_brake_confirmations IS
        'Nonce de un solo uso del segundo toque de /freno on|off
        (contracts/telegram.md). pending_action es la accion fijada en el
        PRIMER toque -- el boton del segundo toque nunca la elige.'
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS telegram_brake_confirmations")
