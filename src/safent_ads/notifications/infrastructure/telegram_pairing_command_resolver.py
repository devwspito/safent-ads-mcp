"""`TelegramPairingCommandResolver`: adaptador `TelegramPairingCommandPort`
que resuelve `/emparejar <codigo>` -- una sesion por comando, mismo patron
que `TelegramCallbackResolver`. Unica pieza de `notifications` que junta
`confirm_telegram_pairing` (aplicacion) con `audit` (decision-log):
`telegram_pairing_succeeded|failed` acierte o falle (contracts/telegram.md
§Emparejamiento)."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from safent_ads.audit.application.record_decision import RecordDecision
from safent_ads.audit.domain.entry import ActorKind, DecisionKind, PendingDecision
from safent_ads.audit.infrastructure.sql_repository import SqlDecisionLogRepository
from safent_ads.notifications.application.confirm_telegram_pairing import (
    ConfirmTelegramPairing,
    ConfirmTelegramPairingCommand,
)
from safent_ads.notifications.application.ports import TelegramPairingConfirmationOutcome
from safent_ads.notifications.domain.pairing import UNSCOPED_BUSINESS_ID, mask_chat_id
from safent_ads.notifications.infrastructure.telegram_pairing_sql import (
    SqlTelegramPairingAttempts,
    SqlTelegramPairingConfirmation,
)
from safent_ads.shared.clock import Clock

_SUCCESS_DECISION_KINDS = {
    TelegramPairingConfirmationOutcome.CONFIRMED: DecisionKind.TELEGRAM_PAIRING_SUCCEEDED
}


class TelegramPairingCommandResolver:
    def __init__(
        self, *, session_factory: async_sessionmaker[AsyncSession], clock: Clock
    ) -> None:
        self._session_factory = session_factory
        self._clock = clock

    async def confirm(
        self, *, chat_id: int, code_text: str
    ) -> TelegramPairingConfirmationOutcome:
        async with self._session_factory() as session:
            use_case = ConfirmTelegramPairing(
                pairing=SqlTelegramPairingConfirmation(session),
                attempts=SqlTelegramPairingAttempts(session),
                clock=self._clock,
            )
            outcome = await use_case.execute(
                ConfirmTelegramPairingCommand(chat_id=chat_id, code_text=code_text)
            )
            await self._record_outcome(session, chat_id=chat_id, outcome=outcome)
            await session.commit()
            return outcome

    async def _record_outcome(
        self,
        session: AsyncSession,
        *,
        chat_id: int,
        outcome: TelegramPairingConfirmationOutcome,
    ) -> None:
        kind = _SUCCESS_DECISION_KINDS.get(outcome, DecisionKind.TELEGRAM_PAIRING_FAILED)
        recorder = RecordDecision(SqlDecisionLogRepository(session))
        await recorder.execute(
            PendingDecision(
                business_id=UNSCOPED_BUSINESS_ID,
                kind=kind,
                actor_kind=ActorKind.OWNER,
                payload={"chat_id_masked": mask_chat_id(chat_id), "outcome": outcome.value},
            )
        )
