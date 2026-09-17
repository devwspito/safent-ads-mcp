"""`TelegramCallbackResolver`: adaptador `CallbackResolverPort` (T079) que
abre una sesion por pulsacion -- el mismo patron que
`composition/execution_rest.py` (`async with session_factory() as session:
... await session.commit()`), porque consumir el nonce y aplicar la
decision (`SubmitApproval`/`Proposal.reject`/`UndoExecution`) tienen que
caer en la MISMA transaccion: si el nonce se consumiera y la decision
fallase a medias, un reintento del propietario ya no podria repetir la
pulsacion (el nonce es de un solo uso)."""

from __future__ import annotations

from collections.abc import Callable

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from safent_ads.composition.container import ExecutionUseCases
from safent_ads.notifications.application.ports import CallbackOutcome
from safent_ads.notifications.application.resolve_callback import ResolveCallback
from safent_ads.notifications.infrastructure.proposal_approval_gateway import (
    ProposalApprovalGateway,
)
from safent_ads.notifications.infrastructure.sql_repositories import SqlTelegramCallbackStore
from safent_ads.notifications.infrastructure.telegram_pairing_guard import SqlTelegramPairingGuard
from safent_ads.shared.clock import Clock

ExecutionUseCasesFactory = Callable[[AsyncSession], ExecutionUseCases]


class TelegramCallbackResolver:
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
            resolver = ResolveCallback(
                store=SqlTelegramCallbackStore(session),
                gateway=ProposalApprovalGateway(
                    session=session, use_cases=use_cases, clock=self._clock
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
