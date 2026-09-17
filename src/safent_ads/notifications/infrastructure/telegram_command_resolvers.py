"""Resolutores de `/estado`, `/pendientes` y `/freno` (este branch): una
sesion por invocacion, mismo patron que `TelegramCallbackResolver`/
`TelegramPairingCommandResolver`. Cuatro clases pequenas y emparentadas
(un comando/callback cada una) agrupadas en un unico fichero, igual que
`sql_repositories.py` agrupa varios adaptadores SQL pequenos de esta misma
lane."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from safent_ads.notifications.application.list_pending_proposals import ListPendingProposals
from safent_ads.notifications.application.ports import (
    BrakeCommandReply,
    CallbackOutcome,
    MessengerPort,
)
from safent_ads.notifications.application.report_business_status import ReportBusinessStatus
from safent_ads.notifications.application.resolve_brake_callback import ResolveBrakeCallback
from safent_ads.notifications.application.resolve_brake_command import ResolveBrakeCommand
from safent_ads.notifications.infrastructure.brake_gateway import TelegramBrakeGateway
from safent_ads.notifications.infrastructure.proposal_approval_gateway import (
    ProposalApprovalGateway,
)
from safent_ads.notifications.infrastructure.sql_business_status_reader import (
    SqlBusinessStatusReader,
)
from safent_ads.notifications.infrastructure.sql_repositories import (
    SqlBrakeConfirmationStore,
    SqlPendingProposalIds,
    SqlTelegramCallbackStore,
)
from safent_ads.notifications.infrastructure.telegram_callback_resolver import (
    ExecutionUseCasesFactory,
)
from safent_ads.notifications.infrastructure.telegram_pairing_guard import SqlTelegramPairingGuard
from safent_ads.shared.clock import Clock


class TelegramStatusCommandResolver:
    """`/estado`: solo lectura, no necesita `ExecutionUseCasesFactory` --
    el freno se lee directo de `execution.infrastructure.sql_brake_state`
    (via `SqlBusinessStatusReader`), sin construir todo el resto de
    `ExecutionUseCases` (chokepoint, spend ledger...) que ese comando no
    usa."""

    def __init__(
        self, *, session_factory: async_sessionmaker[AsyncSession], clock: Clock
    ) -> None:
        self._session_factory = session_factory
        self._clock = clock

    async def execute(self, *, chat_id: int) -> tuple[str, ...]:
        async with self._session_factory() as session:
            use_case = ReportBusinessStatus(
                status=SqlBusinessStatusReader(session, self._clock),
                pairing_guard=SqlTelegramPairingGuard(session),
            )
            return await use_case.execute(chat_id=chat_id)


class TelegramPendingCommandResolver:
    """`/pendientes`: reenvia tarjetas de aprobacion nivel 1, mismo `Bot`
    del proceso -- `messenger` es el `AiogramMessenger` ya construido
    (`telegram_channel.py`), inyectado despues por `set_pending_command_resolver`
    para romper el ciclo de construccion (el mensajero registra este
    comando, y este comando necesita enviar por el mismo mensajero)."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        execution_use_cases_factory: ExecutionUseCasesFactory,
        messenger: MessengerPort,
        clock: Clock,
    ) -> None:
        self._session_factory = session_factory
        self._execution_use_cases_factory = execution_use_cases_factory
        self._messenger = messenger
        self._clock = clock

    async def execute(self, *, chat_id: int) -> int:
        async with self._session_factory() as session:
            use_cases = self._execution_use_cases_factory(session)
            use_case = ListPendingProposals(
                pending_ids=SqlPendingProposalIds(session),
                gateway=ProposalApprovalGateway(
                    session=session, use_cases=use_cases, clock=self._clock
                ),
                callback_store=SqlTelegramCallbackStore(session),
                pairing_guard=SqlTelegramPairingGuard(session),
                messenger=self._messenger,
                clock=self._clock,
            )
            sent = await use_case.execute(chat_id=chat_id)
            await session.commit()
            return sent


class TelegramBrakeCommandResolver:
    """`/freno`, `/freno on`, `/freno off`: emite el primer toque; nunca
    aplica el cambio por si solo (`TelegramBrakeCallbackResolver` resuelve
    el segundo)."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        execution_use_cases_factory: ExecutionUseCasesFactory,
        clock: Clock,
    ) -> None:
        self._session_factory = session_factory
        self._execution_use_cases_factory = execution_use_cases_factory
        self._clock = clock

    async def status(self, *, chat_id: int) -> BrakeCommandReply:
        async with self._session_factory() as session:
            return await self._build(session).status(chat_id=chat_id)

    async def request_engage(self, *, chat_id: int) -> BrakeCommandReply:
        async with self._session_factory() as session:
            reply = await self._build(session).request_engage(chat_id=chat_id)
            await session.commit()
            return reply

    async def request_release(self, *, chat_id: int) -> BrakeCommandReply:
        async with self._session_factory() as session:
            reply = await self._build(session).request_release(chat_id=chat_id)
            await session.commit()
            return reply

    def _build(self, session: AsyncSession) -> ResolveBrakeCommand:
        use_cases = self._execution_use_cases_factory(session)
        return ResolveBrakeCommand(
            gateway=TelegramBrakeGateway(
                toggle=use_cases.toggle_emergency_brake, brakes=use_cases.brakes
            ),
            confirmations=SqlBrakeConfirmationStore(session),
            pairing_guard=SqlTelegramPairingGuard(session),
            clock=self._clock,
        )


class TelegramBrakeCallbackResolver:
    """Segundo toque de `/freno on|off` (`brk:<nonce>:<y|n>`): la MISMA
    transaccion consume el nonce y aplica el cambio, igual que
    `TelegramCallbackResolver` con las propuestas."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        execution_use_cases_factory: ExecutionUseCasesFactory,
        clock: Clock,
    ) -> None:
        self._session_factory = session_factory
        self._execution_use_cases_factory = execution_use_cases_factory
        self._clock = clock

    async def resolve(
        self, *, callback_data: str, chat_id: int, from_user_id: int, message_id: int
    ) -> CallbackOutcome:
        async with self._session_factory() as session:
            use_cases = self._execution_use_cases_factory(session)
            resolver = ResolveBrakeCallback(
                confirmations=SqlBrakeConfirmationStore(session),
                gateway=TelegramBrakeGateway(
                    toggle=use_cases.toggle_emergency_brake, brakes=use_cases.brakes
                ),
                pairing_guard=SqlTelegramPairingGuard(session),
                clock=self._clock,
            )
            outcome = await resolver.execute(
                callback_data=callback_data,
                chat_id=chat_id,
                from_user_id=from_user_id,
                message_id=message_id,
            )
            await session.commit()
            return outcome
