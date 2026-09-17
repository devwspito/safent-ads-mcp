"""Casos de uso del panel para `/telegram/pairing*` (FR-25,
rest-api.md §Conexiones, Telegram y ajustes): emitir codigo, consultar
estado, desemparejar y encolar el mensaje de prueba."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from safent_ads.notifications.application.errors import (
    TelegramAllowlistEmptyError,
    TelegramNotPairedError,
    TelegramTestMessageRateLimitedError,
)
from safent_ads.notifications.application.telegram_pairing import (
    GetTelegramPairingStatus,
    SendTelegramTestMessage,
    StartTelegramPairing,
    UnpairTelegram,
)
from safent_ads.notifications.domain.pairing import (
    MAX_TEST_MESSAGES_PER_WINDOW,
    PAIRING_CODE_TTL,
    PairingStatus,
)
from safent_ads.notifications.testing.fakes import (
    FakeTelegramPairingRepository,
    FakeTestMessageOutbox,
)
from safent_ads.shared.clock import FixedClock
from safent_ads.shared.ids import UuidIdGenerator

_NOW = datetime(2026, 9, 9, 14, 0, tzinfo=UTC)
_OWNER_ID = uuid.uuid4()
_CHAT_ID = 111222333


class TestGetTelegramPairingStatus:
    async def test_no_row_yet_reports_unpaired(self) -> None:
        use_case = GetTelegramPairingStatus(
            pairing=FakeTelegramPairingRepository(), clock=FixedClock(_NOW)
        )

        snapshot = await use_case.execute(_OWNER_ID)

        assert snapshot.status is PairingStatus.UNPAIRED
        assert snapshot.chat_id is None
        assert snapshot.pairing_code is None

    async def test_reports_the_paired_snapshot(self) -> None:
        pairing = FakeTelegramPairingRepository()
        pairing.seed_paired(_OWNER_ID, chat_id=_CHAT_ID, verified_at=_NOW)
        use_case = GetTelegramPairingStatus(pairing=pairing, clock=FixedClock(_NOW))

        snapshot = await use_case.execute(_OWNER_ID)

        assert snapshot.status is PairingStatus.PAIRED
        assert snapshot.chat_id == _CHAT_ID
        assert snapshot.paired_at == _NOW


class TestStartTelegramPairing:
    async def test_allowlist_empty_refuses_to_issue_a_code(self) -> None:
        use_case = StartTelegramPairing(
            pairing=FakeTelegramPairingRepository(),
            clock=FixedClock(_NOW),
            allowlist_configured=False,
        )

        with pytest.raises(TelegramAllowlistEmptyError):
            await use_case.execute(_OWNER_ID)

    async def test_issues_a_code_with_a_ten_minute_ttl(self) -> None:
        pairing = FakeTelegramPairingRepository()
        use_case = StartTelegramPairing(
            pairing=pairing, clock=FixedClock(_NOW), allowlist_configured=True
        )

        started = await use_case.execute(_OWNER_ID)

        assert len(started.pairing_code) == 8
        assert started.code_expires_at == _NOW + PAIRING_CODE_TTL

    async def test_leaves_the_pairing_pending_until_confirmed(self) -> None:
        pairing = FakeTelegramPairingRepository()
        use_case = StartTelegramPairing(
            pairing=pairing, clock=FixedClock(_NOW), allowlist_configured=True
        )

        await use_case.execute(_OWNER_ID)

        snapshot = await pairing.get(_OWNER_ID, now=_NOW)
        assert snapshot is not None
        assert snapshot.status is PairingStatus.PENDING


class TestUnpairTelegram:
    async def test_paired_owner_goes_back_to_unpaired(self) -> None:
        pairing = FakeTelegramPairingRepository()
        pairing.seed_paired(_OWNER_ID, chat_id=_CHAT_ID, verified_at=_NOW)
        use_case = UnpairTelegram(pairing=pairing)

        await use_case.execute(_OWNER_ID)

        snapshot = await pairing.get(_OWNER_ID, now=_NOW)
        assert snapshot is not None
        assert snapshot.status is PairingStatus.UNPAIRED

    async def test_is_idempotent_when_never_paired(self) -> None:
        use_case = UnpairTelegram(pairing=FakeTelegramPairingRepository())

        await use_case.execute(_OWNER_ID)  # no leva a error


class TestSendTelegramTestMessage:
    async def test_not_paired_raises(self) -> None:
        use_case = SendTelegramTestMessage(
            pairing=FakeTelegramPairingRepository(),
            outbox=FakeTestMessageOutbox(),
            id_generator=UuidIdGenerator(),
            clock=FixedClock(_NOW),
        )

        with pytest.raises(TelegramNotPairedError):
            await use_case.execute(_OWNER_ID)

    async def test_pending_is_not_paired_either(self) -> None:
        pairing = FakeTelegramPairingRepository()
        await StartTelegramPairing(
            pairing=pairing, clock=FixedClock(_NOW), allowlist_configured=True
        ).execute(_OWNER_ID)
        use_case = SendTelegramTestMessage(
            pairing=pairing,
            outbox=FakeTestMessageOutbox(),
            id_generator=UuidIdGenerator(),
            clock=FixedClock(_NOW),
        )

        with pytest.raises(TelegramNotPairedError):
            await use_case.execute(_OWNER_ID)

    async def test_paired_owner_enqueues_a_message_for_their_chat(self) -> None:
        pairing = FakeTelegramPairingRepository()
        pairing.seed_paired(_OWNER_ID, chat_id=_CHAT_ID, verified_at=_NOW)
        outbox = FakeTestMessageOutbox()
        use_case = SendTelegramTestMessage(
            pairing=pairing, outbox=outbox, id_generator=UuidIdGenerator(), clock=FixedClock(_NOW)
        )

        notification_id = await use_case.execute(_OWNER_ID)

        pending = await outbox.claim_pending(limit=10)
        assert len(pending) == 1
        assert pending[0].notification_id == notification_id
        assert pending[0].chat_id == _CHAT_ID

    async def test_records_the_last_test_at_timestamp(self) -> None:
        pairing = FakeTelegramPairingRepository()
        pairing.seed_paired(_OWNER_ID, chat_id=_CHAT_ID, verified_at=_NOW)
        later = _NOW + timedelta(minutes=5)
        use_case = SendTelegramTestMessage(
            pairing=pairing,
            outbox=FakeTestMessageOutbox(),
            id_generator=UuidIdGenerator(),
            clock=FixedClock(later),
        )

        await use_case.execute(_OWNER_ID)

        snapshot = await pairing.get(_OWNER_ID, now=later)
        assert snapshot is not None
        assert snapshot.last_test_at == later

    async def test_fourth_message_within_ten_minutes_is_rate_limited(self) -> None:
        """security-review-f4.md item 1: sin freno, `POST
        /telegram/pairing/test-message` podia repetirse sin limite."""
        pairing = FakeTelegramPairingRepository()
        pairing.seed_paired(_OWNER_ID, chat_id=_CHAT_ID, verified_at=_NOW)
        outbox = FakeTestMessageOutbox()
        use_case = SendTelegramTestMessage(
            pairing=pairing, outbox=outbox, id_generator=UuidIdGenerator(), clock=FixedClock(_NOW)
        )
        for _ in range(MAX_TEST_MESSAGES_PER_WINDOW):
            await use_case.execute(_OWNER_ID)

        with pytest.raises(TelegramTestMessageRateLimitedError):
            await use_case.execute(_OWNER_ID)

        pending = await outbox.claim_pending(limit=10)
        assert len(pending) == MAX_TEST_MESSAGES_PER_WINDOW

    async def test_a_message_outside_the_window_does_not_count(self) -> None:
        pairing = FakeTelegramPairingRepository()
        pairing.seed_paired(_OWNER_ID, chat_id=_CHAT_ID, verified_at=_NOW)
        outbox = FakeTestMessageOutbox()
        clock = FixedClock(_NOW)
        use_case = SendTelegramTestMessage(
            pairing=pairing, outbox=outbox, id_generator=UuidIdGenerator(), clock=clock
        )
        for _ in range(MAX_TEST_MESSAGES_PER_WINDOW):
            await use_case.execute(_OWNER_ID)
        clock.advance_to(_NOW + timedelta(minutes=11))

        await use_case.execute(_OWNER_ID)  # no leva a error: la ventana ya expiro
