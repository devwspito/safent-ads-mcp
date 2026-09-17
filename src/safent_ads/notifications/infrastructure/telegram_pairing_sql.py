"""Adaptadores SQL del emparejamiento Telegram<->propietario
(`telegram_owner_chats`, `telegram_pairing_attempts`,
`telegram_test_messages`, 0022_telegram_pairing). Separado de
`sql_repositories.py` (ticker/digest/callback-store): esta lane tiene su
propia clave de cifrado (`AesGcmTotpCipher`, reusada de `iam` -- ver
docstring de la migracion) y no comparte ninguna tabla con esas."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from safent_ads.iam.infrastructure.aesgcm_totp_cipher import (
    PURPOSE_TELEGRAM_PAIRING,
    AesGcmTotpCipher,
)
from safent_ads.notifications.application.ports import (
    PairingSnapshot,
    PendingPairingCandidate,
    PendingTestMessage,
)
from safent_ads.notifications.domain.pairing import PairingCode, PairingStatus, effective_status

_UPSERT_START_PAIRING = text("""
    INSERT INTO telegram_owner_chats
        (owner_id, status, chat_id, pairing_code_hash, pairing_code_encrypted,
         code_expires_at, verified_at)
    VALUES (:owner_id, 'pending', NULL, :code_hash, :code_encrypted, :expires_at, NULL)
    ON CONFLICT (owner_id) DO UPDATE SET
        status = 'pending', chat_id = NULL, pairing_code_hash = EXCLUDED.pairing_code_hash,
        pairing_code_encrypted = EXCLUDED.pairing_code_encrypted,
        code_expires_at = EXCLUDED.code_expires_at, verified_at = NULL
""")

_SELECT_BY_OWNER = text("""
    SELECT status, chat_id, pairing_code_encrypted, code_expires_at, verified_at, last_test_at
      FROM telegram_owner_chats WHERE owner_id = :owner_id
""")

_UPDATE_UNPAIR = text("""
    UPDATE telegram_owner_chats
       SET status = 'unpaired', chat_id = NULL, pairing_code_hash = NULL,
           pairing_code_encrypted = NULL, code_expires_at = NULL, verified_at = NULL
     WHERE owner_id = :owner_id
""")

_UPDATE_LAST_TEST_AT = text(
    "UPDATE telegram_owner_chats SET last_test_at = :at "
    "WHERE owner_id = :owner_id AND status = 'paired'"
)

_SELECT_PENDING_CANDIDATES = text("""
    SELECT owner_id, pairing_code_hash FROM telegram_owner_chats
     WHERE status = 'pending' AND pairing_code_hash IS NOT NULL AND code_expires_at > :now
""")

_UPDATE_CONFIRM_PAIRING = text("""
    UPDATE telegram_owner_chats
       SET status = 'paired', chat_id = :chat_id, verified_at = :at,
           pairing_code_hash = NULL, pairing_code_encrypted = NULL, code_expires_at = NULL
     WHERE owner_id = :owner_id
""")

_COUNT_RECENT_ATTEMPTS = text(
    "SELECT COUNT(*) FROM telegram_pairing_attempts WHERE chat_id = :chat_id "
    "AND attempted_at >= :since"
)

_INSERT_ATTEMPT = text(
    "INSERT INTO telegram_pairing_attempts (chat_id, succeeded, attempted_at) "
    "VALUES (:chat_id, :succeeded, :at)"
)

_INSERT_TEST_MESSAGE = text(
    "INSERT INTO telegram_test_messages (id, owner_id, chat_id, body, created_at) "
    "VALUES (:id, :owner_id, :chat_id, :body, :at)"
)

_COUNT_RECENT_TEST_MESSAGES = text(
    "SELECT COUNT(*) FROM telegram_test_messages WHERE owner_id = :owner_id "
    "AND created_at >= :since"
)

_CLAIM_PENDING_TEST_MESSAGES = text("""
    SELECT id, chat_id, body FROM telegram_test_messages
     WHERE delivery_state = 'PENDING' ORDER BY created_at LIMIT :limit
""")

_MARK_TEST_MESSAGE_SENT = text(
    "UPDATE telegram_test_messages SET delivery_state = 'SENT', "
    "platform_message_id = :platform_message_id, sent_at = now() WHERE id = :id"
)

_MARK_TEST_MESSAGE_FAILED = text(
    "UPDATE telegram_test_messages SET delivery_state = 'FAILED' WHERE id = :id"
)


class SqlTelegramPairingRepository:
    """`TelegramPairingRepositoryPort`: lado del PROPIETARIO (panel),
    siempre resuelto por `owner_id`. `pairing_code_encrypted` viaja
    AES-256-GCM con la misma clave que ya cifra el secreto TOTP
    (`ADS_TOTP_ENC_KEY`) -- ver 0022_telegram_pairing para el porque."""

    def __init__(self, session: AsyncSession, *, cipher: AesGcmTotpCipher) -> None:
        self._session = session
        self._cipher = cipher

    async def get(self, owner_id: uuid.UUID, *, now: datetime) -> PairingSnapshot | None:
        row = (
            await self._session.execute(_SELECT_BY_OWNER, {"owner_id": owner_id})
        ).mappings().one_or_none()
        if row is None:
            return None
        status = effective_status(
            PairingStatus(row["status"]), code_expires_at=row["code_expires_at"], now=now
        )
        return PairingSnapshot(
            status=status,
            chat_id=row["chat_id"] if status is PairingStatus.PAIRED else None,
            paired_at=row["verified_at"] if status is PairingStatus.PAIRED else None,
            code_expires_at=row["code_expires_at"] if status is PairingStatus.PENDING else None,
            pairing_code=self._decrypt_if_pending(status, row["pairing_code_encrypted"]),
            last_test_at=row["last_test_at"],
        )

    def _decrypt_if_pending(self, status: PairingStatus, encrypted: bytes | None) -> str | None:
        if status is not PairingStatus.PENDING or encrypted is None:
            return None
        return self._cipher.decrypt(encrypted, purpose=PURPOSE_TELEGRAM_PAIRING)

    async def start_pairing(
        self, *, owner_id: uuid.UUID, code: PairingCode, expires_at: datetime
    ) -> None:
        await self._session.execute(
            _UPSERT_START_PAIRING,
            {
                "owner_id": owner_id,
                "code_hash": code.hash(),
                "code_encrypted": self._cipher.encrypt(
                    code.value, purpose=PURPOSE_TELEGRAM_PAIRING
                ),
                "expires_at": expires_at,
            },
        )

    async def unpair(self, owner_id: uuid.UUID) -> None:
        await self._session.execute(_UPDATE_UNPAIR, {"owner_id": owner_id})

    async def record_test_message_sent(self, owner_id: uuid.UUID, *, at: datetime) -> None:
        await self._session.execute(_UPDATE_LAST_TEST_AT, {"owner_id": owner_id, "at": at})


class SqlTelegramPairingConfirmation:
    """`TelegramPairingConfirmationPort`: lado del BOT, resuelto por
    CODIGO (no se conoce `owner_id` hasta que el codigo hace match)."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_pending(self, *, now: datetime) -> list[PendingPairingCandidate]:
        rows = (
            await self._session.execute(_SELECT_PENDING_CANDIDATES, {"now": now})
        ).mappings()
        return [
            PendingPairingCandidate(
                owner_id=row["owner_id"], pairing_code_hash=row["pairing_code_hash"]
            )
            for row in rows
        ]

    async def confirm(self, *, owner_id: uuid.UUID, chat_id: int, at: datetime) -> None:
        await self._session.execute(
            _UPDATE_CONFIRM_PAIRING, {"owner_id": owner_id, "chat_id": chat_id, "at": at}
        )


class SqlTelegramPairingAttempts:
    """`TelegramPairingAttemptsPort` sobre `telegram_pairing_attempts`
    (mismo patron que `SqlLoginAttemptRepository` para `login_attempts`)."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def count_recent(self, chat_id: int, *, since: datetime) -> int:
        result = await self._session.execute(
            _COUNT_RECENT_ATTEMPTS, {"chat_id": chat_id, "since": since}
        )
        return int(result.scalar_one())

    async def record(self, chat_id: int, *, succeeded: bool, at: datetime) -> None:
        await self._session.execute(
            _INSERT_ATTEMPT, {"chat_id": chat_id, "succeeded": succeeded, "at": at}
        )


class SqlTestMessageOutbox:
    """`TestMessageOutboxPort` sobre `telegram_test_messages`: `ads-api`
    encola (`enqueue`), `ads-worker` drena (`claim_pending`/`mark_*`)."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def enqueue(
        self,
        *,
        notification_id: uuid.UUID,
        owner_id: uuid.UUID,
        chat_id: int,
        body: str,
        at: datetime,
    ) -> None:
        await self._session.execute(
            _INSERT_TEST_MESSAGE,
            {
                "id": notification_id,
                "owner_id": owner_id,
                "chat_id": chat_id,
                "body": body,
                "at": at,
            },
        )

    async def count_recent(self, owner_id: uuid.UUID, *, since: datetime) -> int:
        result = await self._session.execute(
            _COUNT_RECENT_TEST_MESSAGES, {"owner_id": owner_id, "since": since}
        )
        return int(result.scalar_one())

    async def claim_pending(self, *, limit: int) -> list[PendingTestMessage]:
        rows = (
            await self._session.execute(_CLAIM_PENDING_TEST_MESSAGES, {"limit": limit})
        ).mappings()
        return [
            PendingTestMessage(notification_id=row["id"], chat_id=row["chat_id"], body=row["body"])
            for row in rows
        ]

    async def mark_sent(self, notification_id: uuid.UUID, *, platform_message_id: int) -> None:
        await self._session.execute(
            _MARK_TEST_MESSAGE_SENT,
            {"id": notification_id, "platform_message_id": platform_message_id},
        )

    async def mark_failed(self, notification_id: uuid.UUID) -> None:
        await self._session.execute(_MARK_TEST_MESSAGE_FAILED, {"id": notification_id})
