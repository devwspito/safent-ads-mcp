"""0010_notifications: `notifications` (entrega idempotente por
`dedupe_key`) y `telegram_callbacks` (nonce unico, de un solo uso, con
caducidad).

data-model.md §Notification: "entrega idempotente por dedupe_key (NFR-6);
CallbackNonce ligado a (proposal_id, chat_id, message_id, diff_hash),
caduca y no se reutiliza". contracts/telegram.md §callback_data.

Revision ID: 0010_notifications
Revises: 0009_executions
Create Date: 2026-09-09
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0010_notifications"
down_revision: str | None = "0009_executions"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE notifications (
            id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            business_id     UUID NOT NULL REFERENCES businesses (id) ON DELETE RESTRICT,
            channel         TEXT NOT NULL CHECK (channel IN ('telegram', 'panel')),
            severity        TEXT NOT NULL CHECK (severity IN ('DIGEST', 'NORMAL', 'CRITICAL')),
            kind            TEXT NOT NULL CHECK (kind IN
                                ('ticker', 'digest', 'critical', 'auto_receipt',
                                 'approval_request')),
            payload         JSONB NOT NULL,
            dedupe_key      TEXT NOT NULL CHECK (char_length(dedupe_key) BETWEEN 1 AND 200),
            delivery_state  TEXT NOT NULL DEFAULT 'PENDING' CHECK (delivery_state IN
                                ('PENDING', 'SENT', 'FAILED', 'SUPPRESSED')),
            attempt_count   SMALLINT NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
            chat_id         BIGINT,
            message_id      BIGINT,
            last_error      TEXT,
            sent_at         TIMESTAMPTZ,
            created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),

            -- NFR-6: un reintento de envio no duplica el mensaje. La unicidad
            -- vive en la base, no en la memoria del proceso que reintenta.
            CONSTRAINT notifications_dedupe_key_unique UNIQUE (dedupe_key),
            CONSTRAINT notifications_sent_is_traceable_check CHECK (
                delivery_state <> 'SENT'
                OR (sent_at IS NOT NULL AND message_id IS NOT NULL)
            )
        )
    """)
    op.execute("""
        COMMENT ON COLUMN notifications.payload IS
        'Texto ya compuesto y datos de presentacion. Sin PII ni identificadores '
        'externos completos (contracts/telegram.md).'
    """)
    op.execute("""
        CREATE TRIGGER notifications_set_updated_at
        BEFORE UPDATE ON notifications FOR EACH ROW EXECUTE FUNCTION set_updated_at()
    """)
    # Cola de salida: reclamo con FOR UPDATE SKIP LOCKED de lo no entregado.
    op.execute("""
        CREATE INDEX ix_notifications_outbox
        ON notifications (created_at)
        WHERE delivery_state IN ('PENDING', 'FAILED')
    """)
    # Panel: historial de avisos de un negocio.
    op.execute("""
        CREATE INDEX ix_notifications_business_time
        ON notifications (business_id, created_at DESC)
    """)

    op.execute("""
        CREATE TABLE telegram_callbacks (
            id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            nonce            TEXT NOT NULL CHECK (nonce ~ '^[A-Za-z0-9]{10}$'),
            proposal_id      UUID NOT NULL REFERENCES proposals (id) ON DELETE CASCADE,
            notification_id  UUID REFERENCES notifications (id) ON DELETE SET NULL,
            chat_id          BIGINT NOT NULL,
            message_id       BIGINT NOT NULL,
            diff_hash        TEXT NOT NULL CHECK (diff_hash ~ '^[a-f0-9]{64}$'),
            action           TEXT NOT NULL CHECK (action IN ('a', 'c', 'r', 's', 'e', 'u')),
            is_single_use    BOOLEAN NOT NULL DEFAULT true,
            consumed_at      TIMESTAMPTZ,
            expires_at       TIMESTAMPTZ NOT NULL,
            created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),

            CONSTRAINT telegram_callbacks_nonce_unique UNIQUE (nonce),
            CONSTRAINT telegram_callbacks_ttl_check CHECK (expires_at > created_at)
        )
    """)
    op.execute("""
        COMMENT ON COLUMN telegram_callbacks.is_single_use IS
        'true = el toque lo consume. Solo el boton de detalle (accion e) se reutiliza.'
    """)
    # Un solo uso de verdad: aunque la aplicacion olvide el
    # `WHERE consumed_at IS NULL`, el segundo consumo no pasa de aqui.
    op.execute("""
        CREATE FUNCTION telegram_callbacks_single_use() RETURNS TRIGGER AS $$
        BEGIN
            IF OLD.is_single_use
               AND OLD.consumed_at IS NOT NULL
               AND NEW.consumed_at IS DISTINCT FROM OLD.consumed_at THEN
                RAISE EXCEPTION 'telegram_callbacks: el nonce % ya fue consumido', OLD.nonce;
            END IF;
            IF NEW.nonce <> OLD.nonce THEN
                RAISE EXCEPTION 'telegram_callbacks: el nonce no se reescribe';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
    """)
    op.execute("""
        CREATE TRIGGER telegram_callbacks_single_use
        BEFORE UPDATE ON telegram_callbacks
        FOR EACH ROW EXECUTE FUNCTION telegram_callbacks_single_use()
    """)

    # Purga de caducados (contracts/telegram.md: TTL <= 6 h).
    op.execute("CREATE INDEX ix_telegram_callbacks_expires ON telegram_callbacks (expires_at)")
    # Reemision de la tarjeta cuando el diff_hash vivo cambio (INV-1).
    op.execute("""
        CREATE INDEX ix_telegram_callbacks_proposal
        ON telegram_callbacks (proposal_id, created_at DESC)
    """)


def downgrade() -> None:
    op.execute("DROP TABLE telegram_callbacks")
    op.execute("DROP FUNCTION IF EXISTS telegram_callbacks_single_use()")
    op.execute("DROP TABLE notifications")
