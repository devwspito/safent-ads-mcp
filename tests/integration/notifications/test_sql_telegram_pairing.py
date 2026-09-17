"""Adaptadores SQL del emparejamiento Telegram<->propietario
(`telegram_pairing_sql.py`, 0022_telegram_pairing) contra Postgres real:
cifrado/descifrado del codigo en claro, TTL efectivo, un solo codigo vivo
por propietario, confirmacion por codigo (lado del bot) y el contador de
intentos."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from safent_ads.iam.infrastructure.aesgcm_totp_cipher import AesGcmTotpCipher
from safent_ads.notifications.domain.pairing import PairingCode, PairingStatus
from safent_ads.notifications.infrastructure.telegram_pairing_sql import (
    SqlTelegramPairingAttempts,
    SqlTelegramPairingConfirmation,
    SqlTelegramPairingRepository,
    SqlTestMessageOutbox,
)
from tests.conftest import OwnerFactory

pytestmark = pytest.mark.integration

_VALID_32_BYTE_KEY_B64 = "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA="
_NOW = datetime(2026, 9, 9, 14, 0, tzinfo=UTC)
_EXPIRES_AT = _NOW + timedelta(minutes=10)
_CHAT_ID = 111222333


def _cipher() -> AesGcmTotpCipher:
    return AesGcmTotpCipher(_VALID_32_BYTE_KEY_B64)


class TestPairingRepository:
    async def test_unknown_owner_returns_none(self, db_session: AsyncSession) -> None:
        repo = SqlTelegramPairingRepository(db_session, cipher=_cipher())

        assert await repo.get(uuid.uuid4(), now=_NOW) is None

    async def test_start_pairing_stores_a_pending_row_with_decryptable_code(
        self, db_session: AsyncSession, owner_factory: OwnerFactory
    ) -> None:
        owner_id = await owner_factory.create()
        repo = SqlTelegramPairingRepository(db_session, cipher=_cipher())
        code = PairingCode.generate()

        await repo.start_pairing(owner_id=owner_id, code=code, expires_at=_EXPIRES_AT)

        snapshot = await repo.get(owner_id, now=_NOW)
        assert snapshot is not None
        assert snapshot.status is PairingStatus.PENDING
        assert snapshot.pairing_code == code.value
        assert snapshot.code_expires_at == _EXPIRES_AT

    async def test_expired_pending_code_reads_as_unpaired_and_hides_the_code(
        self, db_session: AsyncSession, owner_factory: OwnerFactory
    ) -> None:
        owner_id = await owner_factory.create()
        repo = SqlTelegramPairingRepository(db_session, cipher=_cipher())
        await repo.start_pairing(
            owner_id=owner_id, code=PairingCode.generate(), expires_at=_NOW - timedelta(seconds=1)
        )

        snapshot = await repo.get(owner_id, now=_NOW)

        assert snapshot is not None
        assert snapshot.status is PairingStatus.UNPAIRED
        assert snapshot.pairing_code is None

    async def test_starting_again_invalidates_the_previous_code(
        self, db_session: AsyncSession, owner_factory: OwnerFactory
    ) -> None:
        owner_id = await owner_factory.create()
        repo = SqlTelegramPairingRepository(db_session, cipher=_cipher())
        first_code = PairingCode.generate()
        await repo.start_pairing(owner_id=owner_id, code=first_code, expires_at=_EXPIRES_AT)

        second_code = PairingCode.generate()
        await repo.start_pairing(owner_id=owner_id, code=second_code, expires_at=_EXPIRES_AT)

        snapshot = await repo.get(owner_id, now=_NOW)
        assert snapshot is not None
        assert snapshot.pairing_code == second_code.value

        confirmation = SqlTelegramPairingConfirmation(db_session)
        candidates = await confirmation.list_pending(now=_NOW)
        matching = [c for c in candidates if c.owner_id == owner_id]
        assert len(matching) == 1
        assert matching[0].pairing_code_hash == second_code.hash()

    async def test_unpair_is_idempotent_when_no_row_exists(
        self, db_session: AsyncSession
    ) -> None:
        repo = SqlTelegramPairingRepository(db_session, cipher=_cipher())

        await repo.unpair(uuid.uuid4())  # no leva a error

    async def test_unpair_clears_a_paired_row(
        self, db_session: AsyncSession, owner_factory: OwnerFactory
    ) -> None:
        owner_id = await owner_factory.create()
        repo = SqlTelegramPairingRepository(db_session, cipher=_cipher())
        confirmation = SqlTelegramPairingConfirmation(db_session)
        code = PairingCode.generate()
        await repo.start_pairing(owner_id=owner_id, code=code, expires_at=_EXPIRES_AT)
        await confirmation.confirm(owner_id=owner_id, chat_id=_CHAT_ID, at=_NOW)

        await repo.unpair(owner_id)

        snapshot = await repo.get(owner_id, now=_NOW)
        assert snapshot is not None
        assert snapshot.status is PairingStatus.UNPAIRED
        assert snapshot.chat_id is None

    async def test_record_test_message_sent_updates_last_test_at(
        self, db_session: AsyncSession, owner_factory: OwnerFactory
    ) -> None:
        owner_id = await owner_factory.create()
        repo = SqlTelegramPairingRepository(db_session, cipher=_cipher())
        confirmation = SqlTelegramPairingConfirmation(db_session)
        await repo.start_pairing(
            owner_id=owner_id, code=PairingCode.generate(), expires_at=_EXPIRES_AT
        )
        await confirmation.confirm(owner_id=owner_id, chat_id=_CHAT_ID, at=_NOW)

        await repo.record_test_message_sent(owner_id, at=_NOW)

        snapshot = await repo.get(owner_id, now=_NOW)
        assert snapshot is not None
        assert snapshot.last_test_at == _NOW


class TestPairingConfirmation:
    async def test_list_pending_excludes_expired_codes(
        self, db_session: AsyncSession, owner_factory: OwnerFactory
    ) -> None:
        owner_id = await owner_factory.create()
        repo = SqlTelegramPairingRepository(db_session, cipher=_cipher())
        await repo.start_pairing(
            owner_id=owner_id, code=PairingCode.generate(), expires_at=_NOW - timedelta(seconds=1)
        )

        candidates = await SqlTelegramPairingConfirmation(db_session).list_pending(now=_NOW)

        assert all(c.owner_id != owner_id for c in candidates)

    async def test_confirm_sets_paired_with_chat_id(
        self, db_session: AsyncSession, owner_factory: OwnerFactory
    ) -> None:
        owner_id = await owner_factory.create()
        repo = SqlTelegramPairingRepository(db_session, cipher=_cipher())
        await repo.start_pairing(
            owner_id=owner_id, code=PairingCode.generate(), expires_at=_EXPIRES_AT
        )

        await SqlTelegramPairingConfirmation(db_session).confirm(
            owner_id=owner_id, chat_id=_CHAT_ID, at=_NOW
        )

        snapshot = await repo.get(owner_id, now=_NOW)
        assert snapshot is not None
        assert snapshot.status is PairingStatus.PAIRED
        assert snapshot.chat_id == _CHAT_ID
        assert snapshot.paired_at == _NOW


class TestPairingAttempts:
    async def test_count_recent_only_counts_within_the_window(
        self, db_session: AsyncSession
    ) -> None:
        attempts = SqlTelegramPairingAttempts(db_session)
        await attempts.record(_CHAT_ID, succeeded=False, at=_NOW)
        await attempts.record(_CHAT_ID, succeeded=False, at=_NOW - timedelta(hours=2))

        recent = await attempts.count_recent(_CHAT_ID, since=_NOW - timedelta(hours=1))

        assert recent == 1

    async def test_count_recent_is_scoped_to_the_chat(self, db_session: AsyncSession) -> None:
        attempts = SqlTelegramPairingAttempts(db_session)
        await attempts.record(_CHAT_ID, succeeded=False, at=_NOW)
        await attempts.record(999888777, succeeded=False, at=_NOW)

        recent = await attempts.count_recent(_CHAT_ID, since=_NOW - timedelta(hours=1))

        assert recent == 1


class TestTestMessageOutbox:
    async def test_enqueue_and_claim_pending(
        self, db_session: AsyncSession, owner_factory: OwnerFactory
    ) -> None:
        owner_id = await owner_factory.create()
        outbox = SqlTestMessageOutbox(db_session)
        notification_id = uuid.uuid4()

        await outbox.enqueue(
            notification_id=notification_id,
            owner_id=owner_id,
            chat_id=_CHAT_ID,
            body="hola",
            at=_NOW,
        )
        await db_session.flush()

        pending = await outbox.claim_pending(limit=10)
        assert any(task.notification_id == notification_id for task in pending)

    async def test_mark_sent_removes_it_from_pending(
        self, db_session: AsyncSession, owner_factory: OwnerFactory
    ) -> None:
        owner_id = await owner_factory.create()
        outbox = SqlTestMessageOutbox(db_session)
        notification_id = uuid.uuid4()
        await outbox.enqueue(
            notification_id=notification_id,
            owner_id=owner_id,
            chat_id=_CHAT_ID,
            body="hola",
            at=_NOW,
        )
        await db_session.flush()

        await outbox.mark_sent(notification_id, platform_message_id=42)
        await db_session.flush()

        pending = await outbox.claim_pending(limit=10)
        assert all(task.notification_id != notification_id for task in pending)

    async def test_mark_failed_removes_it_from_pending(
        self, db_session: AsyncSession, owner_factory: OwnerFactory
    ) -> None:
        owner_id = await owner_factory.create()
        outbox = SqlTestMessageOutbox(db_session)
        notification_id = uuid.uuid4()
        await outbox.enqueue(
            notification_id=notification_id,
            owner_id=owner_id,
            chat_id=_CHAT_ID,
            body="hola",
            at=_NOW,
        )
        await db_session.flush()

        await outbox.mark_failed(notification_id)
        await db_session.flush()

        pending = await outbox.claim_pending(limit=10)
        assert all(task.notification_id != notification_id for task in pending)

    async def test_count_recent_is_scoped_to_the_owner_and_window(
        self, db_session: AsyncSession, owner_factory: OwnerFactory
    ) -> None:
        owner_id = await owner_factory.create()
        other_owner_id = await owner_factory.create()
        outbox = SqlTestMessageOutbox(db_session)
        await outbox.enqueue(
            notification_id=uuid.uuid4(), owner_id=owner_id, chat_id=_CHAT_ID, body="hola", at=_NOW
        )
        await outbox.enqueue(
            notification_id=uuid.uuid4(),
            owner_id=owner_id,
            chat_id=_CHAT_ID,
            body="hola",
            at=_NOW - timedelta(minutes=20),
        )
        await outbox.enqueue(
            notification_id=uuid.uuid4(),
            owner_id=other_owner_id,
            chat_id=999888777,
            body="hola",
            at=_NOW,
        )
        await db_session.flush()

        recent = await outbox.count_recent(owner_id, since=_NOW - timedelta(minutes=10))

        assert recent == 1
