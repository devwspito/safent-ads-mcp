"""`ConfirmTelegramPairing` (`/emparejar <codigo>`, contracts/telegram.md
§Emparejamiento): un solo codigo vivo, un solo uso via `confirm`, maximo 3
intentos por chat y hora, comparacion en tiempo constante contra CADA
candidata viva. La allow-list ya la verifico el adaptador del bot antes de
llegar aqui -- este caso de uso no la conoce."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from safent_ads.notifications.application.confirm_telegram_pairing import (
    ConfirmTelegramPairing,
    ConfirmTelegramPairingCommand,
)
from safent_ads.notifications.application.ports import (
    PendingPairingCandidate,
    TelegramPairingConfirmationOutcome,
)
from safent_ads.notifications.domain.pairing import MAX_PAIRING_ATTEMPTS_PER_HOUR, PairingCode
from safent_ads.notifications.testing.fakes import (
    FakeTelegramPairingAttempts,
    FakeTelegramPairingConfirmation,
)
from safent_ads.shared.clock import FixedClock

_NOW = datetime(2026, 9, 9, 14, 0, tzinfo=UTC)
_CHAT_ID = 111222333
_OWNER_ID = uuid.uuid4()
_CODE = PairingCode("ABCD2345")


def _build(
    *, candidates: list[PendingPairingCandidate] | None = None
) -> tuple[ConfirmTelegramPairing, FakeTelegramPairingConfirmation, FakeTelegramPairingAttempts]:
    pairing = FakeTelegramPairingConfirmation()
    pairing.candidates = candidates or []
    attempts = FakeTelegramPairingAttempts()
    use_case = ConfirmTelegramPairing(pairing=pairing, attempts=attempts, clock=FixedClock(_NOW))
    return use_case, pairing, attempts


class TestSuccessfulMatch:
    async def test_matching_code_confirms_the_owner(self) -> None:
        candidate = PendingPairingCandidate(owner_id=_OWNER_ID, pairing_code_hash=_CODE.hash())
        use_case, pairing, _attempts = _build(candidates=[candidate])

        outcome = await use_case.execute(
            ConfirmTelegramPairingCommand(chat_id=_CHAT_ID, code_text=_CODE.value)
        )

        assert outcome is TelegramPairingConfirmationOutcome.CONFIRMED
        assert pairing.confirmed == [(_OWNER_ID, _CHAT_ID)]

    async def test_successful_match_records_a_successful_attempt(self) -> None:
        candidate = PendingPairingCandidate(owner_id=_OWNER_ID, pairing_code_hash=_CODE.hash())
        use_case, _pairing, attempts = _build(candidates=[candidate])

        await use_case.execute(
            ConfirmTelegramPairingCommand(chat_id=_CHAT_ID, code_text=_CODE.value)
        )

        assert await attempts.count_recent(_CHAT_ID, since=_NOW - timedelta(hours=1)) == 1

    async def test_picks_the_right_candidate_among_several(self) -> None:
        other_owner = uuid.uuid4()
        other_hash = PairingCode("WXYZ6789").hash()
        candidates = [
            PendingPairingCandidate(owner_id=other_owner, pairing_code_hash=other_hash),
            PendingPairingCandidate(owner_id=_OWNER_ID, pairing_code_hash=_CODE.hash()),
        ]
        use_case, pairing, _attempts = _build(candidates=candidates)

        outcome = await use_case.execute(
            ConfirmTelegramPairingCommand(chat_id=_CHAT_ID, code_text=_CODE.value)
        )

        assert outcome is TelegramPairingConfirmationOutcome.CONFIRMED
        assert pairing.confirmed == [(_OWNER_ID, _CHAT_ID)]


class TestNoMatch:
    async def test_no_pending_candidates_is_no_match(self) -> None:
        use_case, pairing, _attempts = _build(candidates=[])

        outcome = await use_case.execute(
            ConfirmTelegramPairingCommand(chat_id=_CHAT_ID, code_text=_CODE.value)
        )

        assert outcome is TelegramPairingConfirmationOutcome.NO_MATCH
        assert pairing.confirmed == []

    async def test_wrong_code_against_a_live_candidate_is_no_match(self) -> None:
        candidate = PendingPairingCandidate(owner_id=_OWNER_ID, pairing_code_hash=_CODE.hash())
        use_case, pairing, _attempts = _build(candidates=[candidate])

        outcome = await use_case.execute(
            ConfirmTelegramPairingCommand(chat_id=_CHAT_ID, code_text="WXYZ6789")
        )

        assert outcome is TelegramPairingConfirmationOutcome.NO_MATCH
        assert pairing.confirmed == []

    async def test_no_match_still_records_the_failed_attempt(self) -> None:
        use_case, _pairing, attempts = _build(candidates=[])

        await use_case.execute(
            ConfirmTelegramPairingCommand(chat_id=_CHAT_ID, code_text=_CODE.value)
        )

        assert await attempts.count_recent(_CHAT_ID, since=_NOW - timedelta(hours=1)) == 1


class TestInvalidCode:
    async def test_malformed_code_never_reaches_the_candidates(self) -> None:
        candidate = PendingPairingCandidate(owner_id=_OWNER_ID, pairing_code_hash=_CODE.hash())
        use_case, pairing, attempts = _build(candidates=[candidate])

        outcome = await use_case.execute(
            ConfirmTelegramPairingCommand(chat_id=_CHAT_ID, code_text="not-a-code")
        )

        assert outcome is TelegramPairingConfirmationOutcome.INVALID_CODE
        assert pairing.confirmed == []
        assert await attempts.count_recent(_CHAT_ID, since=_NOW - timedelta(hours=1)) == 1


class TestRateLimit:
    async def test_third_attempt_still_allowed(self) -> None:
        use_case, _pairing, attempts = _build(candidates=[])
        for _ in range(MAX_PAIRING_ATTEMPTS_PER_HOUR - 1):
            await attempts.record(_CHAT_ID, succeeded=False, at=_NOW)

        outcome = await use_case.execute(
            ConfirmTelegramPairingCommand(chat_id=_CHAT_ID, code_text=_CODE.value)
        )

        assert outcome is not TelegramPairingConfirmationOutcome.RATE_LIMITED

    async def test_fourth_attempt_within_the_hour_is_rate_limited(self) -> None:
        candidate = PendingPairingCandidate(owner_id=_OWNER_ID, pairing_code_hash=_CODE.hash())
        use_case, pairing, attempts = _build(candidates=[candidate])
        for _ in range(MAX_PAIRING_ATTEMPTS_PER_HOUR):
            await attempts.record(_CHAT_ID, succeeded=False, at=_NOW)

        outcome = await use_case.execute(
            ConfirmTelegramPairingCommand(chat_id=_CHAT_ID, code_text=_CODE.value)
        )

        assert outcome is TelegramPairingConfirmationOutcome.RATE_LIMITED
        assert pairing.confirmed == []  # ni con el codigo correcto

    async def test_rate_limit_is_per_chat_not_global(self) -> None:
        other_chat_id = 999888777
        candidate = PendingPairingCandidate(owner_id=_OWNER_ID, pairing_code_hash=_CODE.hash())
        use_case, _pairing, attempts = _build(candidates=[candidate])
        for _ in range(MAX_PAIRING_ATTEMPTS_PER_HOUR):
            await attempts.record(_CHAT_ID, succeeded=False, at=_NOW)

        outcome = await use_case.execute(
            ConfirmTelegramPairingCommand(chat_id=other_chat_id, code_text=_CODE.value)
        )

        assert outcome is TelegramPairingConfirmationOutcome.CONFIRMED

    async def test_attempts_older_than_an_hour_do_not_count(self) -> None:
        use_case, _pairing, attempts = _build(candidates=[])
        for _ in range(MAX_PAIRING_ATTEMPTS_PER_HOUR):
            await attempts.record(_CHAT_ID, succeeded=False, at=_NOW - timedelta(hours=2))

        outcome = await use_case.execute(
            ConfirmTelegramPairingCommand(chat_id=_CHAT_ID, code_text=_CODE.value)
        )

        assert outcome is not TelegramPairingConfirmationOutcome.RATE_LIMITED
