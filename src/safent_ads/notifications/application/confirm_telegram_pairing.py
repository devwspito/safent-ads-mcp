"""`ConfirmTelegramPairing`: resuelve `/emparejar <codigo>`
(contracts/telegram.md §Emparejamiento). El adaptador del bot
(`aiogram_messenger.py`) ya verifico que `chat_id` esta en la allow-list
ANTES de llegar aqui -- este caso de uso nunca conoce `TELEGRAM_OWNER_CHAT_IDS`
(C-4: el emparejamiento no la amplia, solo actua dentro de ella)."""

from __future__ import annotations

import hmac
import uuid
from dataclasses import dataclass

from safent_ads.notifications.application.ports import (
    PendingPairingCandidate,
    TelegramPairingAttemptsPort,
    TelegramPairingConfirmationOutcome,
    TelegramPairingConfirmationPort,
)
from safent_ads.notifications.domain.pairing import (
    MAX_PAIRING_ATTEMPTS_PER_HOUR,
    PAIRING_ATTEMPTS_WINDOW,
    InvalidPairingCodeError,
    PairingCode,
)
from safent_ads.shared.clock import Clock

Outcome = TelegramPairingConfirmationOutcome


@dataclass(frozen=True, slots=True)
class ConfirmTelegramPairingCommand:
    chat_id: int
    code_text: str


class ConfirmTelegramPairing:
    def __init__(
        self,
        *,
        pairing: TelegramPairingConfirmationPort,
        attempts: TelegramPairingAttemptsPort,
        clock: Clock,
    ) -> None:
        self._pairing = pairing
        self._attempts = attempts
        self._clock = clock

    async def execute(self, command: ConfirmTelegramPairingCommand) -> Outcome:
        now = self._clock.now()
        since = now - PAIRING_ATTEMPTS_WINDOW
        recent = await self._attempts.count_recent(command.chat_id, since=since)
        if recent >= MAX_PAIRING_ATTEMPTS_PER_HOUR:
            return Outcome.RATE_LIMITED

        try:
            code = PairingCode(command.code_text)
        except InvalidPairingCodeError:
            await self._attempts.record(command.chat_id, succeeded=False, at=now)
            return Outcome.INVALID_CODE

        candidates = await self._pairing.list_pending(now=now)
        match = _find_matching_owner(candidates, code)
        if match is None:
            await self._attempts.record(command.chat_id, succeeded=False, at=now)
            return Outcome.NO_MATCH

        await self._pairing.confirm(owner_id=match, chat_id=command.chat_id, at=now)
        await self._attempts.record(command.chat_id, succeeded=True, at=now)
        return Outcome.CONFIRMED


def _find_matching_owner(
    candidates: list[PendingPairingCandidate], code: PairingCode
) -> uuid.UUID | None:
    """Compara CADA candidata en tiempo constante (`hmac.compare_digest`,
    contracts/telegram.md) contra el codigo tecleado: no hay `return`
    temprano dentro del bucle a proposito, para que el numero de
    candidatas vivas no filtre en que posicion hizo (o no hizo) match."""
    target_hash = code.hash()
    match: uuid.UUID | None = None
    for candidate in candidates:
        if hmac.compare_digest(candidate.pairing_code_hash, target_hash):
            match = candidate.owner_id
    return match
