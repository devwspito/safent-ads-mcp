"""`LiveSignalOutcomeStep`: adaptador real de `SignalOutcomeEvaluationStepPort`
(plan.md §7, profitability-engine.md §6, tasks.md T199). Una sesion propia
por negocio y ciclo, mismo patron que `LiveSignalStep`."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from safent_ads.optimization.application.evaluate_signal_outcomes import EvaluateSignalOutcomes
from safent_ads.optimization.infrastructure.sql_signal_outcome_ports import (
    SqlEntityCpaWindowPort,
    SqlPendingSignalOutcomesPort,
    SqlRuleActionKindPort,
    SqlSignalOutcomeRepository,
    SqlSignalResolutionPort,
)
from safent_ads.shared.clock import Clock
from safent_ads.shared.ids import BusinessId


class LiveSignalOutcomeStep:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession], clock: Clock) -> None:
        self._session_factory = session_factory
        self._clock = clock

    async def run(self, business_id: BusinessId, cycle_id: str, now: datetime) -> None:
        del cycle_id, now
        async with self._session_factory() as session:
            await self._build_use_case(session).execute(business_id=business_id)
            await session.commit()

    def _build_use_case(self, session: AsyncSession) -> EvaluateSignalOutcomes:
        return EvaluateSignalOutcomes(
            due_signals=SqlPendingSignalOutcomesPort(session),
            resolution=SqlSignalResolutionPort(session),
            cpa_window=SqlEntityCpaWindowPort(session),
            action_kinds=SqlRuleActionKindPort(session),
            outcomes=SqlSignalOutcomeRepository(session),
            clock=self._clock,
        )
