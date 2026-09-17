"""`SqlTelegramPairingGuard` implementa `TelegramPairingGuardPort`. Vive en
su propio fichero, separado de `sql_repositories.py`, por el mismo motivo
que `proposal_approval_gateway.py`: es la UNICA pieza de `notifications`
que importa `audit` para dejar constancia en `decision_log` de una
pulsacion denegada por falta de emparejamiento
(contracts/telegram.md: "sin fila verificada el canal avisa pero no
decide")."""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from safent_ads.audit.application.record_decision import RecordDecision
from safent_ads.audit.domain.entry import ActorKind, DecisionKind, PendingDecision
from safent_ads.audit.infrastructure.sql_repository import SqlDecisionLogRepository
from safent_ads.notifications.domain.pairing import UNSCOPED_BUSINESS_ID, mask_chat_id

_IS_CHAT_PAIRED = text(
    "SELECT 1 FROM telegram_owner_chats WHERE chat_id = :chat_id AND status = 'paired'"
)


class SqlTelegramPairingGuard:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def is_chat_paired(self, chat_id: int) -> bool:
        result = await self._session.execute(_IS_CHAT_PAIRED, {"chat_id": chat_id})
        return result.first() is not None

    async def record_unpaired_callback(self, chat_id: int) -> None:
        recorder = RecordDecision(SqlDecisionLogRepository(self._session))
        await recorder.execute(
            PendingDecision(
                business_id=UNSCOPED_BUSINESS_ID,
                kind=DecisionKind.TELEGRAM_CALLBACK_UNPAIRED,
                actor_kind=ActorKind.SYSTEM,
                payload={"chat_id_masked": mask_chat_id(chat_id)},
            )
        )

    async def record_unpaired_command_denied(self, chat_id: int, *, command: str) -> None:
        recorder = RecordDecision(SqlDecisionLogRepository(self._session))
        await recorder.execute(
            PendingDecision(
                business_id=UNSCOPED_BUSINESS_ID,
                kind=DecisionKind.TELEGRAM_UNPAIRED_COMMAND_DENIED,
                actor_kind=ActorKind.SYSTEM,
                payload={"chat_id_masked": mask_chat_id(chat_id), "command": command},
            )
        )
