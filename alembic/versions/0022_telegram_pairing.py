"""0022_telegram_pairing: `telegram_owner_chats` (data-model.md
§OwnerTelegramChat, FR-25), `telegram_pairing_attempts` (limite de 3
intentos por chat y hora, contracts/telegram.md §Emparejamiento) y
`telegram_test_messages` (cola de `POST /telegram/pairing/test-message`:
`ads-api` encola, `ads-worker` -- unico proceso con el `Bot` real --
entrega).

`telegram_owner_chats` liga `chat_id -> owner_id`, NO liga a ningun
`business_id`: el emparejamiento es del propietario (una sola cuenta de
Telegram por instalacion de propietario unico, data-model.md), nunca del
negocio que el panel tenga abierto -- por eso no hay FK a `businesses` ni
columna `business_id` (mismo criterio documentado que
`oauth_reconnect_sessions`/`autonomy_confirmations` de 0013/0019: cada
tabla nueva lleva solo las FKs que su invariante real exige). `chat_id`
solo se conoce tras `/emparejar` (C-4: la allow-list nunca se amplia, este
emparejamiento solo AUTENTICA un chat ya permitido), asi que nace NULL.

`pairing_code_hash` es sha256 del codigo (`^[A-Z2-9]{8}$`, un solo uso, TTL
10 min): es lo que el bot compara en tiempo constante. `pairing_code_encrypted`
es AES-256-GCM (misma clave `ADS_TOTP_ENC_KEY` que ya cifra el secreto TOTP,
`AesGcmTotpCipher` reusada, no un secreto nuevo que desplegar) del mismo
codigo en claro: sin esto, `GET /telegram/pairing` no podria reponer el
codigo tras refrescar la pagina mientras el emparejamiento sigue `pending`
-- solo el hash no basta para eso, es de un solo sentido a proposito.

`telegram_test_messages` NO reutiliza la tabla `notifications` (0010): esa
tiene `business_id UUID NOT NULL REFERENCES businesses`, y un mensaje de
prueba de emparejamiento no tiene negocio real que asociar (mismo problema
que ya resolvio `execution_rest.py::_business_id_for_scope` con un id
centinela para el freno global -- aqui, en vez de inventar un `business_id`
que no existe y romper la FK, la cola vive en su propia tabla pequena).

Aditiva sobre `0020_totp_replay_guard` (head actual).

Revision ID: 0022_telegram_pairing
Revises: 0020_totp_replay_guard
Create Date: 2026-09-09
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0022_telegram_pairing"
down_revision: str | None = "0021_creative_review"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE telegram_owner_chats (
            id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            owner_id                UUID NOT NULL UNIQUE
                                       REFERENCES owners (id) ON DELETE RESTRICT,
            chat_id                 BIGINT,
            status                  TEXT NOT NULL DEFAULT 'unpaired'
                                       CHECK (status IN ('unpaired', 'pending', 'paired')),
            pairing_code_hash       TEXT,
            pairing_code_encrypted  BYTEA,
            code_expires_at         TIMESTAMPTZ,
            verified_at             TIMESTAMPTZ,
            last_test_at            TIMESTAMPTZ,
            created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at              TIMESTAMPTZ NOT NULL DEFAULT now(),

            CONSTRAINT telegram_owner_chats_paired_has_chat CHECK (
                status <> 'paired' OR (chat_id IS NOT NULL AND verified_at IS NOT NULL)
            )
        )
    """)
    op.execute("""
        COMMENT ON TABLE telegram_owner_chats IS
        'Emparejamiento chat_id -> owner_id (FR-25). La allow-list
        TELEGRAM_OWNER_CHAT_IDS sigue siendo la UNICA autorizacion (C-4):
        esta tabla nunca la amplia, solo verifica identidad dentro de ella.'
    """)
    op.execute("""
        CREATE TRIGGER telegram_owner_chats_set_updated_at
        BEFORE UPDATE ON telegram_owner_chats FOR EACH ROW EXECUTE FUNCTION set_updated_at()
    """)
    # Un chat solo puede estar emparejado con un propietario a la vez.
    op.execute("""
        CREATE UNIQUE INDEX ix_telegram_owner_chats_paired_chat
        ON telegram_owner_chats (chat_id) WHERE status = 'paired'
    """)

    op.execute("""
        CREATE TABLE telegram_pairing_attempts (
            id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            chat_id       BIGINT NOT NULL,
            succeeded     BOOLEAN NOT NULL,
            attempted_at  TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    op.execute("""
        COMMENT ON TABLE telegram_pairing_attempts IS
        'Contador de intentos de /emparejar por chat (contracts/telegram.md:
        "maximo 3 intentos por chat y hora"), mismo patron que login_attempts
        (0001_bootstrap) para el bloqueo 5/15 min de iam.'
    """)
    op.execute(
        "CREATE INDEX ix_telegram_pairing_attempts_chat_time "
        "ON telegram_pairing_attempts (chat_id, attempted_at DESC)"
    )

    op.execute("""
        CREATE TABLE telegram_test_messages (
            id                    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            owner_id              UUID NOT NULL REFERENCES owners (id) ON DELETE RESTRICT,
            chat_id               BIGINT NOT NULL,
            body                  TEXT NOT NULL,
            delivery_state        TEXT NOT NULL DEFAULT 'PENDING'
                                     CHECK (delivery_state IN ('PENDING', 'SENT', 'FAILED')),
            platform_message_id   BIGINT,
            created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
            sent_at               TIMESTAMPTZ
        )
    """)
    # Reclamo del drenaje del worker (mismo patron que ix_notifications_outbox).
    op.execute("""
        CREATE INDEX ix_telegram_test_messages_pending
        ON telegram_test_messages (created_at) WHERE delivery_state = 'PENDING'
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS telegram_test_messages")
    op.execute("DROP TABLE IF EXISTS telegram_pairing_attempts")
    op.execute("DROP TABLE IF EXISTS telegram_owner_chats")
